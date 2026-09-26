from collections.abc import Callable

from psycopg import Error as PsycopgError
from sqlglot.errors import ParseError

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.executor import dry_run, execute
from app.llm_client import generate
from app.models import QueryResult, RowCapExceeded
from app.prompting import build_messages, feedback
from app.retrieval import retrieve_schema
from app.sql_guard import validate_sql


def run_pipeline(
    question: str,
    db: str,
    *,
    settings: Settings | None = None,
    retrieve: Callable[..., list] = retrieve_schema,
    generate_sql: Callable[[list[dict[str, str]]], object] = generate,
) -> QueryResult:
    settings = settings or get_settings()
    blocks = retrieve(question, db, settings.top_k)
    messages = build_messages(blocks, question)
    last_error = "unknown pipeline failure"
    sql = ""
    attempts_used = 0

    for attempt in range(1, settings.max_retries + 1):
        attempts_used = attempt
        response = generate_sql(messages)
        sql = response.sql
        try:
            validate_sql(sql)
        except ParseError as error:
            last_error = str(error)
            messages.append(feedback("parse_error", last_error))
            continue

        try:
            with readonly_connection() as connection:
                dry_run(connection, sql)
        except PsycopgError as error:
            last_error = str(error).splitlines()[0]
            messages.append(feedback("dry_run_error", last_error))
            continue
        break
    else:
        return QueryResult(status="failed", reason=last_error, attempts=attempts_used)

    try:
        with readonly_connection() as connection:
            dataframe = execute(connection, sql, settings.max_result_rows)
    except RowCapExceeded as error:
        messages.append(feedback("row_cap", str(error)))
        response = generate_sql(messages)
        sql = response.sql
        try:
            validate_sql(sql)
            with readonly_connection() as connection:
                dry_run(connection, sql)
                dataframe = execute(connection, sql, settings.max_result_rows)
            attempts_used += 1
        except (ParseError, PsycopgError, RowCapExceeded) as error:
            return QueryResult(status="failed", reason=str(error), attempts=attempts_used)
    except PsycopgError as error:
        return QueryResult(
            status="failed", sql=sql, reason=str(error).splitlines()[0], attempts=attempts_used
        )

    return QueryResult(
        status="success",
        sql=sql,
        table_markdown=dataframe.to_markdown(index=False),
        attempts=attempts_used,
    )
