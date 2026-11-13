import time
from uuid import UUID

import psycopg
from pgvector.psycopg import register_vector

from app.config import Settings, get_settings
from app.connections.validate import DatabaseTimeoutError
from app.embedding import embed_passage
from app.schema import table_ddl

MAX_DDL_SIZE_BYTES = 32 * 1024  # 32 KB per table DDL cap


def ingest_user_schemas(
    admin_conn: psycopg.Connection,
    user_conn: psycopg.Connection,
    connection_id: UUID | str,
    user_id: str,
    schemas: list[str],
    settings: Settings | None = None,
) -> int:
    """
    Ingest table DDL and compute embeddings for user-selected schemas.
    Runs with a hard deadline (INGEST_DEADLINE_SECONDS).
    Caps total tables at MAX_TABLES_PER_CONNECTION.
    Inserts into tenant_schema_embeddings via admin_conn.
    Returns total tables ingested.
    """
    settings = settings or get_settings()
    deadline = time.monotonic() + settings.ingest_deadline_seconds
    max_tables = settings.max_tables_per_connection

    register_vector(admin_conn)

    total_tables_ingested = 0

    # Query all readable tables across target schemas from user_conn
    with user_conn.cursor() as user_cur:
        for schema_name in schemas:
            if time.monotonic() > deadline:
                raise DatabaseTimeoutError(
                    f"Schema ingestion timed out after {settings.ingest_deadline_seconds} seconds.",
                    code="user_db_timeout",
                )

            user_cur.execute(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND c.relkind IN ('r', 'p')
                  AND has_table_privilege(current_user, c.oid, 'SELECT')
                ORDER BY c.relname
                """,
                (schema_name,),
            )
            tables = [r[0] for r in user_cur.fetchall()]

            for table_name in tables:
                if total_tables_ingested >= max_tables:
                    raise ValueError(
                        f"Schema exceeds maximum table limit ({max_tables} tables per connection)."
                    )

                if time.monotonic() > deadline:
                    raise DatabaseTimeoutError(
                        f"Schema ingestion timed out after {settings.ingest_deadline_seconds}s.",
                        code="user_db_timeout",
                    )

                # Generate DDL using user_conn
                ddl = table_ddl(user_conn, schema_name, table_name)
                if len(ddl.encode("utf-8")) > MAX_DDL_SIZE_BYTES:
                    # Truncate DDL if excessively large to avoid abuse
                    ddl = ddl[:MAX_DDL_SIZE_BYTES]

                # Generate embedding
                embedding = embed_passage(ddl)

                # Store embedding in admin_conn
                with admin_conn.cursor() as admin_cur:
                    admin_cur.execute(
                        """
                        INSERT INTO public.tenant_schema_embeddings
                          (connection_id, user_id, schema_name, table_name, ddl, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (connection_id, schema_name, table_name)
                        DO UPDATE SET
                          ddl = EXCLUDED.ddl,
                          embedding = EXCLUDED.embedding
                        """,
                        (str(connection_id), user_id, schema_name, table_name, ddl, embedding),
                    )

                total_tables_ingested += 1

    return total_tables_ingested
