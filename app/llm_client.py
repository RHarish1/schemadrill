from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pydantic import SecretStr

from app.config import get_request_id, get_settings
from app.models import SqlResponse
from app.providers import get_llm_provider
from app.reliability import (
    ReliabilityMetrics,
    RetryPolicy,
    call_with_provider_retry_sync,
    is_retryable_provider_error,
)
from app.trace import get_current_trace

logger = logging.getLogger(__name__)
provider_metrics = ReliabilityMetrics()


class ProviderBudgetExceeded(RuntimeError):
    """Raised when the per-request total provider call cap is reached."""


@dataclass
class ModelRoutingState:
    provider_name: str = "gemini"
    model_name: str = ""
    fallback_model: str | None = None
    fallback_used: bool = False
    total_provider_calls: int = 0
    max_total_provider_calls: int = 12
    provider_max_attempts: int = 3
    deadline: float | None = None


def count_prompt_tokens(messages: list[dict[str, str]]) -> int:
    from google import genai

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    result = client.models.count_tokens(model=settings.model_name, contents=messages)
    return int(result.total_tokens)


def _invoke_with_retry(
    messages: list[dict[str, str]],
    *,
    provider,
    api_key: SecretStr,
    routing: ModelRoutingState,
    timeout_ms: int,
    settings,
    trace,
) -> SqlResponse:
    remaining_calls = max(0, routing.max_total_provider_calls - routing.total_provider_calls)
    if remaining_calls <= 0:
        raise ProviderBudgetExceeded("Maximum total provider calls exceeded for this request.")
    max_attempts = min(routing.provider_max_attempts, remaining_calls)

    def request() -> SqlResponse:
        if routing.total_provider_calls >= routing.max_total_provider_calls:
            raise ProviderBudgetExceeded("Maximum total provider calls exceeded for this request.")
        routing.total_provider_calls += 1
        return provider.generate(
            messages,
            api_key=api_key,
            model_name=routing.model_name,
            timeout_ms=timeout_ms,
        )

    def record_retry(attempt: int, info) -> None:
        if trace is not None:
            trace.record_provider_retry(
                attempt=attempt,
                category=info.category,
                status_code=info.status_code,
                root_error_type=info.root_error_type,
            )

    return call_with_provider_retry_sync(
        request,
        RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=settings.provider_backoff_base_seconds,
            max_delay_seconds=settings.provider_backoff_max_seconds,
            jitter_seconds=settings.provider_backoff_jitter_seconds,
            deadline=routing.deadline,
        ),
        metrics=provider_metrics,
        on_retry=record_retry,
    )


def generate(
    messages: list[dict[str, str]],
    *,
    api_key: SecretStr | None = None,
    routing: ModelRoutingState | None = None,
) -> SqlResponse:
    settings = get_settings()
    effective_key = (
        api_key if (api_key is not None and api_key.get_secret_value()) else settings.gemini_api_key
    )
    if not effective_key.get_secret_value():
        raise RuntimeError("GEMINI_API_KEY is not configured")

    active_routing = routing or ModelRoutingState(
        provider_name="gemini",
        model_name=settings.model_name,
        provider_max_attempts=settings.provider_max_attempts,
        max_total_provider_calls=settings.max_total_provider_calls,
    )
    if not active_routing.model_name:
        active_routing.model_name = settings.model_name

    provider = get_llm_provider(active_routing.provider_name)
    request_id = get_request_id()
    logger.info(
        "llm_request request_id=%s provider=%s model=%s message_count=%d",
        request_id,
        provider.name,
        active_routing.model_name,
        len(messages),
    )

    timeout_ms = int(settings.provider_timeout_seconds * 1000)
    trace = get_current_trace()
    started = time.perf_counter()

    try:
        response = _invoke_with_retry(
            messages,
            provider=provider,
            api_key=effective_key,
            routing=active_routing,
            timeout_ms=timeout_ms,
            settings=settings,
            trace=trace,
        )
    except Exception as error:
        can_fallback = (
            bool(active_routing.fallback_model)
            and not active_routing.fallback_used
            and active_routing.fallback_model != active_routing.model_name
            and is_retryable_provider_error(error)
            and active_routing.total_provider_calls < active_routing.max_total_provider_calls
            and (active_routing.deadline is None or time.monotonic() < active_routing.deadline)
        )
        if not can_fallback:
            raise

        active_routing.model_name = active_routing.fallback_model or active_routing.model_name
        active_routing.fallback_used = True
        provider_metrics.increment("fallback_activations")
        if trace is not None:
            trace.fallback_used = True
            trace.model = active_routing.model_name
        logger.warning(
            "llm_fallback_activated request_id=%s provider=%s fallback_model=%s",
            request_id,
            provider.name,
            active_routing.model_name,
        )
        response = _invoke_with_retry(
            messages,
            provider=provider,
            api_key=effective_key,
            routing=active_routing,
            timeout_ms=timeout_ms,
            settings=settings,
            trace=trace,
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "llm_response request_id=%s provider=%s model=%s fallback_used=%s duration_ms=%.0f",
        request_id,
        provider.name,
        active_routing.model_name,
        active_routing.fallback_used,
        elapsed_ms,
    )
    return response
