import argparse

import psycopg
from pgvector.psycopg import register_vector

from app.config import get_settings
from app.embedding import embed_passage


def table_ddl(connection: psycopg.Connection, schema_name: str, table_name: str) -> str:
    columns = connection.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema_name, table_name),
    ).fetchall()
    foreign_keys = connection.execute(
        """
        SELECT kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s AND tc.table_name = %s
        """,
        (schema_name, table_name),
    ).fetchall()
    lines = [f"CREATE TABLE {schema_name}.{table_name} ("]
    lines.extend(
        f"  {name} {data_type}{' NOT NULL' if nullable == 'NO' else ''},"
        for name, data_type, nullable in columns
    )
    lines.extend(
        f"  FOREIGN KEY ({column}) REFERENCES {target_table}({target_column}),"
        for column, target_table, target_column in foreign_keys
    )
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append(");")
    return "\n".join(lines)


def ingest_schema(schema_name: str) -> None:
    settings = get_settings()
    with psycopg.connect(settings.ingest_database_url) as connection:
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
