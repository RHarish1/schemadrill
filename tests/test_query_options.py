import time
from contextlib import contextmanager
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.auth import _jwks_client
from app.config import Settings
from app.main import app
from app.models import DDLBlock, RetrievalResult, SqlResponse
from app.pipeline import run_pipeline


class _FakeDataFrame:
    columns = ["name"]

    def to_dict(self, orient="records"):
        return [{"name": "AC/DC"}]

    def to_markdown(self, index=False):
        return "| name |\n|---|\n| AC/DC |"


@contextmanager
def _fake_connection():
    yield object()


def _configure_test_env(monkeypatch, *, base_settings: Settings | None = None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    settings = base_settings or Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        top_k=3,
        enable_fk_expansion=False,
        max_retries=3,
        disable_self_correction=False,
    )
    monkeypatch.setattr("app.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    fake_jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr("app.auth._jwks_client", lambda: fake_jwks)
    monkeypatch.setattr("app.main.provision_user", lambda *args, **kwargs: False)
    monkeypatch.setattr("app.main.user_has_dataset", lambda *args, **kwargs: True)

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "user-options-1",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
        private_key,
        algorithm="EdDSA",
    )
    return {"Authorization": f"Bearer {token}"}


def test_query_requires_authentication_for_tuning_options(monkeypatch):
    _configure_test_env(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/query",
            json={
                "db": "chinook",
                "question": "List artists",
                "top_k": 5,
                "fk_expansion": True,
                "max_retries": 2,
            },
        )
    assert response.status_code == 401


def test_query_applies_default_and_custom_tuning_options_to_pipeline(monkeypatch):
    headers = _configure_test_env(monkeypatch)
    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: _FakeDataFrame())

    observed_retrieval_args: list[tuple[int, bool]] = []

    def fake_get_retrieval_provider(settings):
        def retrieve(question, db, top_k):
            observed_retrieval_args.append((top_k, settings.enable_fk_expansion))
            blocks = [
                DDLBlock(
                    schema_name=db,
                    table_name="artist",
                    ddl_text="CREATE TABLE artist (id int);",
                )
            ]
            if settings.enable_fk_expansion:
                blocks.append(
                    DDLBlock(
                        schema_name=db,
                        table_name="album",
                        ddl_text="CREATE TABLE album (id int);",
                    )
                )
                return RetrievalResult(blocks=blocks, fk_added=[f"{db}.album"])
            return blocks

        return SimpleNamespace(retrieve=retrieve)

    monkeypatch.setattr("app.pipeline.get_retrieval_provider", fake_get_retrieval_provider)

    def pipeline_with_mocked_llm(question, db, settings=None, trace=None):
        # Fail first attempt, succeed second attempt
        generated = iter(
            [
                SqlResponse(sql="SELECT FROM"),
                SqlResponse(sql="SELECT name FROM artist"),
            ]
        )
        return run_pipeline(
            question,
            db,
            settings=settings,
            generate_sql=lambda messages: next(generated),
            trace=trace,
        )

    monkeypatch.setattr("app.main.run_pipeline", pipeline_with_mocked_llm)

    with TestClient(app) as client:
        default_resp = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "List artists"},
        )
        assert default_resp.status_code == 200
        default_body = default_resp.json()
        assert default_body["status"] == "success"
        assert default_body["attempts"] == 2
        assert default_body["execution_trace"]["top_k"] == 3
        assert default_body["execution_trace"]["fk_expansion"] is False
        assert default_body["execution_trace"]["retry_count"] == 1

        no_retry_resp = client.post(
            "/query",
            headers=headers,
            json={
                "db": "chinook",
                "question": "List artists",
                "top_k": 5,
                "fk_expansion": True,
                "max_retries": 0,
            },
        )
        assert no_retry_resp.status_code == 200
        no_retry_body = no_retry_resp.json()
        assert no_retry_body["status"] == "failed"
        assert no_retry_body["attempts"] == 1
        assert no_retry_body["execution_trace"]["top_k"] == 5
        assert no_retry_body["execution_trace"]["fk_expansion"] is True
        assert no_retry_body["execution_trace"]["retry_count"] == 0
        assert no_retry_body["execution_trace"]["retrieved_tables"] == ["artist", "album"]

    assert observed_retrieval_args == [(3, False), (5, True)]


def test_query_allows_up_to_three_retries_when_requested(monkeypatch):
    headers = _configure_test_env(monkeypatch)
    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: _FakeDataFrame())

    def pipeline_with_three_failures_then_success(question, db, settings=None, trace=None):
        generated = iter(
            [
                SqlResponse(sql="SELECT FROM"),
                SqlResponse(sql="SELECT FROM"),
                SqlResponse(sql="SELECT FROM"),
                SqlResponse(sql="SELECT name FROM artist"),
            ]
        )
        return run_pipeline(
            question,
            db,
            settings=settings,
            retrieve=lambda q, d, k: [
                DDLBlock(schema_name=d, table_name="artist", ddl_text="CREATE TABLE artist;")
            ],
            generate_sql=lambda messages: next(generated),
            trace=trace,
        )

    monkeypatch.setattr("app.main.run_pipeline", pipeline_with_three_failures_then_success)

    with TestClient(app) as client:
        response = client.post(
            "/query",
            headers=headers,
            json={
                "db": "chinook",
                "question": "List artists",
                "top_k": 2,
                "fk_expansion": False,
                "max_retries": 3,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["attempts"] == 4
    assert body["execution_trace"]["top_k"] == 2
    assert body["execution_trace"]["fk_expansion"] is False
    assert body["execution_trace"]["retry_count"] == 3


def test_query_validates_tuning_option_bounds(monkeypatch):
    headers = _configure_test_env(monkeypatch)
    with TestClient(app) as client:
        for invalid_payload in (
            {"db": "chinook", "question": "List artists", "top_k": 0},
            {"db": "chinook", "question": "List artists", "top_k": 11},
            {"db": "chinook", "question": "List artists", "max_retries": -1},
            {"db": "chinook", "question": "List artists", "max_retries": 4},
        ):
            response = client.post("/query", headers=headers, json=invalid_payload)
            assert response.status_code == 422
