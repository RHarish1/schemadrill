import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from psycopg import Error as PsycopgError

from app.config import Settings, get_request_id, get_settings
from app.db import readonly_connection
from app.executor import dry_run, execute
from app.instrumentation import PipelineTelemetry
from app.llm_client import generate
from app.models import QueryResult, RetrievalResult, RetryState, RowCapExceeded, SqlResponse
from app.prompting import estimate_prompt_tokens, feedback, messages_for_retry
from app.reliability import classify_provider_error, sanitize_log_detail
from app.retrieval import get_retrieval_provider
from app.sql_guard import default_guard_config, guard_sql
from app.trace import (
    InternalTrace,
    build_public_execution_trace,
    reset_current_trace,
    set_current_trace,
)

logger = logging.getLogger(__name__)


class PipelineStage(Protocol):
    def __call__(self, state: RetryState) -> RetryState: ...


class _RetryStage(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail


class _TerminalStage(Exception):
    pass


def _provider_error_summary(error: Exception) -> str:
    category = classify_provider_error(error).category
    if category == "rate_limit":
        return "AI model rate limit reached."
    if category == "unavailable":
        return "Provider temporarily unavailable."
    return "SQL generation failed."


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
    trace: InternalTrace | None = None,
) -> list[PipelineStage]:
    retrieve_fn = retrieve or get_retrieval_provider(settings).retrieve
    retrieval_complete = False
    authorized_tables = (
        {table.lower() for table in allowed_tables} if allowed_tables is not None else None
    )

    def retrieval_stage(state: RetryState) -> RetryState:
        nonlocal authorized_tables, retrieval_complete
        if not retrieval_complete:
            started = time.perf_counter()
            try:
                with (
                    telemetry.span("retrieval", attempt=state.attempt)
                    if telemetry
                    else _null_span()
                ):
                    retrieved = retrieve_fn(question, db, settings.top_k)
            except Exception:
                if trace is not None:
                    trace.record_stage(
                        "Schema retrieval",
                        status="failed",
                        duration_ms=(time.perf_counter() - started) * 1000,
                        details={"summary": "Schema retrieval failed."},
                    )
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            fk_added_count = 0
            if isinstance(retrieved, RetrievalResult):
                state.retrieved_ddl = retrieved.blocks
                fk_added_count = len(retrieved.fk_added)
                if telemetry:
                    telemetry.retrieval = {
                        "tables_retrieved": len(retrieved.blocks),
                        "foreign_keys_added": fk_added_count,
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
            if trace is not None:
                table_ids = [
                    f"{block.schema_name}.{block.table_name}" for block in state.retrieved_ddl
                ]
                table_names = [block.table_name for block in state.retrieved_ddl]
                trace.record_retrieval(table_ids, table_names, fk_added_count=fk_added_count)
                trace.record_stage(
                    "Schema retrieval",
                    status="completed",
                    duration_ms=elapsed_ms,
                    details={"tables_identified": len(trace.retrieved_tables)},
                    accumulate_duration=False,
                )
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
        prompt_started = time.perf_counter()
        messages = messages_for_retry(state)
        prompt_ms = (time.perf_counter() - prompt_started) * 1000
        request_chars = sum(len(item["content"]) for item in messages)
        if trace is not None:
            trace.record_prompting(prompt_ms, estimate_prompt_tokens(messages), request_chars)

        llm_started = time.perf_counter()
        try:
            with telemetry.span("model_call", attempt=state.attempt) if telemetry else _null_span():
                response = SqlResponse.model_validate(generate_sql(messages))
        except Exception as error:
            llm_ms = (time.perf_counter() - llm_started) * 1000
            if trace is not None:
                trace.record_llm_call(
                    provider="Google Gemini",
                    model=settings.model_name,
                    duration_ms=llm_ms,
                )
                trace.record_stage(
                    "SQL generation",
                    status="failed",
                    duration_ms=llm_ms,
                    details={
                        "provider": "Google Gemini",
                        "model": settings.model_name,
                        "summary": _provider_error_summary(error),
                    },
                )
            raise

        llm_ms = (time.perf_counter() - llm_started) * 1000
        if trace is not None:
            trace.record_llm_call(
                provider="Google Gemini",
                model=settings.model_name,
                duration_ms=llm_ms,
            )
            trace.record_stage(
                "SQL generation",
                status="completed",
                duration_ms=llm_ms,
                details={
                    "provider": "Google Gemini",
                    "model": settings.model_name,
                },
            )
        if telemetry:
            telemetry.model_calls.append(
                {
                    "attempt": state.attempt,
                    "input_tokens": None,
                    "output_tokens": None,
                    "estimated_cost": None,
                    "provider_latency_ms": None,
                    "request_characters": request_chars,
                    "response_parse_ms": None,
                }
            )
        state.last_sql = response.sql
        return state

    def gate_stage(state: RetryState) -> RetryState:
        started = time.perf_counter()
        with telemetry.span("sql_validation", attempt=state.attempt) if telemetry else _null_span():
            guard_result = guard_sql(
                state.last_sql or "",
                authorized_tables or set(),
                default_guard_config(settings.max_result_rows),
            )
        if not guard_result.allowed:
            validation_ms = (time.perf_counter() - started) * 1000
            kind = "parse_error" if guard_result.statement_type == "UNKNOWN" else "sql_guard"
            summary = (
                "Generated SQL could not be parsed as valid PostgreSQL."
                if kind == "parse_error"
                else (
                    guard_result.user_facing_message
                    or "The generated query was rejected by the security policy."
                )
            )
            if trace is not None:
                trace.record_validation(
                    attempt=state.attempt,
                    duration_ms=validation_ms,
                    allowed=False,
                    statement_type=guard_result.statement_type,
                    limit_injected=False,
                    dry_run_passed=False,
                    rejection_reason=guard_result.rejection_reason,
                )
                trace.record_stage(
                    "SQL validation",
                    status="failed",
                    duration_ms=validation_ms,
                    details={"summary": summary},
                )
            raise _RetryStage(kind, guard_result.user_facing_message or "Security policy rejection")
        state.last_sql = guard_result.sql

        try:
            with (
                telemetry.span("sql_dry_run", attempt=state.attempt) if telemetry else _null_span()
            ):
                with readonly_connection() as connection:
                    dry_run(connection, state.last_sql or "")
        except PsycopgError as error:
            validation_ms = (time.perf_counter() - started) * 1000
            if trace is not None:
                trace.record_validation(
                    attempt=state.attempt,
                    duration_ms=validation_ms,
                    allowed=True,
                    statement_type=guard_result.statement_type,
                    limit_injected=guard_result.limit_injected,
                    dry_run_passed=False,
                    rejection_reason=str(error).splitlines()[0],
                )
                trace.record_stage(
                    "SQL validation",
                    status="failed",
                    duration_ms=validation_ms,
                    details={"summary": "Generated SQL failed database dry-run validation."},
                )
            raise _RetryStage("dry_run_error", str(error).splitlines()[0]) from error

        validation_ms = (time.perf_counter() - started) * 1000
        if trace is not None:
            trace.record_validation(
                attempt=state.attempt,
                duration_ms=validation_ms,
                allowed=True,
                statement_type=guard_result.statement_type,
                limit_injected=guard_result.limit_injected,
                dry_run_passed=True,
            )
            summary = (
                "Passed read-only policy and dry-run checks (row limit applied)."
                if guard_result.limit_injected
                else "Passed read-only policy and dry-run checks."
            )
            trace.record_stage(
                "SQL validation",
                status="passed",
                duration_ms=validation_ms,
                details={"summary": summary},
            )
        return state

    def execute_stage(state: RetryState) -> RetryState:
        started = time.perf_counter()
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
            exec_ms = (time.perf_counter() - started) * 1000
            if trace is not None:
                trace.record_execution(duration_ms=exec_ms, error_type="RowCapExceeded")
                trace.record_stage(
                    "Query execution",
                    status="failed",
                    duration_ms=exec_ms,
                    details={"summary": "The query exceeded the maximum allowed result rows."},
                )
            raise _RetryStage("row_cap", str(error)) from error
        except PsycopgError as error:
            exec_ms = (time.perf_counter() - started) * 1000
            detail = sanitize_log_detail(str(error)) or type(error).__name__
            logger.warning(
                "pipeline_execution_failed request_id=%s db=%s attempt=%d error_type=%s detail=%r",
                get_request_id(),
                db,
                state.attempt,
                type(error).__name__,
                detail,
            )
            user_message = (
                "The database query took too long to execute."
                if "statement timeout" in detail.lower()
                else "Query execution failed."
            )
            if trace is not None:
                trace.record_execution(duration_ms=exec_ms, error_type=type(error).__name__)
                trace.record_stage(
                    "Query execution",
                    status="failed",
                    duration_ms=exec_ms,
                    details={"summary": user_message},
                )
            raise _TerminalStage(user_message) from error

        exec_ms = (time.perf_counter() - started) * 1000
        if trace is not None:
            trace.record_execution(duration_ms=exec_ms)
            trace.record_stage(
                "Query execution",
                status="completed",
                duration_ms=exec_ms,
            )
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
    trace: InternalTrace | None = None,
) -> QueryResult:
    settings = settings or get_settings()
    internal_trace = trace or InternalTrace(request_id=get_request_id(), db=db)
    internal_trace.started_at = time.perf_counter()
    internal_trace.model = settings.model_name
    trace_token = set_current_trace(internal_trace)

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
        internal_trace,
    )
    effective_attempt_budget = 1 if settings.disable_self_correction else settings.max_retries

    root_context = telemetry.span("pipeline", attempt=1) if telemetry else _null_span()
    try:
        with root_context as root_span:
            for attempt in range(1, effective_attempt_budget + 1):
                state.attempt = attempt
                internal_trace.attempts = attempt
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
                            state = RetryState.model_validate(
                                stage(RetryState.model_validate(state))
                            )
                except _RetryStage as error:
                    state.last_error = error.detail
                    if attempt < effective_attempt_budget:
                        internal_trace.record_pipeline_retry(error.kind)
                    logger.info(
                        "pipeline_stage_retry request_id=%s db=%s attempt=%d kind=%s detail=%r",
                        get_request_id(),
                        db,
                        attempt,
                        error.kind,
                        sanitize_log_detail(error.detail),
                    )
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
                    internal_trace.log_internal_telemetry(status="failed")
                    public_trace = build_public_execution_trace(internal_trace, finalize=True)
                    return QueryResult(
                        status="failed",
                        sql=state.last_sql,
                        reason=str(error),
                        attempts=attempt,
                        execution_trace=public_trace,
                    )
                finally:
                    if telemetry:
                        telemetry.default_parent_span_id = None

                dataframe = result["dataframe"]
                columns = list(getattr(dataframe, "columns", []))
                to_dict = getattr(dataframe, "to_dict", None)
                rows = to_dict(orient="records") if callable(to_dict) else []
                internal_trace.record_execution(duration_ms=0.0, rows_returned=len(rows))
                internal_trace.record_stage(
                    "Query execution",
                    status="completed",
                    duration_ms=0.0,
                    details={"rows_returned": len(rows)},
                )
                with (
                    telemetry.span("result_serialization", attempt=attempt)
                    if telemetry
                    else _null_span()
                ):
                    table_markdown = dataframe.to_markdown(index=False)
                logger.info(
                    "pipeline_completed request_id=%s db=%s status=success attempts=%d rows=%d",
                    get_request_id(),
                    db,
                    attempt,
                    len(rows),
                )
                internal_trace.log_internal_telemetry(status="success")
                public_trace = build_public_execution_trace(internal_trace, finalize=True)
                return QueryResult(
                    status="success",
                    sql=state.last_sql,
                    table_markdown=table_markdown,
                    columns=[str(column) for column in columns],
                    rows=rows,
                    attempts=attempt,
                    execution_trace=public_trace,
                )

        logger.warning(
            "pipeline_completed request_id=%s db=%s status=failed attempts=%d detail=%r",
            get_request_id(),
            db,
            effective_attempt_budget,
            sanitize_log_detail(state.last_error),
        )
        internal_trace.log_internal_telemetry(status="failed")
        public_trace = build_public_execution_trace(internal_trace, finalize=True)
        return QueryResult(
            status="failed",
            sql=state.last_sql,
            reason=state.last_error or "unknown pipeline failure",
            attempts=effective_attempt_budget,
            execution_trace=public_trace,
        )
    except Exception as error:
        internal_trace.log_internal_telemetry(status="error")
        public_trace = build_public_execution_trace(internal_trace, finalize=True)
        try:
            error._execution_trace = public_trace
        except (AttributeError, TypeError):
            pass
        raise
    finally:
        reset_current_trace(trace_token)


@contextmanager
def _null_span() -> Iterator[None]:
    yield None
