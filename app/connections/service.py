import datetime
import logging
from dataclasses import dataclass

import psycopg

from app.config import Settings, get_settings
from app.connections.ingest import ingest_user_schemas
from app.connections.validate import (
    CredentialsError,
    ParsedConnectionInfo,
    SchemaTableSummary,
    connect_user_db,
    inspect_user_database,
    verify_readonly_role,
)

logger = logging.getLogger(__name__)


class ConnectionLimitError(Exception):
    def __init__(self, message: str = "Maximum number of connections reached"):
        super().__init__(message)
        self.code = "connection_limit"
        self.message = message


class ConnectionNotFoundError(Exception):
    def __init__(self, message: str = "Connection not found"):
        super().__init__(message)
        self.code = "not_found"
        self.message = message


class ConnectionMismatchError(Exception):
    def __init__(
        self, message: str = "Provided credentials do not match stored connection parameters"
    ):
        super().__init__(message)
        self.code = "connection_mismatch"
        self.message = message


@dataclass(frozen=True)
class TenantConnectionSummary:
    id: str
    label: str
    host: str
    database: str
    role_name: str
    schemas: list[str]
    ingest_status: str
    table_count: int
    ingested_at: datetime.datetime | None
    expires_at: datetime.datetime
    created_at: datetime.datetime
    is_expired: bool
    needs_credentials: bool = True


def reap_expired_and_stale_connections(settings: Settings | None = None) -> None:
    """
    On startup and opportunistically before mutations:
    - Delete expired connections (expires_at < now()). Tenant_schema_embeddings cascade.
    - Mark stale 'running' rows older than 5 minutes as 'failed'.
    """
    settings = settings or get_settings()
    try:
        with psycopg.connect(settings.ingest_database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                # 1. Delete expired
                cur.execute(
                    """
                    DELETE FROM public.tenant_connections
                    WHERE expires_at < now()
                    """
                )
                deleted = cur.rowcount

                # 2. Mark stale running as failed
                cur.execute(
                    """
                    UPDATE public.tenant_connections
                    SET ingest_status = 'failed'
                    WHERE ingest_status = 'running'
                      AND created_at < now() - interval '5 minutes'
                    """
                )
                updated = cur.rowcount
                if deleted > 0 or updated > 0:
                    logger.info(
                        "Reaped connections: deleted %d expired, marked %d stale running as failed",
                        deleted,
                        updated,
                    )
    except Exception as e:
        logger.warning("Reaper encountered error: %s", e)


def inspect_connection(
    info: ParsedConnectionInfo,
    settings: Settings | None = None,
) -> list[SchemaTableSummary]:
    """
    Connect to user DB, verify read-only, and inspect available schemas.
    Stores nothing.
    """
    settings = settings or get_settings()
    with connect_user_db(info, settings=settings) as user_conn:
        verify_readonly_role(user_conn, info.user, [], database=info.database)
        return inspect_user_database(user_conn)


def create_and_ingest_connection(
    user_id: str,
    label: str,
    schemas: list[str],
    info: ParsedConnectionInfo,
    settings: Settings | None = None,
) -> TenantConnectionSummary:
    settings = settings or get_settings()
    reap_expired_and_stale_connections(settings)

    if not schemas:
        raise CredentialsError(
            "At least one schema must be selected for ingestion", code="db_credentials_rejected"
        )

    # Check connection count limit
    with psycopg.connect(settings.ingest_database_url) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)::int
                FROM public.tenant_connections
                WHERE user_id = %s AND expires_at > now()
                """,
                (user_id,),
            )
            count = cur.fetchone()[0]
            if count >= settings.max_connections_per_user:
                raise ConnectionLimitError(
                    f"Maximum limit of {settings.max_connections_per_user} connections reached. "
                    "Delete an existing connection to add a new one."
                )

            # Insert initial connection row in 'running' status
            expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                hours=settings.connection_ttl_hours
            )
            cur.execute(
                """
                INSERT INTO public.tenant_connections (
                  user_id, label, host, database, role_name, schemas,
                  ingest_status, table_count, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'running', 0, %s)
                RETURNING id, created_at
                """,
                (user_id, label, info.host, info.database, info.user, schemas, expires_at),
            )
            row = cur.fetchone()
            conn_id = row[0]
            created_at = row[1]
        admin_conn.commit()

    # Now perform ingestion
    try:
        with connect_user_db(info, settings=settings) as user_conn:
            verify_readonly_role(user_conn, info.user, schemas, database=info.database)
            with psycopg.connect(settings.ingest_database_url) as admin_conn:
                table_count = ingest_user_schemas(
                    admin_conn=admin_conn,
                    user_conn=user_conn,
                    connection_id=conn_id,
                    user_id=user_id,
                    schemas=schemas,
                    settings=settings,
                )
                now = datetime.datetime.now(datetime.UTC)
                with admin_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.tenant_connections
                        SET ingest_status = 'ready',
                            table_count = %s,
                            ingested_at = %s
                        WHERE id = %s
                        """,
                        (table_count, now, conn_id),
                    )
                admin_conn.commit()

        return TenantConnectionSummary(
            id=str(conn_id),
            label=label,
            host=info.host,
            database=info.database,
            role_name=info.user,
            schemas=schemas,
            ingest_status="ready",
            table_count=table_count,
            ingested_at=now,
            expires_at=expires_at,
            created_at=created_at,
            is_expired=False,
            needs_credentials=True,
        )
    except Exception:
        # Mark as failed and delete partial embeddings
        with psycopg.connect(settings.ingest_database_url) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.tenant_connections
                    SET ingest_status = 'failed'
                    WHERE id = %s
                    """,
                    (conn_id,),
                )
                cur.execute(
                    """
                    DELETE FROM public.tenant_schema_embeddings
                    WHERE connection_id = %s
                    """,
                    (conn_id,),
                )
            admin_conn.commit()
        raise


def list_connections(
    user_id: str, settings: Settings | None = None
) -> list[TenantConnectionSummary]:
    settings = settings or get_settings()
    reap_expired_and_stale_connections(settings)
    now = datetime.datetime.now(datetime.UTC)

    with psycopg.connect(settings.ingest_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, host, database, role_name, schemas, ingest_status,
                       table_count, ingested_at, expires_at, created_at
                FROM public.tenant_connections
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()

    return [
        TenantConnectionSummary(
            id=str(r[0]),
            label=r[1],
            host=r[2],
            database=r[3],
            role_name=r[4],
            schemas=r[5],
            ingest_status=r[6],
            table_count=r[7],
            ingested_at=r[8],
            expires_at=r[9],
            created_at=r[10],
            is_expired=r[9] <= now,
            needs_credentials=True,
        )
        for r in rows
    ]


def get_connection(
    connection_id: str,
    user_id: str,
    settings: Settings | None = None,
) -> TenantConnectionSummary:
    """Load a connection by (id, user_id) without reaping, else raise ConnectionNotFoundError."""
    from uuid import UUID

    settings = settings or get_settings()
    try:
        UUID(connection_id)
    except ValueError as err:
        raise ConnectionNotFoundError("Connection not found") from err

    now = datetime.datetime.now(datetime.UTC)
    with psycopg.connect(settings.ingest_database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, host, database, role_name, schemas, ingest_status,
                       table_count, ingested_at, expires_at, created_at
                FROM public.tenant_connections
                WHERE id = %s AND user_id = %s
                """,
                (connection_id, user_id),
            )
            r = cur.fetchone()

    if r is None:
        raise ConnectionNotFoundError("Connection not found")

    return TenantConnectionSummary(
        id=str(r[0]),
        label=r[1],
        host=r[2],
        database=r[3],
        role_name=r[4],
        schemas=r[5],
        ingest_status=r[6],
        table_count=r[7],
        ingested_at=r[8],
        expires_at=r[9],
        created_at=r[10],
        is_expired=r[9] <= now,
        needs_credentials=True,
    )


def reingest_connection(
    connection_id: str,
    user_id: str,
    info: ParsedConnectionInfo,
    settings: Settings | None = None,
) -> TenantConnectionSummary:
    settings = settings or get_settings()
    reap_expired_and_stale_connections(settings)

    # 1. Look up existing connection for this user_id
    with psycopg.connect(settings.ingest_database_url) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label, host, database, role_name, schemas, expires_at, created_at
                FROM public.tenant_connections
                WHERE id = %s AND user_id = %s
                """,
                (connection_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise ConnectionNotFoundError("Connection not found")

            (
                conn_uuid,
                label,
                stored_host,
                stored_db,
                stored_role,
                schemas,
                expires_at,
                created_at,
            ) = row

            # Verify host/database/role match stored row
            if (
                stored_host.lower() != info.host.lower()
                or stored_db != info.database
                or stored_role != info.user
            ):
                raise ConnectionMismatchError(
                    "Provided credentials do not match stored connection host, database, or role."
                )

            # Mark status as running
            cur.execute(
                """
                UPDATE public.tenant_connections
                SET ingest_status = 'running'
                WHERE id = %s
                """,
                (conn_uuid,),
            )
        admin_conn.commit()

    # Ingest and atomically replace embeddings
    try:
        with connect_user_db(info, settings=settings) as user_conn:
            verify_readonly_role(user_conn, info.user, schemas, database=info.database)
            with psycopg.connect(settings.ingest_database_url) as admin_conn:
                # Delete existing embeddings in a transaction before inserting new ones
                with admin_conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM public.tenant_schema_embeddings
                        WHERE connection_id = %s
                        """,
                        (conn_uuid,),
                    )
                table_count = ingest_user_schemas(
                    admin_conn=admin_conn,
                    user_conn=user_conn,
                    connection_id=conn_uuid,
                    user_id=user_id,
                    schemas=schemas,
                    settings=settings,
                )
                now = datetime.datetime.now(datetime.UTC)
                new_expires_at = now + datetime.timedelta(hours=settings.connection_ttl_hours)
                with admin_conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE public.tenant_connections
                        SET ingest_status = 'ready',
                            table_count = %s,
                            ingested_at = %s,
                            expires_at = %s
                        WHERE id = %s
                        """,
                        (table_count, now, new_expires_at, conn_uuid),
                    )
                admin_conn.commit()

        return TenantConnectionSummary(
            id=str(conn_uuid),
            label=label,
            host=stored_host,
            database=stored_db,
            role_name=stored_role,
            schemas=schemas,
            ingest_status="ready",
            table_count=table_count,
            ingested_at=now,
            expires_at=new_expires_at,
            created_at=created_at,
            is_expired=False,
            needs_credentials=True,
        )
    except Exception:
        with psycopg.connect(settings.ingest_database_url) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.tenant_connections
                    SET ingest_status = 'failed'
                    WHERE id = %s
                    """,
                    (conn_uuid,),
                )
            admin_conn.commit()
        raise


def delete_connection(
    connection_id: str,
    user_id: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    with psycopg.connect(settings.ingest_database_url) as admin_conn:
        with admin_conn.cursor() as cur:
            # Delete connection row. Embeddings cascade via foreign key constraint.
            cur.execute(
                """
                DELETE FROM public.tenant_connections
                WHERE id = %s AND user_id = %s
                RETURNING id
                """,
                (connection_id, user_id),
            )
            deleted = cur.fetchone() is not None
        admin_conn.commit()

    if not deleted:
        raise ConnectionNotFoundError("Connection not found")
    return True
