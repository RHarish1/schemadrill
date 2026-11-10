"""Internal pipeline telemetry and sanitized public execution trace serializer."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

from app.config import get_request_id
from app.models import ExecutionTraceResponse, PublicStageTrace
from app.reliability import sanitize_log_detail

logger = logging.getLogger(__name__)

StageStatus = Literal["pending", "completed", "passed", "failed"]

_CURRENT_TRACE_VAR: ContextVar[InternalTrace | None] = ContextVar("execution_trace", default=None)

_PUBLIC_STAGE_DETAIL_ALLOWLIST: dict[str, frozenset[str]] = {
    "Schema retrieval": frozenset({"tables_identified", "summary"}),
    "SQL generation": frozenset({"provider", "model", "summary"}),
    "SQL validation": frozenset({"summary"}),
    "Query execution": frozenset({"rows_returned", "summary"}),
}

_PUBLIC_RETRY_LABELS: dict[str, str] = {
    "parse_error": "SQL syntax validation",
    "sql_guard": "SQL security policy",
    "dry_run_error": "Database dry-run validation",
    "row_cap": "Result row limit exceeded",
    "rate_limit": "Provider rate limit",
    "unavailable": "Provider unavailable",
    "internal": "Provider error",
}


def get_current_trace() -> InternalTrace | None:
    return _CURRENT_TRACE_VAR.get()


def set_current_trace(trace: InternalTrace | None) -> Token[InternalTrace | None]:
    return _CURRENT_TRACE_VAR.set(trace)


def reset_current_trace(token: Token[InternalTrace | None]) -> None:
    _CURRENT_TRACE_VAR.reset(token)


class StageTrace:
    """Mutable stage-level telemetry record accumulated during query execution."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status: StageStatus = "pending"
        self.duration_ms: float = 0.0
        self.details: dict[str, object] = {}


@dataclass
class InternalTrace:
    """Internal-only query telemetry; never serialized raw to API clients."""

    request_id: str = field(default_factory=get_request_id)
    db: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    total_duration_ms: float | None = None
    stages: dict[str, StageTrace] = field(default_factory=dict)

    # Retrieval telemetry
    embedding_duration_ms: float | None = None
    retrieved_table_ids: list[str] = field(default_factory=list)
    retrieved_tables: list[str] = field(default_factory=list)
    similarity_scores: list[dict[str, object]] = field(default_factory=list)
    fk_added_count: int = 0

    # Prompting telemetry
    prompt_construction_ms: float = 0.0
    estimated_prompt_tokens: int | None = None
    request_characters: int = 0

    # LLM telemetry
    provider: str = "Google Gemini"
    model: str = ""
    llm_duration_ms: float = 0.0
    provider_retries: int = 0
    provider_retry_reasons: list[dict[str, object]] = field(default_factory=list)

    # Validation telemetry
    sql_parser: str = "sqlglot/postgres"
    validation_results: list[dict[str, object]] = field(default_factory=list)
    validation_duration_ms: float = 0.0

    # Self-correction telemetry
    attempts: int = 0
    pipeline_retries: int = 0
    retry_error_categories: list[str] = field(default_factory=list)

    # Execution telemetry
    execution_duration_ms: float = 0.0
    rows_returned: int | None = None
    execution_error_type: str | None = None

    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def record_stage(
        self,
        name: str,
        *,
        status: StageStatus,
        duration_ms: float,
        details: dict[str, object] | None = None,
        accumulate_duration: bool = True,
    ) -> None:
        with self._lock:
            stage = self.stages.get(name)
            if stage is None:
                stage = StageTrace(name)
                self.stages[name] = stage
            stage.status = status
            stage.duration_ms = (
                stage.duration_ms + duration_ms if accumulate_duration else duration_ms
            )
            if details:
                stage.details.update(details)

    def record_retrieval(
        self,
        table_ids: list[str],
        table_names: list[str],
        *,
        fk_added_count: int = 0,
    ) -> None:
        with self._lock:
            self.retrieved_table_ids = list(table_ids)
            self.retrieved_tables = list(dict.fromkeys(table_names))
            self.fk_added_count = fk_added_count

    def record_embedding(
        self,
        duration_ms: float,
        similarity_scores: list[dict[str, object]],
    ) -> None:
        with self._lock:
            self.embedding_duration_ms = duration_ms
            self.similarity_scores = list(similarity_scores)

    def record_prompting(
        self,
        duration_ms: float,
        estimated_tokens: int,
        request_characters: int,
    ) -> None:
        with self._lock:
            self.prompt_construction_ms += duration_ms
            self.estimated_prompt_tokens = estimated_tokens
            self.request_characters = request_characters

    def record_llm_call(self, *, provider: str, model: str, duration_ms: float) -> None:
        with self._lock:
            self.provider = provider
            self.model = model
            self.llm_duration_ms += duration_ms

    def record_provider_retry(
        self,
        *,
        attempt: int,
        category: str,
        status_code: int | None,
        root_error_type: str,
    ) -> None:
        with self._lock:
            self.provider_retries += 1
            self.provider_retry_reasons.append(
                {
                    "attempt": attempt,
                    "category": category,
                    "status_code": status_code,
                    "root_error_type": root_error_type,
                }
            )
            self.retry_error_categories.append(category)

    def record_validation(
        self,
        *,
        attempt: int,
        duration_ms: float,
        allowed: bool,
        statement_type: str,
        limit_injected: bool,
        dry_run_passed: bool,
        rejection_reason: str | None = None,
    ) -> None:
        with self._lock:
            self.validation_duration_ms += duration_ms
            self.validation_results.append(
                {
                    "attempt": attempt,
                    "parser": self.sql_parser,
                    "allowed": allowed,
                    "statement_type": statement_type,
                    "limit_injected": limit_injected,
                    "dry_run_passed": dry_run_passed,
                    "rejection_reason": sanitize_log_detail(rejection_reason),
                }
            )

    def record_pipeline_retry(self, kind: str) -> None:
        with self._lock:
            self.pipeline_retries += 1
            self.retry_error_categories.append(kind)

    def record_execution(
        self,
        *,
        duration_ms: float,
        rows_returned: int | None = None,
        error_type: str | None = None,
    ) -> None:
        with self._lock:
            self.execution_duration_ms += duration_ms
            if rows_returned is not None:
                self.rows_returned = rows_returned
            if error_type is not None:
                self.execution_error_type = error_type

    def finish(self) -> float:
        with self._lock:
            if self.total_duration_ms is None:
                self.total_duration_ms = (time.perf_counter() - self.started_at) * 1000
            return self.total_duration_ms

    def has_stages(self) -> bool:
        with self._lock:
            return bool(self.stages)

    def log_internal_telemetry(self, *, status: str) -> None:
        total_ms = self.finish()
        with self._lock:
            logger.info(
                "pipeline_internal_telemetry request_id=%s db=%s status=%s "
                "total_duration_ms=%.2f embedding_ms=%s retrieved_tables=%s "
                "similarity_scores=%s fk_added=%d prompt_ms=%.2f prompt_tokens=%s "
                "request_chars=%d llm_provider=%s llm_model=%s llm_ms=%.2f "
                "provider_retries=%d provider_retry_reasons=%s sql_parser=%s "
                "validation_ms=%.2f validation_results=%s attempts=%d "
                "pipeline_retries=%d retry_categories=%s execution_ms=%.2f "
                "rows_returned=%s execution_error_type=%s",
                self.request_id,
                self.db,
                status,
                total_ms,
                (
                    f"{self.embedding_duration_ms:.2f}"
                    if self.embedding_duration_ms is not None
                    else None
                ),
                self.retrieved_table_ids,
                self.similarity_scores,
                self.fk_added_count,
                self.prompt_construction_ms,
                self.estimated_prompt_tokens,
                self.request_characters,
                self.provider,
                self.model,
                self.llm_duration_ms,
                self.provider_retries,
                self.provider_retry_reasons,
                self.sql_parser,
                self.validation_duration_ms,
                self.validation_results,
                self.attempts,
                self.pipeline_retries,
                self.retry_error_categories,
                self.execution_duration_ms,
                self.rows_returned,
                self.execution_error_type,
            )


def build_public_execution_trace(
    trace: InternalTrace,
    *,
    finalize: bool = False,
) -> ExecutionTraceResponse:
    """Serialize internal telemetry into a sanitized user-facing execution trace."""
    total_ms = (
        trace.finish()
        if finalize
        else (
            trace.total_duration_ms
            if trace.total_duration_ms is not None
            else (time.perf_counter() - trace.started_at) * 1000
        )
    )
    with trace._lock:
        public_stages: list[PublicStageTrace] = []
        for stage in trace.stages.values():
            allowed_keys = _PUBLIC_STAGE_DETAIL_ALLOWLIST.get(stage.name, frozenset())
            sanitized_details = {
                key: value
                for key, value in stage.details.items()
                if key in allowed_keys and value is not None
            }
            public_stages.append(
                PublicStageTrace(
                    name=stage.name,
                    status=stage.status,
                    duration_ms=round(stage.duration_ms, 2),
                    details=sanitized_details,
                )
            )

        retry_reasons: list[str] = []
        for category in trace.retry_error_categories:
            label = _PUBLIC_RETRY_LABELS.get(category)
            if label and label not in retry_reasons:
                retry_reasons.append(label)

        return ExecutionTraceResponse(
            stages=public_stages,
            retrieved_tables=list(trace.retrieved_tables),
            retry_count=trace.pipeline_retries + trace.provider_retries,
            retry_reasons=retry_reasons,
            total_duration_ms=round(total_ms, 2),
        )
