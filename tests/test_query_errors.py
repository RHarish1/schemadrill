import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from instructor.core.exceptions import InstructorRetryException
from psycopg import Error as PsycopgError

from app.auth import _jwks_client
from app.config import Settings
from app.main import app
from app.models import DDLBlock, SqlResponse
from app.pipeline import run_pipeline


def _auth_headers(monkeypatch) -> dict[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
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
            "sub": "user-test-1",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
        private_key,
        algorithm="EdDSA",
    )
    return {"Authorization": f"Bearer {token}"}


def _wrapped_genai_error(status_code: int, status_text: str, message: str, *, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(status_code, headers=headers)
    payload = {"error": {"code": status_code, "message": message, "status": status_text}}
    inner = (
        genai_errors.ClientError(status_code, payload, response)
        if 400 <= status_code < 500
        else genai_errors.ServerError(status_code, payload, response)
    )
    try:
        raise InstructorRetryException(
            str(inner),
            n_attempts=1,
            total_usage=0,
            failed_attempts=[],
        ) from inner
    except InstructorRetryException as wrapped:
        return wrapped


def test_query_exposes_model_rate_limit_safely_and_logs_details(monkeypatch, caplog):
    headers = _auth_headers(monkeypatch)
    wrapped_429 = _wrapped_genai_error(
        429, "RESOURCE_EXHAUSTED", "Quota exceeded for model", retry_after="7"
    )

    def raise_rate_limit(*args, **kwargs):
        raise wrapped_429

    monkeypatch.setattr("app.main.run_pipeline", raise_rate_limit)

    with caplog.at_level("WARNING", logger="app.main"):
        with TestClient(app) as client:
            response = client.post(
                "/query",
                headers={**headers, "X-Request-Id": "req-429-test"},
                json={"db": "chinook", "question": "List artists"},
            )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.headers["X-Request-Id"] == "req-429-test"
    assert response.json() == {
        "detail": (
            "The free tier AI model is temporarily busy. Please try again in a moment "
            "or add your own Gemini API key in Settings."
        )
    }
    assert "query_provider_rate_limited" in caplog.text
    assert "request_id=req-429-test" in caplog.text
    assert "root_error_type=ClientError" in caplog.text
    assert "status_code=429" in caplog.text


def test_query_exposes_model_unavailable_safely_and_logs_details(monkeypatch, caplog):
    headers = _auth_headers(monkeypatch)
    wrapped_503 = _wrapped_genai_error(
        503,
        "UNAVAILABLE",
        "This model is currently experiencing high demand.",
    )

    def raise_unavailable(*args, **kwargs):
        raise wrapped_503

    monkeypatch.setattr("app.main.run_pipeline", raise_unavailable)

    with caplog.at_level("WARNING", logger="app.main"):
        with TestClient(app) as client:
            response = client.post(
                "/query",
                headers=headers,
                json={"db": "chinook", "question": "List artists"},
            )

    assert response.status_code == 503
    assert response.headers.get("X-Request-Id")
    assert response.json() == {
        "detail": "The AI model is temporarily busy or unavailable. Please try again in a moment."
    }
    assert "query_provider_unavailable" in caplog.text
    assert "root_error_type=ServerError" in caplog.text
    assert "status_code=503" in caplog.text


def test_query_hides_internal_config_mistakes_and_logs_exception(monkeypatch, caplog):
    headers = _auth_headers(monkeypatch)

    def raise_config_error(*args, **kwargs):
        raise RuntimeError("GEMINI_API_KEY is not configured")

    monkeypatch.setattr("app.main.run_pipeline", raise_config_error)

    with caplog.at_level("ERROR", logger="app.main"):
        with TestClient(app) as client:
            response = client.post(
                "/query",
                headers=headers,
                json={"db": "chinook", "question": "List artists"},
            )

    assert response.status_code == 500
    assert response.json() == {"detail": "Query failed"}
    assert "GEMINI_API_KEY" not in response.text
    assert "query_failed" in caplog.text
    assert "GEMINI_API_KEY is not configured" in caplog.text


def test_pipeline_execution_error_logs_detail_without_exposing_db_internals(monkeypatch, caplog):
    @contextmanager
    def fake_conn():
        yield object()

    monkeypatch.setattr("app.pipeline.readonly_connection", fake_conn)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)

    def fail_execute(connection, sql, max_rows):
        raise PsycopgError("permission denied for table private_billing_ledger")

    monkeypatch.setattr("app.pipeline.execute", fail_execute)

    with caplog.at_level("WARNING", logger="app.pipeline"):
        result = run_pipeline(
            "question",
            "chinook",
            settings=Settings(_env_file=None, max_retries=1),
            retrieve=lambda question, db, top_k: [
                DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")
            ],
            generate_sql=lambda messages: SqlResponse(sql="SELECT 1"),
        )

    assert result.status == "failed"
    assert result.reason == "Query execution failed."
    assert "private_billing_ledger" not in result.reason
    assert "pipeline_execution_failed" in caplog.text
    assert "private_billing_ledger" in caplog.text


def test_unhandled_endpoint_error_hides_internals_and_logs_traceback(monkeypatch, caplog):
    headers = _auth_headers(monkeypatch)

    def explode(*args, **kwargs):
        raise RuntimeError("secret db host password leaked")

    monkeypatch.setattr("app.main.provision_user", explode)

    with caplog.at_level("ERROR", logger="app.main"):
        with TestClient(app) as client:
            response = client.get("/api/me", headers=headers)

    assert response.status_code == 500
    assert response.headers.get("X-Request-Id")
    assert response.json() == {"detail": "Internal server error"}
    assert "secret db host" not in response.text
    assert "unhandled_request_error" in caplog.text
    assert "secret db host password leaked" in caplog.text
