import json
import logging

import instructor

from app.config import get_settings
from app.models import SqlResponse

logger = logging.getLogger(__name__)


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
        "gemini_request model=%s messages=%s",
        settings.model_name,
        json.dumps(messages, ensure_ascii=True),
    )
    client = instructor.from_provider(
        f"google/{settings.model_name}",
        api_key=settings.gemini_api_key,
        mode=instructor.Mode.GENAI_TOOLS,
    )
    response = client.create(
        messages=messages,
        response_model=SqlResponse,
    )
    logger.info("gemini_response sql=%r", response.sql)
    return response
