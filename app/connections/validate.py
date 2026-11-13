import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import psycopg
from pydantic import SecretStr

from app.config import Settings, get_settings


class CredentialsError(Exception):
    """Base exception for user DB connection credential issues."""

    def __init__(self, message: str, code: str = "db_credentials_rejected"):
        super().__init__(message)
        self.message = message
        self.code = code


class DatabaseUnreachableError(Exception):
    """Raised when user DB cannot be reached."""

    def __init__(self, message: str, code: str = "user_db_unreachable"):
        super().__init__(message)
        self.message = message
        self.code = code


class DatabaseTimeoutError(Exception):
    """Raised when user DB connection or query times out."""

    def __init__(self, message: str, code: str = "user_db_timeout"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class ParsedConnectionInfo:
    host: str
    port: int
    database: str
    user: str
    password: str
    resolved_ip: str
    is_pooler: bool


@dataclass(frozen=True)
class SchemaTableSummary:
    schema_name: str
    table_count: int


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _check_ip_allowed(ip_str: str, allow_private: bool = False) -> None:
    ip = ipaddress.ip_address(ip_str)
    if allow_private:
        # In test mode with ALLOW_PRIVATE_DB_HOSTS_FOR_TESTS, allow loopback / private
        return

    # Metadata: 169.254.169.254
    if ip == ipaddress.ip_address("169.254.169.254"):
        raise CredentialsError(
            "Database host resolves to cloud metadata service address",
            code="db_credentials_rejected",
        )

    # Check for private, loopback, link-local, multicast, reserved, CGNAT (100.64.0.0/10)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise CredentialsError(
            f"Database host resolves to invalid/prohibited address: {ip_str}",
            code="db_credentials_rejected",
        )
    # CGNAT: 100.64.0.0/10
    cgnat_net = ipaddress.ip_network("100.64.0.0/10")
    if ip in cgnat_net:
        raise CredentialsError(
            f"Database host resolves to carrier-grade NAT address: {ip_str}",
            code="db_credentials_rejected",
        )


def parse_and_validate_db_url(
    db_url_secret: SecretStr,
    settings: Settings | None = None,
) -> ParsedConnectionInfo:
    settings = settings or get_settings()
    raw_url = db_url_secret.get_secret_value()
    if not raw_url:
        raise CredentialsError("Missing database connection URL", code="db_credentials_required")

    try:
        parsed = urlparse(raw_url)
    except Exception as e:
        raise CredentialsError(
            "Malformed database connection URL", code="db_credentials_rejected"
        ) from e

    if parsed.scheme not in ("postgresql", "postgres"):
        raise CredentialsError(
            f"Invalid URL scheme '{parsed.scheme}'. Must be 'postgresql://' or 'postgres://'",
            code="db_credentials_rejected",
        )

    host = parsed.hostname
    if not host:
        raise CredentialsError("Missing database host in URL", code="db_credentials_rejected")

    # Reject IP literals
    if _is_ip_literal(host):
        raise CredentialsError(
            "Host must be a domain name, not an IP literal",
            code="db_credentials_rejected",
        )

    # Validate allowed host suffix (unless test override)
    allow_private = settings.allow_private_db_hosts_for_tests
    if not allow_private:
        matched = any(
            host.lower().endswith(suffix.lower()) for suffix in settings.allowed_db_host_suffixes
        )
        if not matched:
            suffixes = ", ".join(settings.allowed_db_host_suffixes)
            raise CredentialsError(
                f"Host '{host}' is not permitted. Only databases matching [{suffixes}] "
                "are allowed.",
                code="db_credentials_rejected",
            )

    # Port must be 5432 (or default)
    port = parsed.port or 5432
    if port != 5432:
        raise CredentialsError(
            f"Invalid port {port}. SchemaDrill only permits connection on port 5432.",
            code="db_credentials_rejected",
        )

    database = parsed.path.lstrip("/")
    if not database:
        raise CredentialsError("Missing database name in URL", code="db_credentials_rejected")

    user = unquote(parsed.username) if parsed.username else ""
    if not user:
        raise CredentialsError("Missing username in database URL", code="db_credentials_rejected")

    password = unquote(parsed.password) if parsed.password else ""

    # Check query params: disallow custom sslmode overrides or unknown params
    if parsed.query:
        params = parse_qs(parsed.query)
        allowed_params = {"sslmode", "channel_binding"}
        disallowed = [p for p in params if p not in allowed_params]
        if disallowed:
            raise CredentialsError(
                f"URL query parameters not permitted: {', '.join(disallowed)}",
                code="db_credentials_rejected",
            )
        if (
            "sslmode" in params
            and params["sslmode"] != ["verify-full"]
            and params["sslmode"] != ["require"]
        ):
            if not allow_private:
                raise CredentialsError(
                    "sslmode must be verify-full or omitted (verify-full is enforced)",
                    code="db_credentials_rejected",
                )

    # DNS resolution and SSRF check
    try:
        addr_infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception as e:
        raise DatabaseUnreachableError(
            f"Could not resolve host '{host}'. Please verify the hostname and availability.",
            code="user_db_unreachable",
        ) from e

    if not addr_infos:
        raise DatabaseUnreachableError(
            f"Could not resolve host '{host}' (no DNS records).",
            code="user_db_unreachable",
        )

    # Validate EVERY resolved IP (IPv4 and IPv6)
    resolved_ips = [info[4][0] for info in addr_infos]
    for ip_str in resolved_ips:
        _check_ip_allowed(ip_str, allow_private=allow_private)

    # Prefer IPv4 (AF_INET) for hostaddr pinning because serverless runtimes
    # like Cloud Run often lack outbound IPv6 routing and fail with ENETUNREACH.
    ipv4_ips = [info[4][0] for info in addr_infos if info[0] == socket.AF_INET]
    pinned_ip = ipv4_ips[0] if ipv4_ips else resolved_ips[0]
    is_pooler = "-pooler" in host.lower()

    return ParsedConnectionInfo(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        resolved_ip=pinned_ip,
        is_pooler=is_pooler,
    )


def connect_user_db(
    info: ParsedConnectionInfo,
    settings: Settings | None = None,
) -> psycopg.Connection:
    """
    Connect to user database using hostaddr pinning and SNI/verify-full.
    Includes 1 retry for Neon cold start.
    """
    settings = settings or get_settings()
    allow_private = settings.allow_private_db_hosts_for_tests

    # Re-validate DNS resolution to defend against DNS rebinding
    try:
        current_addrs = socket.getaddrinfo(
            info.host, info.port, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        for c_info in current_addrs:
            _check_ip_allowed(c_info[4][0], allow_private=allow_private)
    except Exception as e:
        if isinstance(e, CredentialsError):
            raise
        raise DatabaseUnreachableError(
            f"DNS resolution failed on connect to '{info.host}'", code="user_db_unreachable"
        ) from e

    conn_params: dict[str, Any] = {
        "host": info.host,
        "hostaddr": info.resolved_ip,
        "port": info.port,
        "dbname": info.database,
        "user": info.user,
        "password": info.password,
        "connect_timeout": int(settings.user_db_connect_timeout_seconds),
        "application_name": "schemadrill",
    }

    if not allow_private:
        conn_params["sslmode"] = "verify-full"
        conn_params["sslrootcert"] = "system"
    else:
        conn_params["sslmode"] = "prefer"

    # Prepare threshold for pooler
    if info.is_pooler:
        conn_params["prepare_threshold"] = None

    last_error: Exception | None = None
    for attempt in range(2):  # 1 initial + 1 retry for Neon cold start
        try:
            conn = psycopg.connect(**conn_params, autocommit=False)
            # Configure statement timeouts inside transaction / session
            statement_timeout_ms = settings.user_db_statement_timeout_ms
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {statement_timeout_ms}")
                cur.execute(
                    f"SET LOCAL idle_in_transaction_session_timeout = {statement_timeout_ms}"
                )
            return conn
        except psycopg.OperationalError as e:
            last_error = e
            if attempt == 0:
                time.sleep(1.0)
                continue
            break
        except Exception as e:
            last_error = e
            break

    err_str = str(last_error) if last_error else ""
    if "timeout" in err_str.lower():
        raise DatabaseTimeoutError(
            "Connection to user database timed out. If your Neon endpoint was suspended "
            "or behind IP Allow rules, verify its status.",
            code="user_db_timeout",
        )
    if "password authentication failed" in err_str.lower() or "role" in err_str.lower():
        raise CredentialsError(
            "Authentication failed for user database credentials. Verify username, password, "
            "and database name.",
            code="db_credentials_rejected",
        )
    raise DatabaseUnreachableError(
        "Unable to reach user database. Check if your Neon project is active or if "
        f"IP Allow rules restrict connections. (Error: {err_str[:100]})",
        code="user_db_unreachable",
    )


_READONLY_SETUP_SQL_TEMPLATE = """-- Run this in your Neon SQL Editor:
CREATE ROLE {role_name} WITH LOGIN PASSWORD 'YOUR_SECRET_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE {database} TO {role_name};
GRANT USAGE ON SCHEMA {schema} TO {role_name};
GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role_name};
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {role_name};
"""


def verify_readonly_role(
    conn: psycopg.Connection,
    role_name: str,
    target_schemas: list[str],
    database: str = "neondb",
) -> None:
    """
    Verify the connected role is strictly READ-ONLY.
    Rejects if:
    - Superuser (rolsuper = true)
    - Member of neon_superuser or pg_write_all_data
    - Has CREATEROLE or CREATEDB
    - Owns any table in the inspected schemas
    - Has INSERT, UPDATE, DELETE, or TRUNCATE on any inspected table
    """
    with conn.cursor() as cur:
        # 1. Check role attributes in pg_roles
        cur.execute(
            """
            SELECT rolsuper, rolcreaterole, rolcreatedb
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
        row = cur.fetchone()
        if not row:
            raise CredentialsError(
                f"Role '{role_name}' not found in pg_roles", code="db_credentials_rejected"
            )

        rolsuper, rolcreaterole, rolcreatedb = row
        violations = []
        if rolsuper:
            violations.append("Role is a SUPERUSER")
        if rolcreaterole:
            violations.append("Role has CREATEROLE privilege")
        if rolcreatedb:
            violations.append("Role has CREATEDB privilege")

        # 2. Check membership in dangerous roles
        cur.execute(
            """
            SELECT r.rolname
            FROM pg_roles r
            JOIN pg_auth_members m ON r.oid = m.roleid
            JOIN pg_roles u ON u.oid = m.member
            WHERE u.rolname = current_user
              AND r.rolname IN ('neon_superuser', 'pg_write_all_data', 'pg_database_owner')
            """
        )
        membership_rows = cur.fetchall()
        for (m_role,) in membership_rows:
            violations.append(f"Role is a member of '{m_role}'")

        # 3. Check table ownership in target schemas
        if target_schemas:
            placeholders = ",".join(["%s"] * len(target_schemas))
            cur.execute(
                f"""
                SELECT c.relname, n.nspname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_roles r ON r.oid = c.relowner
                WHERE n.nspname IN ({placeholders})
                  AND c.relkind IN ('r', 'v', 'm', 'p')
                  AND r.rolname = current_user
                LIMIT 5
                """,
                tuple(target_schemas),
            )
            owned_tables = cur.fetchall()
            if owned_tables:
                table_names = [f"{schema}.{tbl}" for tbl, schema in owned_tables]
                violations.append(
                    f"Role owns tables: {', '.join(table_names)} (owners retain write rights)"
                )

            # 4. Check write privileges (INSERT, UPDATE, DELETE, TRUNCATE) on tables
            cur.execute(
                f"""
                SELECT c.relname, n.nspname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname IN ({placeholders})
                  AND c.relkind IN ('r', 'p')
                  AND (
                    has_table_privilege(current_user, c.oid, 'INSERT')
                    OR has_table_privilege(current_user, c.oid, 'UPDATE')
                    OR has_table_privilege(current_user, c.oid, 'DELETE')
                    OR has_table_privilege(current_user, c.oid, 'TRUNCATE')
                  )
                LIMIT 5
                """,
                tuple(target_schemas),
            )
            write_tables = cur.fetchall()
            if write_tables:
                table_names = [f"{schema}.{tbl}" for tbl, schema in write_tables]
                violations.append(f"Role has write privileges on tables: {', '.join(table_names)}")

        if violations:
            schema_hint = target_schemas[0] if target_schemas else "public"
            setup_snippet = _READONLY_SETUP_SQL_TEMPLATE.format(
                role_name="schemadrill_reader",
                database=database,
                schema=schema_hint,
            )
            detail = (
                f"Role '{role_name}' is not read-only: {'; '.join(violations)}.\n\n"
                "To safely use SchemaDrill, create a read-only role using the Neon SQL Editor:\n\n"
                f"{setup_snippet}"
            )
            raise CredentialsError(detail, code="db_credentials_rejected")


def inspect_user_database(conn: psycopg.Connection) -> list[SchemaTableSummary]:
    """
    Query selectable schemas and their table counts for current_user.
    Filters out system schemas (pg_catalog, information_schema, pg_toast, etc.).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.nspname AS schema_name,
                   count(c.oid)::int AS table_count
            FROM pg_namespace n
            LEFT JOIN pg_class c
              ON c.relnamespace = n.oid
             AND c.relkind IN ('r', 'v', 'm', 'p')
             AND has_table_privilege(current_user, c.oid, 'SELECT')
            WHERE n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
              AND has_schema_privilege(current_user, n.oid, 'USAGE')
            GROUP BY n.nspname
            ORDER BY n.nspname
            """
        )
        rows = cur.fetchall()
        return [SchemaTableSummary(schema_name=r[0], table_count=r[1]) for r in rows]
