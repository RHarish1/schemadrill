import asyncio
import logging
import math
import random
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Literal, Protocol

from app.config import get_active_llm_key, get_request_id

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE),
    re.compile(r"\b(?:AIza|AQ\.)[A-Za-z0-9._-]{8,}"),
    re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)"),
)
_INVALID_KEY_PHRASES = (
    "api key not valid",
    "api_key_invalid",
    "api key expired",
    "invalid api key",
)
_MAX_LOG_DETAIL_CHARS = 300


class ReliabilityMetrics:
    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def increment(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]: ...

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...


class RedisRateLimiter:
    _SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return {count, redis.call('TTL', KEYS[1])}
    """

    def __init__(self, client: Any, prefix: str = "schemadrill:rate") -> None:
        self.client = client
        self.prefix = prefix

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        count, ttl = await self.client.eval(
            self._SCRIPT,
            1,
            f"{self.prefix}:{key}",
            window_seconds,
        )
        count_int = int(count)
        return RateLimitResult(
            allowed=count_int <= limit,
            remaining=max(0, limit - count_int),
            retry_after=max(int(ttl), 1),
        )

    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        result = await self.check(key, limit, window_seconds)
        return result.allowed, result.retry_after


class InMemoryRateLimiter:
    """Thread-safe fixed-window rate limiter used when Redis is not configured."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = Lock()
        self._buckets: dict[str, tuple[int, float]] = {}

    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now = self._now()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None or now >= entry[1]:
                count = 1
                expires_at = now + window_seconds
            else:
                count = entry[0] + 1
                expires_at = entry[1]
            self._buckets[key] = (count, expires_at)
            retry_after = max(1, math.ceil(expires_at - now))
            return RateLimitResult(
                allowed=count <= limit,
                remaining=max(0, limit - count),
                retry_after=retry_after,
            )

    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        result = await self.check(key, limit, window_seconds)
        return result.allowed, result.retry_after


class AllowAllRateLimiter:
    async def check(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        del key, window_seconds
        return RateLimitResult(allowed=True, remaining=limit, retry_after=1)

    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        del key, limit, window_seconds
        return True, 1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float
    deadline: float | None = None


def retry_after_seconds(value: str | None, now: Callable[[], float] = time.time) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class ProviderErrorInfo:
    category: Literal["invalid_key", "rate_limit", "unavailable", "internal"]
    status_code: int | None
    retry_after_seconds: int | None
    root_error_type: str
    detail: str | None


def sanitize_log_detail(text: str | None) -> str | None:
    if not text:
        return None
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    if not line:
        return None
    active_key = get_active_llm_key()
    if active_key is not None:
        secret = active_key.get_secret_value()
        if secret and secret in line:
            line = line.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            line = pattern.sub(r"\1[REDACTED]\2", line)
        elif pattern.groups == 1:
            line = pattern.sub(r"\1[REDACTED]", line)
        else:
            line = pattern.sub("[REDACTED]", line)
    if len(line) > _MAX_LOG_DETAIL_CHARS:
        return line[: _MAX_LOG_DETAIL_CHARS - 3] + "..."
    return line


def _iter_exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [error]
    while stack:
        current = stack.pop()
        obj_id = id(current)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        yield current
        failed_attempts = getattr(current, "failed_attempts", None)
        if failed_attempts:
            for attempt in failed_attempts:
                exc = getattr(attempt, "exception", None)
                if isinstance(exc, BaseException):
                    stack.append(exc)
        if current.__context__ is not None:
            stack.append(current.__context__)
        if current.__cause__ is not None:
            stack.append(current.__cause__)


def _valid_http_status(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def provider_status_code(error: BaseException) -> int | None:
    for exc in _iter_exception_chain(error):
        for attribute in ("status_code", "http_status", "status", "code"):
            status = _valid_http_status(getattr(exc, attribute, None))
            if status is not None:
                return status
        response = getattr(exc, "response", None)
        status = _valid_http_status(getattr(response, "status_code", None))
        if status is not None:
            return status
    return None


def provider_retry_after(error: BaseException) -> str | None:
    for exc in _iter_exception_chain(error):
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            header_value = headers.get("retry-after") or headers.get("Retry-After")
            if header_value:
                return str(header_value)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return str(retry_after)
    return None


def is_invalid_key_error(error: BaseException) -> bool:
    status = provider_status_code(error)
    if status in (401, 403):
        return True
    if status == 400:
        for exc in _iter_exception_chain(error):
            msg = getattr(exc, "message", None)
            text = (msg if isinstance(msg, str) else str(exc)).lower()
            if any(phrase in text for phrase in _INVALID_KEY_PHRASES):
                return True
    return False


def is_retryable_provider_error(error: BaseException) -> bool:
    if is_invalid_key_error(error):
        return False
    for exc in _iter_exception_chain(error):
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return True
    status = provider_status_code(error)
    return status == 429 or (status is not None and 500 <= status <= 599)


def classify_provider_error(error: BaseException) -> ProviderErrorInfo:
    chain = list(_iter_exception_chain(error))
    root = chain[-1]
    status = provider_status_code(error)
    raw_retry_after = retry_after_seconds(provider_retry_after(error))
    retry_after = max(1, math.ceil(raw_retry_after)) if raw_retry_after is not None else None
    is_transport_error = any(
        isinstance(exc, (TimeoutError, ConnectionError, OSError)) for exc in chain
    )

    if is_invalid_key_error(error):
        category: Literal["invalid_key", "rate_limit", "unavailable", "internal"] = "invalid_key"
        retry_after = None
    elif status == 429:
        category = "rate_limit"
    elif (status is not None and 500 <= status <= 599) or is_transport_error:
        category = "unavailable"
    else:
        category = "internal"

    detail_source = getattr(root, "message", None)
    detail = sanitize_log_detail(detail_source if isinstance(detail_source, str) else str(root))
    return ProviderErrorInfo(
        category=category,
        status_code=status,
        retry_after_seconds=retry_after,
        root_error_type=type(root).__name__,
        detail=detail,
    )


def _compute_retry_delay(
    error: BaseException,
    attempt: int,
    policy: RetryPolicy,
    random_value: Callable[[], float],
) -> float:
    retry_after = retry_after_seconds(provider_retry_after(error))
    delay = min(
        policy.max_delay_seconds,
        policy.base_delay_seconds * (2 ** (attempt - 1)),
    )
    return max(delay, retry_after or 0.0) + policy.jitter_seconds * random_value()


def _log_provider_exhausted(
    error: BaseException, attempt: int, max_attempts: int, retryable: bool
) -> None:
    info = classify_provider_error(error)
    logger.warning(
        "provider_attempt_exhausted request_id=%s attempt=%d max_attempts=%d "
        "retryable=%s error_type=%s root_error_type=%s status_code=%s detail=%r",
        get_request_id(),
        attempt,
        max_attempts,
        retryable,
        type(error).__name__,
        info.root_error_type,
        info.status_code,
        info.detail,
    )


def _log_provider_retry(error: BaseException, attempt: int, delay: float) -> None:
    info = classify_provider_error(error)
    logger.info(
        "provider_retry request_id=%s attempt=%d next_attempt=%d delay_ms=%.0f "
        "error_type=%s root_error_type=%s status_code=%s",
        get_request_id(),
        attempt,
        attempt + 1,
        delay * 1000,
        type(error).__name__,
        info.root_error_type,
        info.status_code,
    )


async def call_with_provider_retry(
    operation: Callable[[], Any],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_value: Callable[[], float] = random.random,
    metrics: ReliabilityMetrics | None = None,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await asyncio.to_thread(operation)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            retryable = is_retryable_provider_error(error)
            if not retryable or attempt >= policy.max_attempts:
                if attempt >= policy.max_attempts:
                    metrics.increment("provider_exhausted") if metrics else None
                _log_provider_exhausted(error, attempt, policy.max_attempts, retryable)
                raise
            delay = _compute_retry_delay(error, attempt, policy, random_value)
            if policy.deadline is not None:
                remaining = policy.deadline - now()
                if remaining <= 0 or delay >= remaining:
                    metrics.increment("provider_exhausted") if metrics else None
                    _log_provider_exhausted(error, attempt, policy.max_attempts, retryable)
                    raise
            _log_provider_retry(error, attempt, delay)
            metrics.increment("provider_retries") if metrics else None
            await sleep(delay)


def call_with_provider_retry_sync(
    operation: Callable[[], Any],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
    metrics: ReliabilityMetrics | None = None,
    on_retry: Callable[[int, ProviderErrorInfo], None] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            retryable = is_retryable_provider_error(error)
            if not retryable or attempt >= policy.max_attempts:
                if attempt >= policy.max_attempts:
                    metrics.increment("provider_exhausted") if metrics else None
                _log_provider_exhausted(error, attempt, policy.max_attempts, retryable)
                raise
            delay = _compute_retry_delay(error, attempt, policy, random_value)
            if policy.deadline is not None:
                remaining = policy.deadline - now()
                if remaining <= 0 or delay >= remaining:
                    metrics.increment("provider_exhausted") if metrics else None
                    _log_provider_exhausted(error, attempt, policy.max_attempts, retryable)
                    raise
            _log_provider_retry(error, attempt, delay)
            if on_retry is not None:
                on_retry(attempt, classify_provider_error(error))
            metrics.increment("provider_retries") if metrics else None
            sleep(delay)
