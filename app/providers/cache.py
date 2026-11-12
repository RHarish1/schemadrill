from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock

from pydantic import SecretStr

from app.providers.base import ModelInfo


class ModelListCache:
    """In-memory TTL+LRU cache keyed by HMAC-SHA256(pepper, api_key) — never by raw key."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 256,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._now = now
        self._lock = Lock()
        self._entries: OrderedDict[str, tuple[float, list[ModelInfo]]] = OrderedDict()
        self._fallback_pepper = secrets.token_bytes(32)

    def fingerprint(self, provider: str, api_key: SecretStr, pepper: SecretStr) -> str:
        pepper_value = pepper.get_secret_value()
        key_bytes = pepper_value.encode("utf-8") if pepper_value else self._fallback_pepper
        digest = hmac.new(
            key_bytes,
            api_key.get_secret_value().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{provider}:{digest}"

    def get(
        self,
        provider: str,
        api_key: SecretStr,
        pepper: SecretStr,
    ) -> list[ModelInfo] | None:
        cache_key = self.fingerprint(provider, api_key, pepper)
        now = self._now()
        with self._lock:
            item = self._entries.get(cache_key)
            if item is None:
                return None
            expires_at, models = item
            if now >= expires_at:
                del self._entries[cache_key]
                return None
            self._entries.move_to_end(cache_key)
            return list(models)

    def set(
        self,
        provider: str,
        api_key: SecretStr,
        pepper: SecretStr,
        models: list[ModelInfo],
    ) -> None:
        cache_key = self.fingerprint(provider, api_key, pepper)
        now = self._now()
        with self._lock:
            self._entries[cache_key] = (now + self._ttl_seconds, list(models))
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
