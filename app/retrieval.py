import logging
from collections.abc import Callable
from typing import Protocol

from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.embedding import embed_query
from app.models import DDLBlock

logger = logging.getLogger(__name__)


class RetrievalProvider(Protocol):
    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock]: ...


class PgVectorRetrievalProvider:
    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock]:
        return _retrieve_pgvector(question, db, top_k)


class UnavailableRetrievalProvider:
    def __init__(self, backend: str) -> None:
        self.backend = backend

    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock]:
        raise RuntimeError(
            f"Retrieval backend {self.backend!r} is configured but has no v0 adapter"
        )


_PROVIDER_FACTORIES: dict[str, Callable[[], RetrievalProvider]] = {
    "pgvector": PgVectorRetrievalProvider,
    "qdrant": lambda: UnavailableRetrievalProvider("qdrant"),
    "faiss": lambda: UnavailableRetrievalProvider("faiss"),
}


def get_retrieval_provider(settings: Settings | None = None) -> RetrievalProvider:
    settings = settings or get_settings()
    try:
        return _PROVIDER_FACTORIES[settings.retrieval_backend.lower()]()
    except KeyError as error:
        supported = ", ".join(sorted(_PROVIDER_FACTORIES))
        raise ValueError(
            f"Unsupported retrieval backend {settings.retrieval_backend!r}; choose {supported}"
        ) from error


def retrieve_schema(question: str, db: str, top_k: int) -> list[DDLBlock]:
    return _retrieve_pgvector(question, db, top_k)


def _retrieve_pgvector(question: str, db: str, top_k: int) -> list[DDLBlock]:
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
