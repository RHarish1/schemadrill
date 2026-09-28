from collections.abc import Callable

from psycopg import Error as PsycopgError
from sqlglot.errors import ParseError

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.executor import dry_run, execute
from app.llm_client import generate
from app.models import QueryResult, RetryState, RowCapExceeded
from app.prompting import feedback, messages_for_retry
from app.retrieval import get_retrieval_provider
from app.sql_guard import validate_sql


def run_pipeline(
    question: str,
    db: str,
    *,
    settings: Settings | None = None,
    retrieve: Callable[..., list] | None = None,
    generate_sql: Callable[[list[dict[str, str]]], object] = generate,
    pre_execute_check: Callable[[str], None] | None = None,
) -> QueryResult:
    settings = settings or get_settings()
    retrieve_fn = retrieve or get_retrieval_provider(settings).retrieve
    blocks = retrieve_fn(question, db, settings.top_k)
    state = RetryState(question=question, retrieved_ddl=blocks, attempt=1)
    last_error = "unknown pipeline failure"
    sql = ""
    attempts_used = 0

    for attempt in range(1, settings.max_retries + 1):
        attempts_used = attempt
        state.attempt = attempt
        response = generate_sql(messages_for_retry(state))
        sql = response.sql
        state.last_sql = sql
        try:
            validate_sql(sql)
        except ParseError as error:
            last_error = str(error)
            feedback("parse_error", last_error)
            state.last_error = last_error
            continue

        try:
            with readonly_connection() as connection:
                dry_run(connection, sql)
        except PsycopgError as error:
            last_error = str(error).splitlines()[0]
            feedback("dry_run_error", last_error)
            state.last_error = last_error
            continue
        break
    else:
        return QueryResult(status="failed", reason=last_error, attempts=attempts_used)

    try:
        with readonly_connection() as connection:
            if pre_execute_check:
                pre_execute_check(sql)
            dataframe = execute(connection, sql, settings.max_result_rows)
    except RowCapExceeded as error:
        state.attempt += 1
        state.last_error = str(error)
        feedback("row_cap", state.last_error)
        response = generate_sql(messages_for_retry(state))
        sql = response.sql
        try:
            validate_sql(sql)
            with readonly_connection() as connection:
                dry_run(connection, sql)
                if pre_execute_check:
                    pre_execute_check(sql)
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
