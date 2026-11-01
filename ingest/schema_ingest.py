import argparse

import psycopg
from pgvector.psycopg import register_vector

from app.config import get_settings
from app.embedding import embed_passage
from app.schema import table_ddl


def ingest_schema(schema_name: str) -> None:
    settings = get_settings()
    with psycopg.connect(settings.ingest_database_url, autocommit=True) as connection:
        register_vector(connection)
        tables = connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema_name,),
        ).fetchall()
        with connection.cursor() as cursor:
            for (table_name,) in tables:
                ddl_text = table_ddl(connection, schema_name, table_name)
                cursor.execute(
                    """
                    INSERT INTO schema_embeddings (schema_name, table_name, ddl_text, embedding)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (schema_name, table_name)
                    DO UPDATE SET ddl_text = EXCLUDED.ddl_text, embedding = EXCLUDED.embedding
                    """,
                    (schema_name, table_name, ddl_text, embed_passage(ddl_text)),
                )
        connection.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed database table DDL into pgvector")
    parser.add_argument("schema", help="Postgres schema to ingest")
    ingest_schema(parser.parse_args().schema)
