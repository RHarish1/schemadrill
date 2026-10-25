"""Small, dependency-free timing primitives used by benchmark instrumentation."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpanRecorder:
    """Collect nested monotonic timing spans without changing application behavior."""

    clock_ns: Any = time.perf_counter_ns
    spans: list[dict[str, Any]] = field(default_factory=list)
    default_parent_span_id: str | None = None

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attempt: int = 1,
        parent_id: str | None = None,
    ) -> Iterator[str]:
        span_id = uuid.uuid4().hex
        parent_id = parent_id if parent_id is not None else self.default_parent_span_id
        started = self.clock_ns()
        status = "success"
        error: str | None = None
        try:
            yield span_id
        except TimeoutError as exception:
            status = "timeout"
            error = str(exception) or type(exception).__name__
            raise
        except BaseException as exception:
            status = "failure"
            error = str(exception) or type(exception).__name__
            raise
        finally:
            self.spans.append(
                {
                    "span_id": span_id,
                    "parent_span_id": parent_id,
                    "stage": name,
                    "duration_ms": (self.clock_ns() - started) / 1_000_000,
                    "status": status,
                    "attempt": attempt,
                    "error": error,
                }
            )


@dataclass
class PipelineTelemetry(SpanRecorder):
    """Optional pipeline telemetry and factual retrieval/model counters."""

    retrieval: dict[str, Any] = field(default_factory=dict)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
