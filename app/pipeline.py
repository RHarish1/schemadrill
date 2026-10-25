import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from psycopg import Error as PsycopgError

from app.config import Settings, get_settings
from app.db import readonly_connection
from app.executor import dry_run, execute
from app.instrumentation import PipelineTelemetry
from app.llm_client import generate
from app.models import QueryResult, RetrievalResult, RetryState, RowCapExceeded, SqlResponse
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
    telemetry: PipelineTelemetry | None = None,
) -> list[PipelineStage]:
    retrieve_fn = retrieve or get_retrieval_provider(settings).retrieve
    retrieval_complete = False
    authorized_tables = (
        {table.lower() for table in allowed_tables} if allowed_tables is not None else None
    )

    def retrieval_stage(state: RetryState) -> RetryState:
        nonlocal authorized_tables, retrieval_complete
        if not retrieval_complete:
            with telemetry.span("retrieval", attempt=state.attempt) if telemetry else _null_span():
                retrieved = retrieve_fn(question, db, settings.top_k)
            if isinstance(retrieved, RetrievalResult):
                state.retrieved_ddl = retrieved.blocks
                if telemetry:
                    telemetry.retrieval = {
                        "tables_retrieved": len(retrieved.blocks),
                        "foreign_keys_added": len(retrieved.fk_added),
                        "foreign_key_tables": retrieved.fk_added,
                        "context_characters": sum(
                            len(block.ddl_text) for block in retrieved.blocks
                        ),
                    }
            else:
                state.retrieved_ddl = retrieved
                if telemetry:
                    telemetry.retrieval = {
                        "tables_retrieved": len(retrieved),
                        "foreign_keys_added": 0,
                        "foreign_key_tables": [],
                        "context_characters": sum(len(block.ddl_text) for block in retrieved),
                    }
            retrieved_tables = {
                table_name
                for block in state.retrieved_ddl
                for table_name in (
                    f"{block.schema_name}.{block.table_name}".lower(),
                    block.table_name.lower(),
                )
            }
            authorized_tables = (authorized_tables or set()) | retrieved_tables
            retrieval_complete = True
        return state

    def generation_stage(state: RetryState) -> RetryState:
        messages = messages_for_retry(state)
        with telemetry.span("model_call", attempt=state.attempt) if telemetry else _null_span():
            response = SqlResponse.model_validate(generate_sql(messages))
        if telemetry:
            telemetry.model_calls.append(
                {
                    "attempt": state.attempt,
                    "input_tokens": None,
                    "output_tokens": None,
                    "estimated_cost": None,
                    "provider_latency_ms": None,
                    "request_characters": sum(len(item["content"]) for item in messages),
                    "response_parse_ms": None,
                }
            )
        state.last_sql = response.sql
        return state

    def gate_stage(state: RetryState) -> RetryState:
        with telemetry.span("sql_validation", attempt=state.attempt) if telemetry else _null_span():
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
            with (
                telemetry.span("sql_dry_run", attempt=state.attempt) if telemetry else _null_span()
            ):
                with readonly_connection() as connection:
                    dry_run(connection, state.last_sql or "")
        except PsycopgError as error:
            raise _RetryStage("dry_run_error", str(error).splitlines()[0]) from error
        return state

    def execute_stage(state: RetryState) -> RetryState:
        try:
            with (
                telemetry.span("sql_execution", attempt=state.attempt)
                if telemetry
                else _null_span()
            ):
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
    telemetry: PipelineTelemetry | None = None,
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
        telemetry,
    )
    effective_attempt_budget = 1 if settings.disable_self_correction else settings.max_retries

    root_context = telemetry.span("pipeline", attempt=1) if telemetry else _null_span()
    with root_context as root_span:
        for attempt in range(1, effective_attempt_budget + 1):
            state.attempt = attempt
            attempt_context = (
                telemetry.span("attempt", attempt=attempt, parent_id=root_span)
                if telemetry
                else _null_span()
            )
            try:
                with attempt_context as attempt_span:
                    if telemetry:
                        telemetry.default_parent_span_id = attempt_span
                    for stage in stages:
                        state = RetryState.model_validate(stage(RetryState.model_validate(state)))
            except _RetryStage as error:
                state.last_error = error.detail
                if telemetry:
                    telemetry.spans.append(
                        {
                            "span_id": uuid.uuid4().hex,
                            "parent_span_id": root_span,
                            "stage": "correction_feedback",
                            "duration_ms": 0.0,
                            "status": "success",
                            "attempt": attempt,
                            "error": None,
                        }
                    )
                feedback(error.kind, error.detail)
                continue
            except _TerminalStage as error:
                return QueryResult(
                    status="failed",
                    sql=state.last_sql,
                    reason=str(error),
                    attempts=attempt,
                )
            finally:
                if telemetry:
                    telemetry.default_parent_span_id = None

            dataframe = result["dataframe"]
            columns = list(getattr(dataframe, "columns", []))
            to_dict = getattr(dataframe, "to_dict", None)
            rows = to_dict(orient="records") if callable(to_dict) else []
            with (
                telemetry.span("result_serialization", attempt=attempt)
                if telemetry
                else _null_span()
            ):
                table_markdown = dataframe.to_markdown(index=False)
            return QueryResult(
                status="success",
                sql=state.last_sql,
                table_markdown=table_markdown,
                columns=[str(column) for column in columns],
                rows=rows,
                attempts=attempt,
            )

    return QueryResult(
        status="failed",
        sql=state.last_sql,
        reason=state.last_error or "unknown pipeline failure",
        attempts=effective_attempt_budget,
    )


@contextmanager
def _null_span() -> Iterator[None]:
    yield None
