import logging
from collections.abc import Callable
from typing import Protocol

from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.embedding import embed_query
from app.models import DDLBlock, RetrievalResult

logger = logging.getLogger(__name__)


class RetrievalProvider(Protocol):
    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock] | RetrievalResult: ...


class PgVectorRetrievalProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def retrieve(
        self, question: str, db: str, top_k: int
    ) -> list[DDLBlock] | RetrievalResult:
        blocks = _retrieve_pgvector(question, db, top_k)
        if not self.settings.enable_fk_expansion:
            table_names = [f"{block.schema_name}.{block.table_name}" for block in blocks]
            logger.info(
                "fk_retrieval_expansion vector_retrieved_tables=%s "
                "fk_expanded_tables=[] final_retrieved_tables=%s",
                table_names,
                table_names,
            )
            return blocks
        return _retrieve_with_fk_expansion(blocks, db)


class UnavailableRetrievalProvider:
    def __init__(self, backend: str) -> None:
        self.backend = backend

    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock]:
        raise RuntimeError(
            f"Retrieval backend {self.backend!r} is configured but has no v0 adapter"
        )


_PROVIDER_FACTORIES: dict[str, Callable[[Settings], RetrievalProvider]] = {
    "pgvector": PgVectorRetrievalProvider,
    "qdrant": lambda settings: UnavailableRetrievalProvider("qdrant"),
    "faiss": lambda settings: UnavailableRetrievalProvider("faiss"),
}


def get_retrieval_provider(settings: Settings | None = None) -> RetrievalProvider:
    settings = settings or get_settings()
    try:
        return _PROVIDER_FACTORIES[settings.retrieval_backend.lower()](settings)
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


def _retrieve_with_fk_expansion(blocks: list[DDLBlock], db: str) -> RetrievalResult:
    original_keys = {(block.schema_name, block.table_name) for block in blocks}
    if not original_keys:
        logger.info(
            "fk_retrieval_expansion vector_retrieved_tables=[] fk_expanded_tables=[] "
            "final_retrieved_tables=[]"
        )
        return RetrievalResult(blocks=[], fk_added=[])

    retrieved_in_db = sorted(key for key in original_keys if key[0] == db)
    table_names = sorted({table_name for _, table_name in retrieved_in_db})
    catalog_query = """
        SELECT DISTINCT
               tc.table_schema,
               tc.table_name,
               ccu.table_schema AS referenced_schema,
               ccu.table_name AS referenced_table
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON kcu.constraint_catalog = tc.constraint_catalog
         AND kcu.constraint_schema = tc.constraint_schema
         AND kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema = tc.table_schema
         AND kcu.table_name = tc.table_name
        JOIN information_schema.constraint_column_usage AS ccu
          ON ccu.constraint_catalog = tc.constraint_catalog
         AND ccu.constraint_schema = tc.constraint_schema
         AND ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND (
              (tc.table_schema = %s AND tc.table_name = ANY(%s))
              OR (ccu.table_schema = %s AND ccu.table_name = ANY(%s))
          )
    """
    with readonly_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(catalog_query, (db, table_names, db, table_names))
            neighbor_rows = cursor.fetchall()

            neighbor_keys = {
                (row["table_schema"], row["table_name"])
                for row in neighbor_rows
            }
            neighbor_keys.update(
                (row["referenced_schema"], row["referenced_table"])
                for row in neighbor_rows
            )
            expanded_keys = sorted(neighbor_keys - original_keys)
            if not expanded_keys:
                final_blocks = blocks
            else:
                ddl_query = """
                    SELECT schema_name, table_name, ddl_text
                    FROM schema_embeddings
                    WHERE (schema_name, table_name) IN (
                        SELECT * FROM unnest(%s::text[], %s::text[])
                    )
                """
                cursor.execute(
                    ddl_query,
                    (
                        [schema_name for schema_name, _ in expanded_keys],
                        [table_name for _, table_name in expanded_keys],
                    ),
                )
                ddl_by_key = {
                    (row["schema_name"], row["table_name"]): DDLBlock(**row)
                    for row in cursor.fetchall()
                }
                added_blocks = [
                    ddl_by_key[key] for key in expanded_keys if key in ddl_by_key
                ]
                final_blocks = blocks + added_blocks

    final_keys = {(block.schema_name, block.table_name) for block in final_blocks}
    fk_added = [
        f"{schema_name}.{table_name}"
        for schema_name, table_name in expanded_keys
        if (schema_name, table_name) in final_keys
    ]
    logger.info(
        "fk_retrieval_expansion vector_retrieved_tables=%s fk_expanded_tables=%s "
        "final_retrieved_tables=%s",
        [f"{block.schema_name}.{block.table_name}" for block in blocks],
        fk_added,
        [f"{block.schema_name}.{block.table_name}" for block in final_blocks],
    )
    return RetrievalResult(blocks=final_blocks, fk_added=fk_added)
