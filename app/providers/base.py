from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, SecretStr

from app.models import SqlResponse


class ModelInfo(BaseModel):
    id: str
    display_name: str


class UnknownProviderError(ValueError):
    """Raised when the requested LLM provider name is not registered."""


class ProviderUnavailableError(RuntimeError):
    """Raised when a registered LLM provider is not yet implemented or configured."""


class LLMProvider(Protocol):
    name: str
    display_name: str

    def validate_key(self, api_key: SecretStr) -> list[ModelInfo]: ...

    def list_models(self, api_key: SecretStr) -> list[ModelInfo]: ...

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        api_key: SecretStr,
        model_name: str,
        timeout_ms: int,
    ) -> SqlResponse: ...


class UnavailableLLMProvider:
    def __init__(self, provider_name: str) -> None:
        self.name = provider_name
        self.display_name = provider_name.title()

    def validate_key(self, api_key: SecretStr) -> list[ModelInfo]:
        del api_key
        raise ProviderUnavailableError(f"LLM provider '{self.name}' is not available yet.")

    def list_models(self, api_key: SecretStr) -> list[ModelInfo]:
        del api_key
        raise ProviderUnavailableError(f"LLM provider '{self.name}' is not available yet.")

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        api_key: SecretStr,
        model_name: str,
        timeout_ms: int,
    ) -> SqlResponse:
        del messages, api_key, model_name, timeout_ms
        raise ProviderUnavailableError(f"LLM provider '{self.name}' is not available yet.")
