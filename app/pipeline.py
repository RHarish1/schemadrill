from collections.abc import Callable
from typing import Protocol

from psycopg import Error as PsycopgError

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.executor import dry_run, execute
from app.llm_client import generate
from app.models import QueryResult, RetryState, RowCapExceeded, SqlResponse
from app.prompting import feedback, messages_for_retry
from app.retrieval import get_retrieval_provider
from app.sql_guard import default_guard_config, guard_sql


class PipelineStage(Protocol):
    def __call__(self, state: RetryState) -> RetryState: ...


class _RetryStage(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail


class _TerminalStage(Exception):
    pass


def build_pipeline_stages(
    question: str,
    db: str,
    settings: Settings,
    retrieve: Callable[..., list] | None,
    generate_sql: Callable[[list[dict[str, str]]], object],
    pre_execute_check: Callable[[str], None] | None,
    result: dict[str, object],
    allowed_tables: set[str] | None = None,
) -> list[PipelineStage]:
    retrieve_fn = retrieve or get_retrieval_provider(settings).retrieve
    retrieval_complete = False
    authorized_tables = (
        {table.lower() for table in allowed_tables} if allowed_tables is not None else None
    )

    def retrieval_stage(state: RetryState) -> RetryState:
        nonlocal authorized_tables, retrieval_complete
        if not retrieval_complete:
            state.retrieved_ddl = retrieve_fn(question, db, settings.top_k)
            if authorized_tables is None:
                authorized_tables = {
                    table_name
                    for block in state.retrieved_ddl
                    for table_name in (
                        f"{block.schema_name}.{block.table_name}".lower(),
                        block.table_name.lower(),
                    )
                }
            retrieval_complete = True
        return state

    def generation_stage(state: RetryState) -> RetryState:
        response = SqlResponse.model_validate(generate_sql(messages_for_retry(state)))
        state.last_sql = response.sql
        return state

    def gate_stage(state: RetryState) -> RetryState:
        guard_result = guard_sql(
            state.last_sql or "",
            authorized_tables or set(),
            default_guard_config(settings.max_result_rows),
        )
        if not guard_result.allowed:
            kind = "parse_error" if guard_result.statement_type == "UNKNOWN" else "sql_guard"
            raise _RetryStage(kind, guard_result.user_facing_message or "Security policy rejection")
        state.last_sql = guard_result.sql

        try:
            with readonly_connection() as connection:
                dry_run(connection, state.last_sql or "")
        except PsycopgError as error:
            raise _RetryStage("dry_run_error", str(error).splitlines()[0]) from error
        return state

    def execute_stage(state: RetryState) -> RetryState:
        try:
            with readonly_connection() as connection:
                if pre_execute_check:
                    pre_execute_check(state.last_sql or "")
                result["dataframe"] = execute(
                    connection, state.last_sql or "", settings.max_result_rows
                )
        except RowCapExceeded as error:
            raise _RetryStage("row_cap", str(error)) from error
        except PsycopgError as error:
            raise _TerminalStage(str(error).splitlines()[0]) from error
        return state

    return [retrieval_stage, generation_stage, gate_stage, execute_stage]


def run_pipeline(
    question: str,
    db: str,
    *,
    settings: Settings | None = None,
    retrieve: Callable[..., list] | None = None,
    generate_sql: Callable[[list[dict[str, str]]], object] = generate,
    pre_execute_check: Callable[[str], None] | None = None,
    allowed_tables: set[str] | None = None,
) -> QueryResult:
    settings = settings or get_settings()
    state = RetryState(question=question, attempt=1)
    result: dict[str, object] = {}
    stages = build_pipeline_stages(
        question,
        db,
        settings,
        retrieve,
        generate_sql,
        pre_execute_check,
        result,
        allowed_tables,
    )

    for attempt in range(1, settings.max_retries + 1):
        state.attempt = attempt
        try:
            for stage in stages:
                state = RetryState.model_validate(stage(RetryState.model_validate(state)))
        except _RetryStage as error:
            state.last_error = error.detail
            feedback(error.kind, error.detail)
            continue
        except _TerminalStage as error:
            return QueryResult(
                status="failed",
                sql=state.last_sql,
                reason=str(error),
                attempts=attempt,
            )

        dataframe = result["dataframe"]
        return QueryResult(
            status="success",
            sql=state.last_sql,
            table_markdown=dataframe.to_markdown(index=False),
            attempts=attempt,
        )

    return QueryResult(
        status="failed",
        sql=state.last_sql,
        reason=state.last_error or "unknown pipeline failure",
        attempts=settings.max_retries,
    )
