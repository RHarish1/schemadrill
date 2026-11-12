from __future__ import annotations

from collections.abc import Callable

from app.providers.base import (
    LLMProvider,
    ModelInfo,
    ProviderUnavailableError,
    UnavailableLLMProvider,
    UnknownProviderError,
)
from app.providers.cache import ModelListCache
from app.providers.gemini import GeminiProvider

_PROVIDERS: dict[str, Callable[[], LLMProvider]] = {
    "gemini": GeminiProvider,
    "anthropic": lambda: UnavailableLLMProvider("anthropic"),
    "openai": lambda: UnavailableLLMProvider("openai"),
}


def register_llm_provider(name: str, factory: Callable[[], LLMProvider]) -> None:
    _PROVIDERS[name.strip().lower()] = factory


def get_llm_provider(name: str = "gemini") -> LLMProvider:
    normalized = (name or "gemini").strip().lower()
    factory = _PROVIDERS.get(normalized)
    if factory is None:
        raise UnknownProviderError(f"Unknown LLM provider: {normalized}")
    return factory()


__all__ = [
    "GeminiProvider",
    "LLMProvider",
    "ModelInfo",
    "ModelListCache",
    "ProviderUnavailableError",
    "UnavailableLLMProvider",
    "UnknownProviderError",
    "get_llm_provider",
    "register_llm_provider",
]
