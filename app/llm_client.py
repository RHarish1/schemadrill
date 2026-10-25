import logging

import instructor
from google.genai import types

from app.config import get_settings
from app.models import SqlResponse
from app.reliability import ReliabilityMetrics, RetryPolicy, call_with_provider_retry_sync

logger = logging.getLogger(__name__)
provider_metrics = ReliabilityMetrics()


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
    logger.info(
        "gemini_request model=%s message_count=%d",
        settings.model_name,
        len(messages),
    )

    def request() -> SqlResponse:
        client = instructor.from_provider(
            f"google/{settings.model_name}",
            api_key=settings.gemini_api_key,
            mode=instructor.Mode.GENAI_TOOLS,
            http_options=types.HttpOptions(
                timeout=int(settings.provider_timeout_seconds * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        return client.create(
            messages=messages,
            response_model=SqlResponse,
        )

    response = call_with_provider_retry_sync(
        request,
        RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            base_delay_seconds=settings.provider_backoff_base_seconds,
            max_delay_seconds=settings.provider_backoff_max_seconds,
            jitter_seconds=settings.provider_backoff_jitter_seconds,
        ),
        metrics=provider_metrics,
    )
    logger.info("gemini_response model=%s", settings.model_name)
    return response
