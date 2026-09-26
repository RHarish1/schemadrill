import logging

from psycopg.rows import dict_row

from app.db import readonly_connection
from app.embedding import embed_query
from app.models import DDLBlock

logger = logging.getLogger(__name__)


def retrieve_schema(question: str, db: str, top_k: int) -> list[DDLBlock]:
    query_vector = embed_query(question)
    query = """
        SELECT schema_name, table_name, ddl_text,
               embedding <=> %s::vector AS cosine_distance
        FROM schema_embeddings
        WHERE schema_name = %s
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """
    with readonly_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, (query_vector, db, query_vector, top_k))
            rows = cursor.fetchall()

    logger.info(
        "vector_similarity_results db=%s question=%r top_k=%d results=%s",
        db,
        question,
        top_k,
        [
            {
                "schema_name": row["schema_name"],
                "table_name": row["table_name"],
                "cosine_distance": float(row["cosine_distance"]),
                "cosine_similarity": 1.0 - float(row["cosine_distance"]),
            }
            for row in rows
        ],
    )
    return [DDLBlock(**row) for row in rows]
