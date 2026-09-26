import json
import logging

from app.models import DDLBlock

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert PostgreSQL SQL generator.
Use only the tables and columns present in the supplied schema context.
Return exactly one SQL query through the required response schema.
Target dialect: PostgreSQL. Generate a read-only query.
"""


def build_messages(schema_blocks: list[DDLBlock], question: str) -> list[dict[str, str]]:
    schema_context = "\n\n".join(block.ddl_text for block in schema_blocks)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Schema context:\n{schema_context}\n\nQuestion:\n{question}",
        },
    ]
    logger.info("gemini_prompt messages=%s", json.dumps(messages, ensure_ascii=True))
    return messages


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
