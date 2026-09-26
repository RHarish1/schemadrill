import json
import os
import time

import psycopg
from pgvector.psycopg import register_vector

BENCHMARK_SCHEMA = "ci_benchmark"
VECTOR_SIZE = 384
ITERATIONS = 25


def run() -> dict[str, object]:
    database_url = os.environ.get(
        "INGEST_DATABASE_URL",
        "postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill",
    )
    with psycopg.connect(database_url) as connection:
        register_vector(connection)
        extension = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        table_exists = connection.execute(
            "SELECT to_regclass('public.schema_embeddings') IS NOT NULL"
        ).fetchone()[0]
        hnsw_exists = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname = 'schema_embeddings_embedding_hnsw'
            )
            """
        ).fetchone()[0]
        chinook_tables = connection.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'chinook' AND table_type = 'BASE TABLE'
            """
        ).fetchone()[0]
        default_database = connection.execute("SELECT current_database()").fetchone()[0]

        vector = [0.0] * VECTOR_SIZE
        vector[0] = 1.0
        connection.execute(
            """
            INSERT INTO public.schema_embeddings (schema_name, table_name, ddl_text, embedding)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (schema_name, table_name) DO UPDATE
            SET ddl_text = EXCLUDED.ddl_text, embedding = EXCLUDED.embedding
            """,
            (BENCHMARK_SCHEMA, "probe", "CREATE TABLE ci_benchmark.probe (id int);", vector),
        )
        connection.commit()

        started = time.perf_counter()
        for _ in range(ITERATIONS):
            connection.execute(
                """
                SELECT table_name FROM public.schema_embeddings
                WHERE schema_name = %s
                ORDER BY embedding <=> %s::vector
                LIMIT 3
                """,
                (BENCHMARK_SCHEMA, vector),
            ).fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000 / ITERATIONS
        connection.execute(
            "DELETE FROM public.schema_embeddings WHERE schema_name = %s",
            (BENCHMARK_SCHEMA,),
        )
        connection.commit()

    result = {
        "database": default_database,
        "vector_extension": extension[0] if extension else None,
        "schema_embeddings_table": table_exists,
        "hnsw_index": hnsw_exists,
        "chinook_tables": chinook_tables,
        "vector_lookup_mean_ms": round(elapsed_ms, 3),
    }
    print(json.dumps(result, sort_keys=True))
    if not all((extension, table_exists, hnsw_exists)) or chinook_tables == 0:
        raise RuntimeError(f"database benchmark checks failed: {result}")
    return result


if __name__ == "__main__":
    run()
