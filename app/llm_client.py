import logging
import time
from functools import lru_cache

import instructor
from google.genai import types

from app.config import get_request_id, get_settings
from app.models import SqlResponse
from app.reliability import ReliabilityMetrics, RetryPolicy, call_with_provider_retry_sync
from app.trace import get_current_trace

logger = logging.getLogger(__name__)
provider_metrics = ReliabilityMetrics()


@lru_cache(maxsize=4)
def _get_instructor_client(model_name: str, api_key: str, timeout_ms: int):
    return instructor.from_provider(
        f"google/{model_name}",
        api_key=api_key,
        mode=instructor.Mode.GENAI_TOOLS,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def count_prompt_tokens(messages: list[dict[str, str]]) -> int:
    from google import genai

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    result = client.models.count_tokens(model=settings.model_name, contents=messages)
    return int(result.total_tokens)


def generate(messages: list[dict[str, str]]) -> SqlResponse:
    settings = get_settings()
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    request_id = get_request_id()
    logger.info(
        "gemini_request request_id=%s model=%s message_count=%d",
        request_id,
        settings.model_name,
        len(messages),
    )

    timeout_ms = int(settings.provider_timeout_seconds * 1000)

    def request() -> SqlResponse:
        client = _get_instructor_client(settings.model_name, settings.gemini_api_key, timeout_ms)
        return client.create(
            messages=messages,
            response_model=SqlResponse,
        )

    trace = get_current_trace()

    def record_retry(attempt: int, info) -> None:
        if trace is not None:
            trace.record_provider_retry(
                attempt=attempt,
                category=info.category,
                status_code=info.status_code,
                root_error_type=info.root_error_type,
            )

    started = time.perf_counter()
    response = call_with_provider_retry_sync(
        request,
        RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            base_delay_seconds=settings.provider_backoff_base_seconds,
            max_delay_seconds=settings.provider_backoff_max_seconds,
            jitter_seconds=settings.provider_backoff_jitter_seconds,
        ),
        metrics=provider_metrics,
        on_retry=record_retry,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "gemini_response request_id=%s model=%s duration_ms=%.0f",
        request_id,
        settings.model_name,
        elapsed_ms,
    )
    return response
