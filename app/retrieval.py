import logging
import re
import time
from collections.abc import Callable
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.embedding import embed_query
from app.models import DDLBlock, RetrievalResult
from app.schema import table_ddl
from app.trace import get_current_trace

logger = logging.getLogger(__name__)


class RetrievalProvider(Protocol):
    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock] | RetrievalResult: ...


class PgVectorRetrievalProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock] | RetrievalResult:
        blocks = (
            _retrieve_full_schema(db)
            if self.settings.full_schema_retrieval
            else _retrieve_pgvector(question, db, top_k)
        )
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


_FK_REF_PATTERN = re.compile(
    r"REFERENCES\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.IGNORECASE,
)


class TenantPgVectorRetrievalProvider:
    """Exact cosine scan over tenant_schema_embeddings via the ingest role (no HNSW)."""

    def __init__(self, settings: Settings, *, connection_id: str, user_id: str) -> None:
        self.settings = settings
        self.connection_id = connection_id
        self.user_id = user_id

    def retrieve(self, question: str, db: str, top_k: int) -> list[DDLBlock] | RetrievalResult:
        if self.settings.full_schema_retrieval:
            blocks = self._retrieve_full_schema(db)
        else:
            blocks = self._retrieve_exact_pgvector(question, db, top_k)

        if not self.settings.enable_fk_expansion:
            return blocks
        return self._expand_fks(blocks, db)

    def _retrieve_exact_pgvector(self, question: str, db: str, top_k: int) -> list[DDLBlock]:
        embed_started = time.perf_counter()
        query_vector = embed_query(question)
        embedding_duration_ms = (time.perf_counter() - embed_started) * 1000
        query = """
            SELECT schema_name, table_name, ddl AS ddl_text,
                   embedding <=> %s::vector AS cosine_distance
            FROM public.tenant_schema_embeddings
            WHERE connection_id = %s AND user_id = %s AND schema_name = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        with psycopg.connect(self.settings.ingest_database_url) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    query,
                    (query_vector, self.connection_id, self.user_id, db, query_vector, top_k),
                )
                rows = cursor.fetchall()

        similarity_scores = [
            {
                "schema_name": row["schema_name"],
                "table_name": row["table_name"],
                "cosine_distance": float(row["cosine_distance"]),
                "cosine_similarity": 1.0 - float(row["cosine_distance"]),
            }
            for row in rows
        ]
        trace = get_current_trace()
        if trace is not None:
            trace.record_embedding(embedding_duration_ms, similarity_scores)

        return [
            DDLBlock(
                schema_name=row["schema_name"],
                table_name=row["table_name"],
                ddl_text=row["ddl_text"],
            )
            for row in rows
        ]

    def _retrieve_full_schema(self, db: str) -> list[DDLBlock]:
        query = """
            SELECT schema_name, table_name, ddl AS ddl_text
            FROM public.tenant_schema_embeddings
            WHERE connection_id = %s AND user_id = %s AND schema_name = %s
            ORDER BY table_name
        """
        with psycopg.connect(self.settings.ingest_database_url) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(query, (self.connection_id, self.user_id, db))
                rows = cursor.fetchall()
        return [DDLBlock(**row) for row in rows]

    def _expand_fks(self, blocks: list[DDLBlock], db: str) -> RetrievalResult:
        if not blocks:
            return RetrievalResult(blocks=[], fk_added=[])
        all_blocks = self._retrieve_full_schema(db)
        by_table = {block.table_name.lower(): block for block in all_blocks}
        original_tables = {block.table_name.lower() for block in blocks}

        neighbor_tables: set[str] = set()
        for block in blocks:
            for match in _FK_REF_PATTERN.findall(block.ddl_text):
                neighbor_tables.add(match.lower())
        for candidate in all_blocks:
            refs = {m.lower() for m in _FK_REF_PATTERN.findall(candidate.ddl_text)}
            if refs & original_tables:
                neighbor_tables.add(candidate.table_name.lower())

        expanded_names = sorted(neighbor_tables - original_tables)
        added_blocks = [by_table[name] for name in expanded_names if name in by_table]
        final_blocks = blocks + added_blocks
        fk_added = [f"{b.schema_name}.{b.table_name}" for b in added_blocks]
        return RetrievalResult(blocks=final_blocks, fk_added=fk_added)


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
    embed_started = time.perf_counter()
    query_vector = embed_query(question)
    embedding_duration_ms = (time.perf_counter() - embed_started) * 1000
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

    similarity_scores = [
        {
            "schema_name": row["schema_name"],
            "table_name": row["table_name"],
            "cosine_distance": float(row["cosine_distance"]),
            "cosine_similarity": 1.0 - float(row["cosine_distance"]),
        }
        for row in rows
    ]
    trace = get_current_trace()
    if trace is not None:
        trace.record_embedding(embedding_duration_ms, similarity_scores)

    logger.info(
        "vector_similarity_results db=%s question=%r top_k=%d results=%s",
        db,
        question,
        top_k,
        similarity_scores,
    )
    return [DDLBlock(**row) for row in rows]


def _retrieve_full_schema(db: str) -> list[DDLBlock]:
    with readonly_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """,
                (db,),
            )
            rows = cursor.fetchall()
        blocks = [
            DDLBlock(
                schema_name=db,
                table_name=row["table_name"],
                ddl_text=table_ddl(connection, db, row["table_name"]),
            )
            for row in rows
        ]
    logger.info(
        "full_schema_results db=%s tables=%d context_characters=%d",
        db,
        len(blocks),
        sum(len(block.ddl_text) for block in blocks),
    )
    return blocks


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

            neighbor_keys = {(row["table_schema"], row["table_name"]) for row in neighbor_rows}
            neighbor_keys.update(
                (row["referenced_schema"], row["referenced_table"]) for row in neighbor_rows
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
                added_blocks = [ddl_by_key[key] for key in expanded_keys if key in ddl_by_key]
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
