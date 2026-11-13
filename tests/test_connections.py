from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import _jwks_client
from app.config import (
    Settings,
    get_settings,
    reset_active_db_url,
    set_active_db_url,
)
from app.connections import (
    ConnectionLimitError,
    ConnectionMismatchError,
    ConnectionNotFoundError,
    CredentialsError,
    ParsedConnectionInfo,
    TenantConnectionSummary,
    parse_and_validate_db_url,
    verify_readonly_role,
)
from app.main import app
from app.reliability import sanitize_log_detail


def _make_auth(monkeypatch, *, sub: str = "test-user-1", settings: Settings | None = None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    effective_settings = settings or Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
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
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Credential redaction & sanitize_log_detail
# ---------------------------------------------------------------------------


def test_sanitize_log_detail_redacts_credentials():
    secret_url = "postgresql://myuser:supersecretpass@ep-cool-123.us-east-2.aws.neon.tech/neondb?sslmode=verify-full"
    token = set_active_db_url(SecretStr(secret_url))
    try:
        raw_message = f"Failed to connect to {secret_url} with password=supersecretpass"
        sanitized = sanitize_log_detail(raw_message)
        assert "supersecretpass" not in sanitized
        assert "postgresql://" not in sanitized
        assert "[REDACTED]" in sanitized

        # Also test X-DB-Url header line
        header_line = f"X-DB-Url: {quote(secret_url)}"
        sanitized_header = sanitize_log_detail(header_line)
        assert "supersecretpass" not in sanitized_header
        assert "[REDACTED]" in sanitized_header
    finally:
        reset_active_db_url(token)


# ---------------------------------------------------------------------------
# 2. SSRF & URI Validation Matrix
# ---------------------------------------------------------------------------


def test_parse_and_validate_db_url_valid(monkeypatch):
    valid_url = "postgresql://schemadrill_reader:secret123@ep-fragrant-pond-123456.us-east-2.aws.neon.tech/neondb"
    # Mock DNS resolution to public IP
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("54.200.10.20", 5432))],
    )
    info = parse_and_validate_db_url(SecretStr(valid_url))
    assert info.host == "ep-fragrant-pond-123456.us-east-2.aws.neon.tech"
    assert info.port == 5432
    assert info.database == "neondb"
    assert info.user == "schemadrill_reader"
    assert info.password == "secret123"
    assert info.resolved_ip == "54.200.10.20"
    assert info.is_pooler is False


def test_parse_and_validate_db_url_pooler_host(monkeypatch):
    pooler_url = (
        "postgresql://reader:p@ep-fragrant-pond-123456-pooler.us-east-2.aws.neon.tech/neondb"
    )
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("54.200.10.20", 5432))],
    )
    info = parse_and_validate_db_url(SecretStr(pooler_url))
    assert info.is_pooler is True


def test_parse_and_validate_db_url_rejects_disallowed_suffix():
    bad_url = "postgresql://user:pass@evil.com/db"
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(bad_url))
    assert "Host 'evil.com' is not permitted" in str(exc_info.value)
    assert exc_info.value.code == "db_credentials_rejected"


def test_parse_and_validate_db_url_rejects_ip_literal():
    ip_url = "postgresql://user:pass@127.0.0.1:5432/db"
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(ip_url))
    assert "Host must be a domain name, not an IP literal" in str(exc_info.value)


def test_parse_and_validate_db_url_rejects_non_5432_port():
    bad_port = "postgresql://user:pass@ep-cool.us-east-2.aws.neon.tech:5433/db"
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(bad_port))
    assert "Invalid port 5433" in str(exc_info.value)


def test_parse_and_validate_db_url_rejects_unknown_query_params():
    bad_query = (
        "postgresql://user:pass@ep-cool.us-east-2.aws.neon.tech/db?options=-c%20search_path=evil"
    )
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(bad_query))
    assert "URL query parameters not permitted" in str(exc_info.value)


def test_parse_and_validate_db_url_rejects_private_dns(monkeypatch):
    url = "postgresql://user:pass@ep-cool.us-east-2.aws.neon.tech/db"
    # Resolves to loopback 127.0.0.1
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 5432))],
    )
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(url))
    assert "Database host resolves to invalid/prohibited address: 127.0.0.1" in str(exc_info.value)


def test_parse_and_validate_db_url_rejects_metadata_dns(monkeypatch):
    url = "postgresql://user:pass@ep-cool.us-east-2.aws.neon.tech/db"
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("169.254.169.254", 5432))],
    )
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(url))
    assert "cloud metadata service" in str(exc_info.value)


def test_parse_and_validate_db_url_rejects_cgnat_dns(monkeypatch):
    url = "postgresql://user:pass@ep-cool.us-east-2.aws.neon.tech/db"
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("100.64.0.1", 5432))],
    )
    with pytest.raises(CredentialsError) as exc_info:
        parse_and_validate_db_url(SecretStr(url))
    assert "carrier-grade NAT address" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. Read-Only Privilege Checks
# ---------------------------------------------------------------------------


def test_verify_readonly_role_accepts_clean_role():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    # 1. rolsuper=False, rolcreaterole=False, rolcreatedb=False
    # 2. membership=[]
    # 3. owned_tables=[]
    # 4. write_tables=[]
    mock_cursor.fetchone.return_value = (False, False, False)
    mock_cursor.fetchall.side_effect = [[], [], []]

    # Should succeed without exception
    verify_readonly_role(mock_conn, "test_role", ["public"], "neondb")


def test_verify_readonly_role_rejects_superuser():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (True, False, False)  # superuser!
    mock_cursor.fetchall.return_value = []

    with pytest.raises(CredentialsError) as exc_info:
        verify_readonly_role(mock_conn, "admin_user", ["public"], "neondb")
    assert "Role is a SUPERUSER" in str(exc_info.value)
    assert "CREATE ROLE" in str(exc_info.value)  # setup snippet included


def test_verify_readonly_role_rejects_neon_superuser():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (False, False, False)
    # membership includes neon_superuser
    mock_cursor.fetchall.side_effect = [[("neon_superuser",)], [], []]

    with pytest.raises(CredentialsError) as exc_info:
        verify_readonly_role(mock_conn, "member_user", ["public"], "neondb")
    assert "member of 'neon_superuser'" in str(exc_info.value)


def test_verify_readonly_role_rejects_table_ownership():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (False, False, False)
    # membership: empty, owned_tables: [('albums', 'public')]
    mock_cursor.fetchall.side_effect = [[], [("albums", "public")], []]

    with pytest.raises(CredentialsError) as exc_info:
        verify_readonly_role(mock_conn, "owner_user", ["public"], "neondb")
    assert "Role owns tables" in str(exc_info.value)


def test_verify_readonly_role_rejects_write_privilege():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_cursor.fetchone.return_value = (False, False, False)
    # membership: empty, owned: empty, write_tables: [('invoices', 'public')]
    mock_cursor.fetchall.side_effect = [[], [], [("invoices", "public")]]

    with pytest.raises(CredentialsError) as exc_info:
        verify_readonly_role(mock_conn, "writer_user", ["public"], "neondb")
    assert "Role has write privileges" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. API Endpoints: Inspect, Create, List, Reingest, Delete
# ---------------------------------------------------------------------------


def test_inspect_endpoint_requires_header(monkeypatch):
    headers = _make_auth(monkeypatch)
    with TestClient(app) as client:
        resp = client.post("/api/connections/inspect", headers=headers)
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "db_credentials_required"


def test_inspect_endpoint_success(monkeypatch):
    headers = _make_auth(monkeypatch)
    secret_url = (
        "postgresql://schemadrill_reader:pass@ep-cool-123456.us-east-2.aws.neon.tech/neondb"
    )
    headers["X-DB-Url"] = quote(secret_url)

    # Mock validate and inspect_connection
    monkeypatch.setattr(
        "app.main.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo("h", 5432, "d", "u", "p", "1.2.3.4", False),
    )
    monkeypatch.setattr(
        "app.main.inspect_connection",
        lambda *args, **kwargs: [
            SimpleNamespace(schema_name="public", table_count=12),
            SimpleNamespace(schema_name="analytics", table_count=5),
        ],
    )

    with TestClient(app) as client:
        resp = client.post("/api/connections/inspect", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["schemas"]) == 2
        assert data["schemas"][0]["schema_name"] == "public"
        assert data["schemas"][0]["table_count"] == 12


def test_create_connection_success(monkeypatch):
    headers = _make_auth(monkeypatch, sub="user-create-test")
    secret_url = (
        "postgresql://schemadrill_reader:pass@ep-cool-123456.us-east-2.aws.neon.tech/neondb"
    )
    headers["X-DB-Url"] = quote(secret_url)

    monkeypatch.setattr(
        "app.main.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool.neon.tech", 5432, "neondb", "reader", "p", "1.2.3.4", False
        ),
    )
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    mock_summary = TenantConnectionSummary(
        id="11111111-2222-3333-4444-555555555555",
        label="Production Read Replica",
        host="ep-cool.neon.tech",
        database="neondb",
        role_name="reader",
        schemas=["public"],
        ingest_status="ready",
        table_count=15,
        ingested_at=now,
        expires_at=now + datetime.timedelta(hours=24),
        created_at=now,
        is_expired=False,
        needs_credentials=True,
    )
    monkeypatch.setattr(
        "app.main.create_and_ingest_connection", lambda *args, **kwargs: mock_summary
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/connections",
            headers=headers,
            json={"label": "Production Read Replica", "schemas": ["public"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "11111111-2222-3333-4444-555555555555"
        assert data["table_count"] == 15
        assert data["ingest_status"] == "ready"
        assert data["needs_credentials"] is True


def test_create_connection_limit_exceeded(monkeypatch):
    headers = _make_auth(monkeypatch, sub="user-limit-test")
    headers["X-DB-Url"] = quote("postgresql://reader:p@ep-cool.neon.tech/db")

    monkeypatch.setattr(
        "app.main.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-cool.neon.tech", 5432, "db", "reader", "p", "1.2.3.4", False
        ),
    )

    def _raise_limit(*args, **kwargs):
        raise ConnectionLimitError("Maximum limit of 3 connections reached.")

    monkeypatch.setattr("app.main.create_and_ingest_connection", _raise_limit)

    with TestClient(app) as client:
        resp = client.post(
            "/api/connections",
            headers=headers,
            json={"label": "Fourth connection", "schemas": ["public"]},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "connection_limit"


def test_list_connections(monkeypatch):
    headers = _make_auth(monkeypatch, sub="user-list-test")
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    mock_summary = TenantConnectionSummary(
        id="conn-1",
        label="Test DB",
        host="ep-cool.neon.tech",
        database="neondb",
        role_name="reader",
        schemas=["public"],
        ingest_status="ready",
        table_count=5,
        ingested_at=now,
        expires_at=now + datetime.timedelta(hours=24),
        created_at=now,
        is_expired=False,
        needs_credentials=True,
    )
    monkeypatch.setattr("app.main.list_connections", lambda *args, **kwargs: [mock_summary])

    with TestClient(app) as client:
        resp = client.get("/api/connections", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["label"] == "Test DB"
        assert data[0]["table_count"] == 5


def test_reingest_connection_mismatch(monkeypatch):
    headers = _make_auth(monkeypatch, sub="user-reingest-test")
    headers["X-DB-Url"] = quote("postgresql://different_reader:p@ep-diff.neon.tech/neondb")

    monkeypatch.setattr(
        "app.main.parse_and_validate_db_url",
        lambda *args, **kwargs: ParsedConnectionInfo(
            "ep-diff.neon.tech", 5432, "neondb", "different_reader", "p", "1.2.3.4", False
        ),
    )

    def _raise_mismatch(*args, **kwargs):
        raise ConnectionMismatchError(
            "Provided credentials do not match stored connection host, database, or role."
        )

    monkeypatch.setattr("app.main.reingest_connection", _raise_mismatch)

    with TestClient(app) as client:
        resp = client.post(
            "/api/connections/11111111-2222-3333-4444-555555555555/reingest", headers=headers
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "connection_mismatch"


def test_delete_connection_tenant_isolation(monkeypatch):
    headers = _make_auth(monkeypatch, sub="user-a")

    def _mock_delete(conn_id, user_id, settings):
        raise ConnectionNotFoundError("Connection not found")

    monkeypatch.setattr("app.main.delete_connection", _mock_delete)

    # If user-a tries to delete user-b's connection, raises ConnectionNotFoundError
    with TestClient(app) as client:
        resp = client.delete("/api/connections/non-existent-or-other-user-conn", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# 5. Permission test: schemadrill_readonly denied on tenant tables
# ---------------------------------------------------------------------------


def test_schemadrill_readonly_denied_on_tenant_connections():
    settings = get_settings()
    try:
        with psycopg.connect(settings.database_url, connect_timeout=1) as conn:
            with pytest.raises(psycopg.ProgrammingError) as exc_info:
                conn.execute("SELECT * FROM public.tenant_connections LIMIT 1")
            assert "permission denied" in str(exc_info.value).lower()

            with pytest.raises(psycopg.ProgrammingError) as exc_info2:
                conn.execute("SELECT * FROM public.tenant_schema_embeddings LIMIT 1")
            assert "permission denied" in str(exc_info2.value).lower()
    except psycopg.OperationalError:
        # In CI/environments without live local postgres, verify the SQL definition revokes access
        from pathlib import Path

        sql_text = (
            Path(__file__).resolve().parent.parent / "scripts" / "tenant_connections.sql"
        ).read_text()
        assert (
            "REVOKE ALL ON public.tenant_connections, public.tenant_schema_embeddings" in sql_text
        )
        assert "FROM schemadrill_readonly" in sql_text
