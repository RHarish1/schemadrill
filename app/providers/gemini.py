from __future__ import annotations

import instructor
from google import genai
from google.genai import types
from pydantic import SecretStr

from app.models import SqlResponse
from app.providers.base import ModelInfo


class GeminiProvider:
    name = "gemini"
    display_name = "Google Gemini"

    def _build_genai_client(self, api_key: SecretStr, *, timeout_ms: int) -> genai.Client:
        return genai.Client(
            api_key=api_key.get_secret_value(),
            http_options=types.HttpOptions(
                timeout=timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def _build_instructor_client(
        self,
        api_key: SecretStr,
        *,
        model_name: str,
        timeout_ms: int,
    ):
        return instructor.from_provider(
            f"google/{model_name}",
            api_key=api_key.get_secret_value(),
            mode=instructor.Mode.GENAI_TOOLS,
            http_options=types.HttpOptions(
                timeout=timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def list_models(self, api_key: SecretStr, *, timeout_ms: int = 10_000) -> list[ModelInfo]:
        client = self._build_genai_client(api_key, timeout_ms=timeout_ms)
        models: list[ModelInfo] = []
        seen: set[str] = set()
        for item in client.models.list():
            actions = getattr(item, "supported_actions", None) or []
            if "generateContent" not in actions:
                continue
            raw_name = getattr(item, "name", None) or ""
            model_id = raw_name.removeprefix("models/").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            display_name = (getattr(item, "display_name", None) or model_id).strip()
            models.append(ModelInfo(id=model_id, display_name=display_name))
        models.sort(key=lambda m: m.id)
        return models

    def validate_key(self, api_key: SecretStr) -> list[ModelInfo]:
        return self.list_models(api_key)

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        api_key: SecretStr,
        model_name: str,
        timeout_ms: int,
    ) -> SqlResponse:
        client = self._build_instructor_client(
            api_key,
            model_name=model_name,
            timeout_ms=timeout_ms,
        )
        return client.create(
            messages=messages,
            response_model=SqlResponse,
        )
