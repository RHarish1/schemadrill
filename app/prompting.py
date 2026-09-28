import json
import logging

from app.models import DDLBlock, RetryState

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert PostgreSQL SQL generator.
Use only the tables and columns present in the supplied schema context.
Return exactly one SQL query through the required response schema.
Target dialect: PostgreSQL. Generate a read-only query.
"""


def build_messages(
    schema_blocks: list[DDLBlock],
    question: str,
    *,
    last_sql: str | None = None,
    last_error: str | None = None,
) -> list[dict[str, str]]:
    schema_context = "\n\n".join(block.ddl_text for block in schema_blocks)
    retry_context = ""
    if last_sql is not None or last_error is not None:
        retry_context = (
            "\n\nPrevious attempt:\n"
            f"SQL: {last_sql or '[none]'}\n"
            f"Failure reason: {last_error or '[none]'}\n"
            "Return a corrected SQL query only."
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Schema context:\n{schema_context}\n\nQuestion:\n{question}{retry_context}",
        },
    ]
    logger.info("gemini_prompt messages=%s", json.dumps(messages, ensure_ascii=True))
    return messages


def messages_for_retry(state: RetryState) -> list[dict[str, str]]:
    return build_messages(
        state.retrieved_ddl,
        state.question,
        last_sql=state.last_sql,
        last_error=state.last_error,
    )


def estimate_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """Estimate Gemini prompt tokens without making a network call.

    Gemini's tokenizer is model-specific; the production client can use the
    provider count endpoint. This stable estimate is useful for retry-shape tests.
    """
    return max(1, sum(len(message["content"].encode("utf-8")) for message in messages) // 4)


def feedback(kind: str, detail: str) -> dict[str, str]:
    templates = {
        "parse_error": (
            "The generated SQL could not be parsed as PostgreSQL. Return a corrected SQL query "
            "only. Parser detail: {detail}"
        ),
        "dry_run_error": (
            "The SQL parsed but failed PostgreSQL EXPLAIN validation. Correct the table, column, "
            "or SQL error and return only SQL. Database detail: {detail}"
        ),
        "row_cap": (
            "The query returned too many rows. Add a suitable LIMIT of at most the requested cap "
            "and return only SQL. Detail: {detail}"
        ),
    }
    return {"role": "user", "content": templates[kind].format(detail=detail)}
