import asyncio

from app.reliability import (
    RedisRateLimiter,
    RetryPolicy,
    call_with_provider_retry,
    is_retryable_provider_error,
    retry_after_seconds,
)


class StatusError(Exception):
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.retry_after = retry_after


def test_retry_classification_excludes_auth_and_validation_errors():
    assert is_retryable_provider_error(StatusError(429))
    assert is_retryable_provider_error(StatusError(503))
    assert not is_retryable_provider_error(StatusError(400))
    assert not is_retryable_provider_error(ValueError("invalid request"))


def test_retry_after_supports_seconds_and_invalid_values():
    assert retry_after_seconds("2") == 2
    assert retry_after_seconds("invalid") is None


def test_redis_rate_limiter_uses_atomic_script_result():
    class FakeRedis:
        async def eval(self, script, keys, key, window):
            assert "INCR" in script
            assert keys == 1
            assert key == "schemadrill:rate:client"
            assert window == 60
            return [2, 59]

    allowed, retry_after = asyncio.run(RedisRateLimiter(FakeRedis()).allow("client", 2, 60))
    assert allowed is True
    assert retry_after == 59


def test_provider_retry_uses_bounded_exponential_backoff():
    attempts = 0
    delays = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(503)
        return "ok"

    result = asyncio.run(
        call_with_provider_retry(
            operation,
            RetryPolicy(3, 0.5, 1.0, 0),
            sleep=lambda delay: delays.append(delay) or asyncio.sleep(0),
            random_value=lambda: 0,
        )
    )

    assert result == "ok"
    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_provider_retry_does_not_retry_permanent_error():
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        raise StatusError(401)

    try:
        asyncio.run(
            call_with_provider_retry(
                operation,
                RetryPolicy(3, 0.5, 1.0, 0),
                sleep=lambda _: asyncio.sleep(0),
            )
        )
    except StatusError:
        pass
    else:
        raise AssertionError("expected permanent provider error")
    assert attempts == 1
