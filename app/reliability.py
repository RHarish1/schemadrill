import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Protocol

logger = logging.getLogger(__name__)


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


class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]: ...


class RedisRateLimiter:
    _SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return {count, redis.call('TTL', KEYS[1])}
    """

    def __init__(self, client: Any, prefix: str = "schemadrill:rate") -> None:
        self.client = client
        self.prefix = prefix

    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        count, ttl = await self.client.eval(
            self._SCRIPT,
            1,
            f"{self.prefix}:{key}",
            window_seconds,
        )
        return int(count) <= limit, max(int(ttl), 1)


class AllowAllRateLimiter:
    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        del key, limit, window_seconds
        return True, 1


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    jitter_seconds: float


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


def provider_status_code(error: BaseException) -> int | None:
    for attribute in ("status_code", "http_status", "status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def provider_retry_after(error: BaseException) -> str | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        return headers.get("retry-after") or headers.get("Retry-After")
    return getattr(error, "retry_after", None)


def is_retryable_provider_error(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    status = provider_status_code(error)
    return status == 429 or status is not None and 500 <= status <= 599


async def call_with_provider_retry(
    operation: Callable[[], Any],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_value: Callable[[], float] = random.random,
    metrics: ReliabilityMetrics | None = None,
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
                logger.warning(
                    "provider_attempt_exhausted attempt=%d max_attempts=%d "
                    "retryable=%s error_type=%s",
                    attempt,
                    policy.max_attempts,
                    retryable,
                    type(error).__name__,
                )
                raise
            retry_after = retry_after_seconds(provider_retry_after(error))
            delay = min(
                policy.max_delay_seconds,
                policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
            delay = max(delay, retry_after or 0.0) + policy.jitter_seconds * random_value()
            logger.info(
                "provider_retry attempt=%d next_attempt=%d delay_ms=%.0f error_type=%s",
                attempt,
                attempt + 1,
                delay * 1000,
                type(error).__name__,
            )
            metrics.increment("provider_retries") if metrics else None
            await sleep(delay)


def call_with_provider_retry_sync(
    operation: Callable[[], Any],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random.random,
    metrics: ReliabilityMetrics | None = None,
) -> Any:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            retryable = is_retryable_provider_error(error)
            if not retryable or attempt >= policy.max_attempts:
                if attempt >= policy.max_attempts:
                    metrics.increment("provider_exhausted") if metrics else None
                logger.warning(
                    "provider_attempt_exhausted attempt=%d max_attempts=%d "
                    "retryable=%s error_type=%s",
                    attempt,
                    policy.max_attempts,
                    retryable,
                    type(error).__name__,
                )
                raise
            retry_after = retry_after_seconds(provider_retry_after(error))
            delay = min(
                policy.max_delay_seconds,
                policy.base_delay_seconds * (2 ** (attempt - 1)),
            )
            delay = max(delay, retry_after or 0.0) + policy.jitter_seconds * random_value()
            logger.info(
                "provider_retry attempt=%d next_attempt=%d delay_ms=%.0f error_type=%s",
                attempt,
                attempt + 1,
                delay * 1000,
                type(error).__name__,
            )
            metrics.increment("provider_retries") if metrics else None
            sleep(delay)
