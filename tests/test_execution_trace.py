import json
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
from app.reliability import RetryPolicy, call_with_provider_retry_sync
from app.trace import InternalTrace, build_public_execution_trace, get_current_trace


class _FakeDataFrame:
    columns = ["name"]

    def to_dict(self, orient="records"):
        assert orient == "records"
        return [{"name": "AC/DC"}, {"name": "Accept"}]

    def to_markdown(self, index=False):
        return "| name |\n|---|\n| AC/DC |\n| Accept |"


@contextmanager
def _fake_connection():
    yield object()


def _settings(**overrides):
    defaults = {
        "_env_file": None,
        "max_retries": 3,
        "disable_self_correction": False,
        "top_k": 3,
        "model_name": "gemini-3.1-flash-lite",
        "gemini_api_key": "AIzaSySecretKeyNeverExpose123456",
        "database_url": "postgresql://user:supersecret@db.internal:5432/schemadrill",
        "preload_embedding_model": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _auth_headers(monkeypatch) -> dict[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        model_name="gemini-3.1-flash-lite",
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
            "sub": "user-trace-1",
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


def test_pipeline_returns_sanitized_execution_trace_and_logs_internal_telemetry(
    monkeypatch, caplog
):
    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: _FakeDataFrame())

    def retrieve_with_internal_telemetry(question, db, top_k):
        trace = get_current_trace()
        assert trace is not None
        trace.record_embedding(
            14.2,
            [{"schema_name": db, "table_name": "artist", "cosine_similarity": 0.91}],
        )
        return [
            DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist (id int);"),
            DDLBlock(schema_name=db, table_name="album", ddl_text="CREATE TABLE album (id int);"),
        ]

    with caplog.at_level("INFO", logger="app.trace"):
        result = run_pipeline(
            "List artists",
            "chinook",
            settings=_settings(),
            retrieve=retrieve_with_internal_telemetry,
            generate_sql=lambda messages: SqlResponse(sql="SELECT name FROM artist"),
        )

    assert result.status == "success"
    assert result.execution_trace is not None

    trace = result.execution_trace
    assert [stage.name for stage in trace.stages] == [
        "Schema retrieval",
        "SQL generation",
        "SQL validation",
        "Query execution",
    ]
    assert [stage.status for stage in trace.stages] == [
        "completed",
        "completed",
        "passed",
        "completed",
    ]
    assert trace.stages[0].details == {"tables_identified": 2}
    assert trace.stages[1].details == {
        "provider": "Google Gemini",
        "model": "gemini-3.1-flash-lite",
    }
    assert "summary" in trace.stages[2].details
    assert trace.stages[2].details["sql_syntax"] == "passed"
    assert trace.stages[2].details["read_only_policy"] == "passed"
    assert trace.stages[2].details["dry_run"] == "passed"
    assert trace.stages[3].details == {"rows_returned": 2}
    assert trace.retrieved_tables == ["artist", "album"]
    assert trace.top_k == 3
    assert trace.fk_expansion is False
    assert trace.execution_mode == "read_only_transaction"
    assert trace.retry_count == 0
    assert trace.retry_reasons == []
    assert trace.total_duration_ms >= 0

    serialized = json.dumps(trace.model_dump())
    assert "cosine_similarity" not in serialized
    assert "0.91" not in serialized
    assert "prompt_tokens" not in serialized
    assert "sqlglot" not in serialized
    assert "AIzaSySecretKeyNeverExpose123456" not in serialized
    assert "supersecret" not in serialized

    assert "pipeline_internal_telemetry" in caplog.text
    assert "cosine_similarity" in caplog.text
    assert "prompt_tokens=" in caplog.text
    assert "AIzaSySecretKeyNeverExpose123456" not in caplog.text
    assert "supersecret" not in caplog.text


def test_execution_trace_records_self_correction_retries_without_leaking_db_details(monkeypatch):
    generated = iter(
        [
            SqlResponse(sql="SELECT bad_col FROM artist"),
            SqlResponse(sql="SELECT name FROM artist"),
        ]
    )

    def dry_run_once_fail(connection, sql):
        if "bad_col" in sql:
            raise PsycopgError("column artist.bad_col does not exist on private_schema")

    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", dry_run_once_fail)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: _FakeDataFrame())

    result = run_pipeline(
        "List artists",
        "chinook",
        settings=_settings(),
        retrieve=lambda q, db, k: [
            DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")
        ],
        generate_sql=lambda messages: next(generated),
    )

    assert result.status == "success"
    assert result.attempts == 2
    trace = result.execution_trace
    assert trace is not None
    assert trace.retry_count == 1
    assert trace.retry_reasons == ["Database dry-run validation"]
    assert trace.stages[2].name == "SQL validation"
    assert trace.stages[2].status == "passed"
    assert "private_schema" not in json.dumps(trace.model_dump())
    assert "bad_col" not in json.dumps(trace.model_dump())


def test_execution_trace_available_for_validation_failure_and_execution_failure(monkeypatch):
    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)

    def fail_execute(connection, sql, max_rows):
        raise PsycopgError("permission denied for table internal_payroll")

    monkeypatch.setattr("app.pipeline.execute", fail_execute)

    result = run_pipeline(
        "List artists",
        "chinook",
        settings=_settings(max_retries=1),
        retrieve=lambda q, db, k: [
            DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")
        ],
        generate_sql=lambda messages: SqlResponse(sql="SELECT name FROM artist"),
    )

    assert result.status == "failed"
    trace = result.execution_trace
    assert trace is not None
    assert [s.status for s in trace.stages] == ["completed", "completed", "passed", "failed"]
    assert trace.stages[3].details == {"summary": "Query execution failed."}
    assert "internal_payroll" not in json.dumps(trace.model_dump())


def test_public_trace_serializer_strips_unknown_internal_detail_keys():
    internal = InternalTrace(request_id="req-1", db="chinook")
    internal.record_retrieval(["chinook.artist"], ["artist"])
    internal.record_stage(
        "Schema retrieval",
        status="completed",
        duration_ms=12.3456,
        details={
            "tables_identified": 1,
            "similarity_scores": [0.99],
            "raw_ddl": "CREATE TABLE secret...",
        },
    )
    public = build_public_execution_trace(internal, finalize=True)
    assert public.stages[0].duration_ms == 12.35
    assert public.stages[0].details == {"tables_identified": 1}


def test_query_endpoint_returns_execution_trace_on_provider_503_failure(monkeypatch):
    headers = _auth_headers(monkeypatch)
    wrapped_503 = _wrapped_genai_error(
        503,
        "UNAVAILABLE",
        "This model is currently experiencing high demand.",
    )

    def failing_generate(messages):
        trace = get_current_trace()
        return call_with_provider_retry_sync(
            lambda: (_ for _ in ()).throw(wrapped_503),
            RetryPolicy(
                max_attempts=3,
                base_delay_seconds=0.0,
                max_delay_seconds=0.0,
                jitter_seconds=0.0,
            ),
            sleep=lambda _: None,
            on_retry=lambda attempt, info: (
                trace.record_provider_retry(
                    attempt=attempt,
                    category=info.category,
                    status_code=info.status_code,
                    root_error_type=info.root_error_type,
                )
                if trace
                else None
            ),
        )

    def pipeline_with_503(question, db, settings=None, trace=None):
        return run_pipeline(
            question,
            db,
            settings=settings,
            retrieve=lambda q, d, k: [
                DDLBlock(schema_name=d, table_name="artist", ddl_text="CREATE TABLE artist;"),
                DDLBlock(schema_name=d, table_name="album", ddl_text="CREATE TABLE album;"),
            ],
            generate_sql=failing_generate,
            trace=trace,
        )

    monkeypatch.setattr("app.main.run_pipeline", pipeline_with_503)

    with TestClient(app) as client:
        response = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "List artists"},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == (
        "The AI model is temporarily busy or unavailable. Please try again in a moment."
    )
    trace = body["execution_trace"]
    assert [stage["name"] for stage in trace["stages"]] == [
        "Schema retrieval",
        "SQL generation",
    ]
    assert trace["stages"][0]["status"] == "completed"
    assert trace["stages"][0]["details"] == {"tables_identified": 2}
    assert trace["stages"][1]["status"] == "failed"
    assert trace["stages"][1]["details"]["summary"] == "Provider temporarily unavailable."
    assert trace["retry_count"] == 2
    assert trace["retry_reasons"] == ["Provider unavailable"]
    assert trace["retrieved_tables"] == ["artist", "album"]


def test_query_endpoint_returns_execution_trace_on_internal_error_without_leaking_secrets(
    monkeypatch,
):
    headers = _auth_headers(monkeypatch)

    def pipeline_with_config_error(question, db, settings=None, trace=None):
        return run_pipeline(
            question,
            db,
            settings=settings,
            retrieve=lambda q, d, k: [
                DDLBlock(schema_name=d, table_name="artist", ddl_text="CREATE TABLE artist;")
            ],
            generate_sql=lambda messages: (_ for _ in ()).throw(
                RuntimeError("GEMINI_API_KEY is not configured")
            ),
            trace=trace,
        )

    monkeypatch.setattr("app.main.run_pipeline", pipeline_with_config_error)

    with TestClient(app) as client:
        response = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "List artists"},
        )

    assert response.status_code == 500
    assert "GEMINI_API_KEY" not in response.text
    body = response.json()
    assert body["detail"] == "Query failed"
    assert body["execution_trace"]["stages"][0]["status"] == "completed"
    assert body["execution_trace"]["stages"][1]["status"] == "failed"
    assert body["execution_trace"]["stages"][1]["details"]["summary"] == "SQL generation failed."


def test_execution_trace_reports_independent_validation_checks_and_readonly_role(monkeypatch):
    monkeypatch.setattr("app.pipeline.readonly_connection", _fake_connection)
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: _FakeDataFrame())

    guard_failed = run_pipeline(
        "Delete artists",
        "chinook",
        settings=_settings(
            max_retries=1,
            database_url="postgresql://schemadrill_readonly:secret@db.internal:5432/schemadrill",
        ),
        retrieve=lambda q, db, k: [
            DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")
        ],
        generate_sql=lambda messages: SqlResponse(
            sql="WITH d AS (DELETE FROM artist RETURNING *) SELECT * FROM d"
        ),
    )
    assert guard_failed.status == "failed"
    assert guard_failed.execution_trace is not None
    assert guard_failed.execution_trace.execution_mode == "read_only_role_and_transaction"
    assert guard_failed.execution_trace.stages[2].details["sql_syntax"] == "passed"
    assert guard_failed.execution_trace.stages[2].details["read_only_policy"] == "failed"
    assert guard_failed.execution_trace.stages[2].details["dry_run"] == "skipped"

    def fail_dry_run(connection, sql):
        raise PsycopgError("column missing")

    monkeypatch.setattr("app.pipeline.dry_run", fail_dry_run)
    dry_run_failed = run_pipeline(
        "List artists",
        "chinook",
        settings=_settings(max_retries=1),
        retrieve=lambda q, db, k: [
            DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")
        ],
        generate_sql=lambda messages: SqlResponse(sql="SELECT bad_col FROM artist"),
    )
    assert dry_run_failed.status == "failed"
    assert dry_run_failed.execution_trace is not None
    assert dry_run_failed.execution_trace.stages[2].details["sql_syntax"] == "passed"
    assert dry_run_failed.execution_trace.stages[2].details["read_only_policy"] == "passed"
    assert dry_run_failed.execution_trace.stages[2].details["dry_run"] == "failed"
