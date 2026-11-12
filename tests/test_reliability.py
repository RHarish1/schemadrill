import asyncio

import httpx
from google.genai import errors as genai_errors
from instructor.core.exceptions import FailedAttempt, InstructorRetryException

from app.reliability import (
    RedisRateLimiter,
    RetryPolicy,
    call_with_provider_retry,
    call_with_provider_retry_sync,
    classify_provider_error,
    is_retryable_provider_error,
    provider_status_code,
    retry_after_seconds,
    sanitize_log_detail,
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


def _genai_api_error(status_code: int, status_text: str, message: str, *, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(status_code, headers=headers)
    payload = {"error": {"code": status_code, "message": message, "status": status_text}}
    if 400 <= status_code < 500:
        return genai_errors.ClientError(status_code, payload, response)
    return genai_errors.ServerError(status_code, payload, response)


def _wrap_in_instructor_retry(exc: Exception) -> InstructorRetryException:
    try:
        raise InstructorRetryException(
            str(exc),
            n_attempts=1,
            total_usage=0,
            failed_attempts=[],
        ) from exc
    except InstructorRetryException as wrapped:
        return wrapped


def test_wrapped_genai_503_is_classified_and_retried_sync():
    attempts = 0
    delays = []
    wrapped_503 = _wrap_in_instructor_retry(
        _genai_api_error(503, "UNAVAILABLE", "This model is currently experiencing high demand.")
    )

    assert provider_status_code(wrapped_503) == 503
    assert is_retryable_provider_error(wrapped_503)
    info = classify_provider_error(wrapped_503)
    assert info.category == "unavailable"
    assert info.status_code == 503
    assert info.root_error_type == "ServerError"

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise wrapped_503
        return "ok"

    result = call_with_provider_retry_sync(
        operation,
        RetryPolicy(3, 0.25, 1.0, 0),
        sleep=delays.append,
        random_value=lambda: 0,
    )
    assert result == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_wrapped_genai_429_respects_retry_after_and_classifies_rate_limit():
    wrapped_429 = _wrap_in_instructor_retry(
        _genai_api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded", retry_after="4")
    )

    assert provider_status_code(wrapped_429) == 429
    assert is_retryable_provider_error(wrapped_429)
    info = classify_provider_error(wrapped_429)
    assert info.category == "rate_limit"
    assert info.status_code == 429
    assert info.retry_after_seconds == 4
    assert info.root_error_type == "ClientError"


def test_wrapped_failed_attempts_and_non_retryable_401():
    exc_401 = _genai_api_error(401, "UNAUTHENTICATED", "API key not valid")
    wrapped = InstructorRetryException(
        "failed",
        n_attempts=1,
        total_usage=0,
        failed_attempts=[FailedAttempt(attempt_number=1, exception=exc_401)],
    )

    assert provider_status_code(wrapped) == 401
    assert not is_retryable_provider_error(wrapped)
    info = classify_provider_error(wrapped)
    assert info.category == "invalid_key"
    assert info.root_error_type == "ClientError"


def test_sanitize_log_detail_redacts_secrets():
    raw = (
        "Failed with key AQ.Ab8RN6secretKeyValue123 and AIzaSyDummySecret123456 "
        "on postgresql://admin:supersecret@db.internal:5432/app\nsecond line"
    )
    sanitized = sanitize_log_detail(raw)
    assert sanitized is not None
    assert "AQ.Ab8RN6" not in sanitized
    assert "AIzaSyDummy" not in sanitized
    assert "supersecret" not in sanitized
    assert "second line" not in sanitized
    assert "[REDACTED]" in sanitized
