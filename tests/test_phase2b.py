import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

import jwt
import pandas as pd
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import _jwks_client
from app.config import Settings
from app.connections import (
    ConnectionNotFoundError,
    DatabaseTimeoutError,
    DatabaseUnreachableError,
    ParsedConnectionInfo,
    TenantConnectionSummary,
)
from app.executor import execute
from app.main import app
from app.models import ByteCapExceeded, DDLBlock, SqlResponse
from app.pipeline import run_pipeline
from app.retrieval import TenantPgVectorRetrievalProvider
from app.sql_guard import (
    AUTHORIZATION_DENIED_MESSAGE,
    default_guard_config,
    guard_sql,
)
from app.trace import InternalTrace, build_public_execution_trace


def _make_auth(monkeypatch, *, sub: str = "user-2b-1", settings: Settings | None = None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    effective_settings = settings or Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        gemini_api_key="AIzaSyTestFreeKey123456",
    )
    monkeypatch.setattr("app.auth.get_settings", lambda: effective_settings)
    monkeypatch.setattr("app.main.get_settings", lambda: effective_settings)
    monkeypatch.setattr("app.main.provision_user", lambda *args, **kwargs: False)
    monkeypatch.setattr("app.main.reap_expired_and_stale_connections", lambda *args, **kwargs: None)
    fake_jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr("app.auth._jwks_client", lambda: fake_jwks)
    token = jwt.encode(
        {"sub": sub, "iss": "https://auth.example.neon.tech/neondb/auth", "exp": 2_000_000_000},
        private_key,
        algorithm="EdDSA",
    )
    return {"Authorization": f"Bearer {token}"}, effective_settings


def _sample_conn_summary(
    *,
    conn_id: str = "11111111-2222-3333-4444-555555555555",
    ingest_status: str = "ready",
    is_expired: bool = False,
    schemas: list[str] | None = None,
) -> TenantConnectionSummary:
    now = datetime.datetime.now(datetime.UTC)
    expires = (
        now - datetime.timedelta(hours=1) if is_expired else now + datetime.timedelta(hours=24)
    )
    return TenantConnectionSummary(
        id=conn_id,
        label="My Neon DB",
        host="ep-cool-123.us-east-2.aws.neon.tech",
        database="neondb",
        role_name="schemadrill_reader",
        schemas=schemas or ["public"],
        ingest_status=ingest_status,
        table_count=5,
        ingested_at=now,
        expires_at=expires,
        created_at=now,
        is_expired=is_expired,
        needs_credentials=True,
    )


# ---------------------------------------------------------------------------
# 1. Tenant Retrieval (TenantPgVectorRetrievalProvider)
# ---------------------------------------------------------------------------


def test_tenant_retrieval_scopes_to_connection_and_user_and_uses_exact_scan(monkeypatch):
    executed_queries: list[tuple[str, tuple]] = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            executed_queries.append((sql, params))

        def fetchall(self):
            return [
                {
                    "schema_name": "public",
                    "table_name": "orders",
                    "ddl_text": "CREATE TABLE public.orders (id int);",
                    "cosine_distance": 0.12,
                }
            ]

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self, **kwargs):
            return FakeCursor()

    monkeypatch.setattr("app.retrieval.embed_query", lambda q: [0.1] * 384)
    monkeypatch.setattr("app.retrieval.psycopg.connect", lambda dsn: FakeConn())

    settings = Settings(_env_file=None, preload_embedding_model=False)
    provider = TenantPgVectorRetrievalProvider(
        settings,
        connection_id="11111111-2222-3333-4444-555555555555",
        user_id="user-a",
    )
    blocks = provider.retrieve("total orders", "public", 3)

    assert len(blocks) == 1
    assert blocks[0].table_name == "orders"
    sql, params = executed_queries[0]
    assert "FROM public.tenant_schema_embeddings" in sql
    assert "WHERE connection_id = %s AND user_id = %s AND schema_name = %s" in sql
    assert "ORDER BY embedding <=> %s::vector" in sql
    assert params[1] == "11111111-2222-3333-4444-555555555555"
    assert params[2] == "user-a"
    assert params[3] == "public"


def test_tenant_retrieval_fk_expansion(monkeypatch):
    def fake_full_schema(self, db):
        return [
            DDLBlock(
                schema_name=db,
                table_name="orders",
                ddl_text=(
                    "CREATE TABLE public.orders "
                    "(id int, FOREIGN KEY (user_id) REFERENCES users(id));"
                ),
            ),
            DDLBlock(
                schema_name=db,
                table_name="users",
                ddl_text="CREATE TABLE public.users (id int);",
            ),
            DDLBlock(
                schema_name=db,
                table_name="unrelated",
                ddl_text="CREATE TABLE public.unrelated (id int);",
            ),
        ]

    monkeypatch.setattr(
        TenantPgVectorRetrievalProvider,
        "_retrieve_exact_pgvector",
        lambda self, q, db, k: [fake_full_schema(self, db)[0]],
    )
    monkeypatch.setattr(TenantPgVectorRetrievalProvider, "_retrieve_full_schema", fake_full_schema)

    settings = Settings(_env_file=None, preload_embedding_model=False, enable_fk_expansion=True)
    provider = TenantPgVectorRetrievalProvider(settings, connection_id="c1", user_id="u1")
    res = provider.retrieve("orders", "public", 1)

    assert [b.table_name for b in res.blocks] == ["orders", "users"]
    assert res.fk_added == ["public.users"]


# ---------------------------------------------------------------------------
# 2. Query Path: Validation, Tier Rule, Concurrency, Errors, Stale Hints
# ---------------------------------------------------------------------------


def test_user_db_query_rejects_free_tier_by_default(monkeypatch):
    headers, _ = _make_auth(monkeypatch)
    headers["X-DB-Url"] = quote(
        "postgresql://schemadrill_reader:pw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    with TestClient(app) as client:
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "List tables",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "byok_required"


def test_user_db_query_allows_free_tier_when_configured(monkeypatch):
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        gemini_api_key="AIzaSyTestFreeKey123456",
        allow_free_tier_on_user_db=True,
    )
    headers, _ = _make_auth(monkeypatch, settings=settings)
    # Missing X-DB-Url -> gets past tier check and hits db_credentials_required
    with TestClient(app) as client:
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "List tables",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "db_credentials_required"


def test_user_db_query_404_and_409_states(monkeypatch):
    headers, _ = _make_auth(monkeypatch)
    headers["X-LLM-Api-Key"] = "AIzaSyByokKey123456"
    headers["X-DB-Url"] = quote(
        "postgresql://schemadrill_reader:pw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )

    # 1. Not found / cross-tenant -> 404
    monkeypatch.setattr(
        "app.main.get_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConnectionNotFoundError("Connection not found")
        ),
    )
    with TestClient(app) as client:
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 404

        # 2. ingest_status != ready -> 409
        monkeypatch.setattr(
            "app.main.get_connection",
            lambda *args, **kwargs: _sample_conn_summary(ingest_status="running"),
        )
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "connection_not_ready"

        # 3. Expired -> 409 schema_stale
        monkeypatch.setattr(
            "app.main.get_connection",
            lambda *args, **kwargs: _sample_conn_summary(is_expired=True),
        )
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "schema_stale"

        # 4. Schema not in connection -> 404
        monkeypatch.setattr(
            "app.main.get_connection",
            lambda *args, **kwargs: _sample_conn_summary(schemas=["analytics"]),
        )
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 404


def test_user_db_connection_mismatch_returns_403(monkeypatch):
    headers, _ = _make_auth(monkeypatch)
    headers["X-LLM-Api-Key"] = "AIzaSyByokKey123456"
    # Different role name in header vs stored summary
    headers["X-DB-Url"] = quote(
        "postgresql://wrong_role:supersecretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr("app.main.get_connection", lambda *args, **kwargs: _sample_conn_summary())
    monkeypatch.setattr(
        "app.pipeline.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool-123.us-east-2.aws.neon.tech",
            5432,
            "neondb",
            "wrong_role",
            "supersecretpw",
            "1.2.3.4",
            False,
        ),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "connection_mismatch"
    assert "supersecretpw" not in resp.text


def test_user_db_timeout_and_unreachable_error_mapping(monkeypatch):
    headers, _ = _make_auth(monkeypatch)
    headers["X-LLM-Api-Key"] = "AIzaSyByokKey123456"
    headers["X-DB-Url"] = quote(
        "postgresql://schemadrill_reader:supersecretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr("app.main.get_connection", lambda *args, **kwargs: _sample_conn_summary())

    # 1. Timeout -> 504 + user_db_timeouts metric
    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DatabaseTimeoutError("DB timed out", code="user_db_timeout")
        ),
    )
    with TestClient(app) as client:
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 504
        assert resp.json()["code"] == "user_db_timeout"
        assert app.state.metrics.snapshot().get("user_db_timeouts", 0) >= 1

        # 2. Unreachable -> 503 + user_db_unreachable metric
        monkeypatch.setattr(
            "app.main.run_pipeline",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                DatabaseUnreachableError("Unreachable", code="user_db_unreachable")
            ),
        )
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "user_db_unreachable"
        assert app.state.metrics.snapshot().get("user_db_unreachable", 0) >= 1


def test_executor_byte_caps():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.description = [SimpleNamespace(name="col")]

    # Cell byte cap exceeded
    mock_cursor.fetchmany.return_value = [("x" * 100,)]
    with pytest.raises(ByteCapExceeded):
        execute(mock_conn, "SELECT col FROM t", 10, max_cell_bytes=50)

    # Total result byte cap exceeded
    mock_cursor.fetchmany.return_value = [("x" * 30,), ("y" * 30,)]
    with pytest.raises(ByteCapExceeded):
        execute(mock_conn, "SELECT col FROM t", 10, max_cell_bytes=50, max_result_bytes=50)


def test_user_db_undefined_table_adds_stale_hint(monkeypatch, caplog):
    conn_summary = _sample_conn_summary()
    raw_url = SecretStr(
        "postgresql://schemadrill_reader:secretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr(
        "app.pipeline.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool-123.us-east-2.aws.neon.tech",
            5432,
            "neondb",
            "schemadrill_reader",
            "secretpw",
            "1.2.3.4",
            False,
        ),
    )

    class FakeUndefinedTableError(psycopg.errors.UndefinedTable):
        sqlstate = "42P01"

    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    monkeypatch.setattr("app.pipeline.connect_user_db", lambda *args, **kwargs: fake_conn)
    monkeypatch.setattr(
        "app.pipeline.dry_run",
        lambda conn, sql: (_ for _ in ()).throw(
            FakeUndefinedTableError('relation "users" does not exist')
        ),
    )

    settings = Settings(_env_file=None, preload_embedding_model=False, max_retries=1)
    with caplog.at_level("INFO"):
        res = run_pipeline(
            "List users",
            "public",
            settings=settings,
            retrieve=lambda q, d, k: [
                DDLBlock(
                    schema_name="public",
                    table_name="users",
                    ddl_text="CREATE TABLE public.users (id int);",
                )
            ],
            generate_sql=lambda m: SqlResponse(sql="SELECT * FROM public.users"),
            user_id="user-1",
            connection_summary=conn_summary,
            raw_db_url=raw_url,
        )

    assert res.status == "failed"
    assert res.hint_code == "schema_maybe_stale"
    assert "re-ingest" in (res.hint_message or "").lower()
    assert "secretpw" not in caplog.text


# ---------------------------------------------------------------------------
# 3. Context-Specific SQL Guard Policy
# ---------------------------------------------------------------------------


def test_sql_guard_differs_between_demo_and_user_db():
    # Demo mode denies public.users even if in allowed_tables
    demo_cfg = default_guard_config(mode="demo")
    res_demo = guard_sql("SELECT id FROM public.users", {"public.users", "users"}, demo_cfg)
    assert not res_demo.allowed
    assert res_demo.user_facing_message == AUTHORIZATION_DENIED_MESSAGE

    # Demo mode denies vault schema
    res_vault = guard_sql("SELECT * FROM vault.secrets", {"vault.secrets"}, demo_cfg)
    assert not res_vault.allowed

    # User-DB mode allows public.users when in retrieved allowed_tables
    user_cfg = default_guard_config(mode="user_db", allow_change_scripts=True)
    res_user = guard_sql("SELECT id FROM public.users", {"public.users", "users"}, user_cfg)
    assert res_user.allowed
    assert res_user.intent == "read"

    # User-DB mode hard-denies sensitive catalogs and blocked functions
    res_authid = guard_sql("SELECT * FROM pg_authid", {"pg_authid"}, user_cfg)
    assert not res_authid.allowed

    res_sleep = guard_sql("SELECT pg_sleep(5) FROM public.users", {"public.users"}, user_cfg)
    assert not res_sleep.allowed

    # User-DB mode allows small allow-list of information_schema / pg_catalog
    res_info = guard_sql(
        "SELECT table_name FROM information_schema.tables", {"public.users"}, user_cfg
    )
    assert res_info.allowed

    res_other_cat = guard_sql(
        "SELECT * FROM pg_catalog.pg_stat_activity", {"public.users"}, user_cfg
    )
    assert not res_other_cat.allowed


# ---------------------------------------------------------------------------
# 4. Change-Script Mode
# ---------------------------------------------------------------------------


def test_change_script_never_executed_and_ast_overrides_model_label(monkeypatch):
    conn_summary = _sample_conn_summary()
    raw_url = SecretStr(
        "postgresql://schemadrill_reader:secretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr(
        "app.pipeline.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool-123.us-east-2.aws.neon.tech",
            5432,
            "neondb",
            "schemadrill_reader",
            "secretpw",
            "1.2.3.4",
            False,
        ),
    )

    # Assert NO DB call happens!
    monkeypatch.setattr(
        "app.pipeline.connect_user_db",
        lambda *args, **kwargs: pytest.fail(
            "connect_user_db must NEVER be called for change scripts!"
        ),
    )
    monkeypatch.setattr(
        "app.pipeline.dry_run",
        lambda *args, **kwargs: pytest.fail("dry_run must NEVER be called for change scripts!"),
    )
    monkeypatch.setattr(
        "app.pipeline.execute",
        lambda *args, **kwargs: pytest.fail("execute must NEVER be called for change scripts!"),
    )

    from app.reliability import ReliabilityMetrics

    metrics = ReliabilityMetrics()
    settings = Settings(_env_file=None, preload_embedding_model=False)

    # Model falsely labels DELETE as intent="read" -> AST must override to "change"!
    res = run_pipeline(
        "Delete all users",
        "public",
        settings=settings,
        retrieve=lambda q, d, k: [
            DDLBlock(
                schema_name="public",
                table_name="users",
                ddl_text="CREATE TABLE public.users (id int);",
            )
        ],
        generate_sql=lambda m: SqlResponse(sql="DELETE FROM public.users", intent="read"),
        user_id="user-1",
        connection_summary=conn_summary,
        raw_db_url=raw_url,
        metrics=metrics,
    )

    assert res.status == "success"
    assert res.kind == "change_script"
    assert res.executed is False
    assert res.sql.startswith("-- AI-generated, review before running.\n")
    assert "DELETE FROM public.users" in res.sql
    assert len(res.warnings) == 1
    assert "no WHERE clause" in res.warnings[0]
    assert "Neon SQL Editor" in (res.message or "")
    assert res.execution_trace is not None
    assert res.execution_trace.dataset_kind == "user_db"
    assert res.execution_trace.intent == "change"
    assert metrics.snapshot()["change_scripts_generated"] == 1


def test_change_script_rejected_on_demo_path(monkeypatch):
    monkeypatch.setattr(
        "app.pipeline.connect_user_db",
        lambda *args, **kwargs: pytest.fail("connect_user_db must not be called"),
    )
    settings = Settings(_env_file=None, preload_embedding_model=False)
    res = run_pipeline(
        "Delete all artists",
        "chinook",
        settings=settings,
        retrieve=lambda q, d, k: [
            DDLBlock(
                schema_name="chinook",
                table_name="artist",
                ddl_text="CREATE TABLE chinook.artist (id int);",
            )
        ],
        generate_sql=lambda m: SqlResponse(sql="DELETE FROM chinook.artist", intent="change"),
    )
    assert res.status == "failed"
    assert "demo datasets" in (res.reason or "").lower()


def test_change_script_warnings_and_mixed_rejection():
    cfg = default_guard_config(mode="user_db", allow_change_scripts=True)

    # Drop + Alter drop column + Update without where
    sql = (
        "UPDATE public.users SET active = false; "
        "ALTER TABLE public.users DROP COLUMN legacy; "
        "DROP TABLE public.orders;"
    )
    res = guard_sql(sql, {"public.users", "public.orders"}, cfg)
    assert res.allowed
    assert res.intent == "change"
    assert len(res.warnings) == 3

    # Mixed read + change rejected
    mixed = "SELECT * FROM public.users; DELETE FROM public.users WHERE id = 1;"
    res_mixed = guard_sql(mixed, {"public.users"}, cfg)
    assert not res_mixed.allowed


# ---------------------------------------------------------------------------
# 5. Public vs Internal Trace
# ---------------------------------------------------------------------------


def test_trace_hides_connection_id_and_connect_ms_from_public():
    trace = InternalTrace(
        request_id="req-1",
        db="public",
        dataset_kind="user_db",
        connection_id="11111111-2222-3333-4444-555555555555",
        user_db_connect_ms=42.5,
        intent="read",
    )
    public = build_public_execution_trace(trace, finalize=True)
    dumped = public.model_dump()
    assert dumped["dataset_kind"] == "user_db"
    assert dumped["intent"] == "read"
    assert "connection_id" not in dumped
    assert "user_db_connect_ms" not in dumped


def test_user_db_concurrency_cap_and_connection_closed(monkeypatch):
    headers, _ = _make_auth(monkeypatch, sub="conc-user")
    headers["X-LLM-Api-Key"] = "AIzaSyByokKey123456"
    headers["X-DB-Url"] = quote(
        "postgresql://schemadrill_reader:secretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr("app.main.get_connection", lambda *args, **kwargs: _sample_conn_summary())

    # Simulate 2 slots already held for this user (set inside TestClient after lifespan)
    with TestClient(app) as client:
        app.state.user_db_semaphores = {"conc-user": asyncio.Semaphore(0)}
        resp = client.post(
            "/query",
            headers=headers,
            json={
                "question": "Count users",
                "db": "public",
                "connection_id": "11111111-2222-3333-4444-555555555555",
            },
        )
        assert resp.status_code == 503
        assert "concurrent queries" in resp.json()["detail"].lower()

    # Verify connection context manager __exit__ is called after read execution
    conn_summary = _sample_conn_summary()
    raw_url = SecretStr(
        "postgresql://schemadrill_reader:secretpw@ep-cool-123.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr(
        "app.pipeline.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool-123.us-east-2.aws.neon.tech",
            5432,
            "neondb",
            "schemadrill_reader",
            "secretpw",
            "1.2.3.4",
            False,
        ),
    )
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    monkeypatch.setattr("app.pipeline.connect_user_db", lambda *args, **kwargs: fake_conn)
    monkeypatch.setattr("app.pipeline.dry_run", lambda c, s: None)
    monkeypatch.setattr(
        "app.pipeline.execute",
        lambda c, s, m, **kw: pd.DataFrame([{"id": 1}]),
    )
    settings = Settings(_env_file=None, preload_embedding_model=False)
    res = run_pipeline(
        "List users",
        "public",
        settings=settings,
        retrieve=lambda q, d, k: [
            DDLBlock(
                schema_name="public",
                table_name="users",
                ddl_text="CREATE TABLE public.users (id int);",
            )
        ],
        generate_sql=lambda m: SqlResponse(sql="SELECT id FROM public.users"),
        user_id="conc-user",
        connection_summary=conn_summary,
        raw_db_url=raw_url,
    )
    assert res.status == "success"
    assert fake_conn.__exit__.call_count == 2  # once for dry_run, once for execute
