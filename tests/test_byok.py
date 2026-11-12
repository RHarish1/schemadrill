import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from google.genai import errors as genai_errors
from instructor.core.exceptions import InstructorRetryException
from pydantic import SecretStr

from app.auth import _jwks_client
from app.config import Settings, reset_active_llm_key, set_active_llm_key
from app.llm_client import ModelRoutingState, generate
from app.main import app
from app.models import QueryResult, RetryState, SqlResponse
from app.providers import GeminiProvider, ModelInfo, ModelListCache
from app.reliability import (
    InMemoryRateLimiter,
    RetryPolicy,
    call_with_provider_retry_sync,
    classify_provider_error,
    is_retryable_provider_error,
    sanitize_log_detail,
)
from app.trace import InternalTrace, build_public_execution_trace


def _make_auth(monkeypatch, *, sub: str = "byok-user-1", settings: Settings | None = None):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    _jwks_client.cache_clear()
    effective_settings = settings or Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        gemini_api_key="AIzaSyServerFreeTierKey123456",
        llm_key_fingerprint_pepper="test-pepper-secret",
    )
    monkeypatch.setattr("app.auth.get_settings", lambda: effective_settings)
    monkeypatch.setattr("app.main.get_settings", lambda: effective_settings)
    monkeypatch.setattr("app.llm_client.get_settings", lambda: effective_settings)
    fake_jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
    )
    monkeypatch.setattr("app.auth._jwks_client", lambda: fake_jwks)
    monkeypatch.setattr("app.main.provision_user", lambda *args, **kwargs: False)
    monkeypatch.setattr("app.main.user_has_dataset", lambda *args, **kwargs: True)

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": sub,
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
        private_key,
        algorithm="EdDSA",
    )
    return {"Authorization": f"Bearer {token}"}, effective_settings, private_key


def _wrapped_genai_error(status_code: int, status_text: str, message: str, *, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(status_code, headers=headers)
    payload = {"error": {"code": status_code, "message": message, "status": status_text}}
    inner = (
        genai_errors.ClientError(status_code, payload, response)
        if 400 <= status_code < 500
        else genai_errors.ServerError(status_code, payload, response)
    )
    try:
        raise InstructorRetryException(
            str(inner),
            n_attempts=1,
            total_usage=0,
            failed_attempts=[],
        ) from inner
    except InstructorRetryException as wrapped:
        return wrapped


def test_key_never_appears_in_logs_traces_errors_or_repr(monkeypatch, caplog):
    headers, settings, _ = _make_auth(monkeypatch)
    user_key = "custom-non-aiza-secret-token-998877"
    assert user_key not in repr(settings)
    assert user_key not in str(settings)

    state = RetryState(question="List artists", attempt=1)
    assert user_key not in repr(state)

    def raise_error_with_key(*args, **kwargs):
        raise _wrapped_genai_error(500, "INTERNAL", f"Upstream failed for key {user_key}")

    monkeypatch.setattr("app.main.run_pipeline", raise_error_with_key)

    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            response = client.post(
                "/query",
                headers={**headers, "X-LLM-Api-Key": user_key},
                json={"db": "chinook", "question": "List artists"},
            )

    assert response.status_code == 503
    assert user_key not in response.text
    for record in caplog.records:
        assert user_key not in record.getMessage()

    trace = InternalTrace(request_id="req-1", db="chinook", tier="byok", model="gemini-2.5-flash")
    assert user_key not in repr(trace)
    public_trace = build_public_execution_trace(trace, finalize=True)
    assert user_key not in public_trace.model_dump_json()

    token = set_active_llm_key(SecretStr(user_key))
    try:
        assert user_key not in (sanitize_log_detail(f"error with {user_key}") or "")
    finally:
        reset_active_llm_key(token)


def test_per_request_client_never_shared_across_concurrent_keys(monkeypatch):
    seen_keys: list[str] = []

    class FakeInstructorClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def create(self, **kwargs):
            seen_keys.append(self.api_key)
            return SqlResponse(sql="SELECT 1")

    monkeypatch.setattr(
        "app.providers.gemini.instructor.from_provider",
        lambda model, api_key, **kwargs: FakeInstructorClient(api_key),
    )

    provider = GeminiProvider()
    messages = [{"role": "user", "content": "SELECT 1"}]

    async def run_both():
        return await asyncio.gather(
            asyncio.to_thread(
                provider.generate,
                messages,
                api_key=SecretStr("key-alpha-1111"),
                model_name="gemini-2.5-flash",
                timeout_ms=5000,
            ),
            asyncio.to_thread(
                provider.generate,
                messages,
                api_key=SecretStr("key-beta-2222"),
                model_name="gemini-2.5-flash",
                timeout_ms=5000,
            ),
        )

    asyncio.run(run_both())
    assert set(seen_keys) == {"key-alpha-1111", "key-beta-2222"}


def test_free_tier_fails_closed_when_redis_down_and_byok_follows_fail_open(monkeypatch):
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        redis_fail_open=True,
        gemini_api_key="AIzaSyServerKey123456",
    )
    headers, _, _ = _make_auth(monkeypatch, settings=settings)

    class BrokenLimiter:
        async def check(self, key, limit, window):
            raise ConnectionError("redis down")

        async def allow(self, key, limit, window):
            raise ConnectionError("redis down")

    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda *args, **kwargs: QueryResult(
            status="success",
            sql="SELECT 1",
            table_markdown="| 1 |",
            columns=["1"],
            rows=[],
            attempts=1,
        ),
    )

    with TestClient(app) as client:
        app.state.rate_limiter = BrokenLimiter()
        free_resp = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "List artists"},
        )
        assert free_resp.status_code == 503

        byok_resp = client.post(
            "/query",
            headers={**headers, "X-LLM-Api-Key": "AIzaSyUserByokKey123456"},
            json={"db": "chinook", "question": "List artists"},
        )
        assert byok_resp.status_code == 200


def test_free_tier_buckets_and_global_cap_and_no_redis_in_memory(monkeypatch):
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        free_tier_hourly_limit=2,
        free_tier_daily_limit=5,
        free_tier_global_daily_limit=3,
        gemini_api_key="AIzaSyServerKey123456",
    )
    headers_u1, _, priv_key = _make_auth(monkeypatch, sub="user-free-1", settings=settings)
    now = int(time.time())
    token_u2 = jwt.encode(
        {
            "sub": "user-free-2",
            "iss": "https://auth.example.neon.tech",
            "iat": now,
            "exp": now + 900,
        },
        priv_key,
        algorithm="EdDSA",
    )
    headers_u2 = {"Authorization": f"Bearer {token_u2}"}

    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda *args, **kwargs: QueryResult(
            status="success",
            sql="SELECT 1",
            table_markdown="| 1 |",
            columns=["1"],
            rows=[],
            attempts=1,
        ),
    )

    with TestClient(app) as client:
        assert isinstance(app.state.rate_limiter, InMemoryRateLimiter)
        r1 = client.post("/query", headers=headers_u1, json={"db": "chinook", "question": "Q1"})
        assert r1.status_code == 200
        assert r1.headers["X-RateLimit-Remaining"] == "1"

        r2 = client.post("/query", headers=headers_u1, json={"db": "chinook", "question": "Q2"})
        assert r2.status_code == 200
        assert r2.headers["X-RateLimit-Remaining"] == "0"

        r3 = client.post("/query", headers=headers_u1, json={"db": "chinook", "question": "Q3"})
        assert r3.status_code == 429
        assert r3.json()["code"] == "free_tier_exhausted"
        assert r3.headers["X-RateLimit-Remaining"] == "0"

        # User 2 consumes the 3rd global slot
        r4 = client.post("/query", headers=headers_u2, json={"db": "chinook", "question": "Q4"})
        assert r4.status_code == 200

        # Global cap (3) is now exhausted even for User 2
        r5 = client.post("/query", headers=headers_u2, json={"db": "chinook", "question": "Q5"})
        assert r5.status_code == 429
        assert r5.json()["code"] == "free_tier_exhausted"


def test_error_classification_invalid_key_and_byok_429(monkeypatch):
    err_400_key = _wrapped_genai_error(
        400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."
    )
    assert not is_retryable_provider_error(err_400_key)
    assert classify_provider_error(err_400_key).category == "invalid_key"

    err_401 = _wrapped_genai_error(401, "UNAUTHENTICATED", "Unauthorized")
    assert not is_retryable_provider_error(err_401)
    assert classify_provider_error(err_401).category == "invalid_key"

    err_403 = _wrapped_genai_error(403, "PERMISSION_DENIED", "Forbidden")
    assert not is_retryable_provider_error(err_403)
    assert classify_provider_error(err_403).category == "invalid_key"

    headers, _, _ = _make_auth(monkeypatch)
    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(err_400_key),
    )
    with TestClient(app) as client:
        resp_403 = client.post(
            "/query",
            headers={**headers, "X-LLM-Api-Key": "AIzaSyBadKey123456"},
            json={"db": "chinook", "question": "List artists"},
        )
        assert resp_403.status_code == 403
        assert resp_403.json()["code"] == "invalid_llm_key"

        err_429 = _wrapped_genai_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded", retry_after="9")
        monkeypatch.setattr(
            "app.main.run_pipeline",
            lambda *args, **kwargs: (_ for _ in ()).throw(err_429),
        )
        resp_429 = client.post(
            "/query",
            headers={**headers, "X-LLM-Api-Key": "AIzaSyByokQuotaKey123456"},
            json={"db": "chinook", "question": "List artists"},
        )
        assert resp_429.status_code == 429
        assert resp_429.headers["Retry-After"] == "9"
        assert resp_429.json()["code"] == "llm_quota_exceeded"


def test_fallback_triggers_only_on_provider_exhaustion_is_sticky_and_respects_caps(monkeypatch):
    settings = Settings(
        _env_file=None,
        preload_embedding_model=False,
        gemini_api_key="AIzaSyServerKey123456",
        provider_max_attempts=2,
        provider_backoff_base_seconds=0.0,
        provider_backoff_jitter_seconds=0.0,
        max_total_provider_calls=5,
    )
    monkeypatch.setattr("app.llm_client.get_settings", lambda: settings)

    calls: list[str] = []

    class FakeProvider:
        name = "gemini"
        display_name = "Google Gemini"

        def generate(self, messages, *, api_key, model_name, timeout_ms):
            calls.append(model_name)
            if model_name == "primary-model":
                raise _wrapped_genai_error(429, "RESOURCE_EXHAUSTED", "Busy")
            return SqlResponse(sql="SELECT 1")

    monkeypatch.setattr("app.llm_client.get_llm_provider", lambda name: FakeProvider())

    routing = ModelRoutingState(
        provider_name="gemini",
        model_name="primary-model",
        fallback_model="fallback-model",
        max_total_provider_calls=5,
        provider_max_attempts=2,
    )
    res1 = generate([{"role": "user", "content": "q1"}], api_key=SecretStr("k"), routing=routing)
    assert res1.sql == "SELECT 1"
    assert calls == ["primary-model", "primary-model", "fallback-model"]
    assert routing.fallback_used is True
    assert routing.model_name == "fallback-model"

    # Sticky: second call in the same request goes straight to fallback-model
    res2 = generate([{"role": "user", "content": "q2"}], api_key=SecretStr("k"), routing=routing)
    assert res2.sql == "SELECT 1"
    assert calls == ["primary-model", "primary-model", "fallback-model", "fallback-model"]

    # Non-retryable invalid_key must NOT trigger fallback
    class InvalidKeyProvider:
        name = "gemini"
        display_name = "Google Gemini"

        def generate(self, messages, *, api_key, model_name, timeout_ms):
            calls.append(f"invalid:{model_name}")
            raise _wrapped_genai_error(403, "PERMISSION_DENIED", "Forbidden")

    monkeypatch.setattr("app.llm_client.get_llm_provider", lambda name: InvalidKeyProvider())
    routing_invalid = ModelRoutingState(
        provider_name="gemini",
        model_name="primary-model",
        fallback_model="fallback-model",
    )
    with pytest.raises(InstructorRetryException):
        generate(
            [{"role": "user", "content": "q"}],
            api_key=SecretStr("k"),
            routing=routing_invalid,
        )
    assert not routing_invalid.fallback_used
    assert "invalid:fallback-model" not in calls

    # Deadline enforcement: never sleeps past deadline
    sleeps: list[float] = []
    with pytest.raises(InstructorRetryException):
        call_with_provider_retry_sync(
            lambda: (_ for _ in ()).throw(_wrapped_genai_error(503, "UNAVAILABLE", "down")),
            RetryPolicy(
                max_attempts=3,
                base_delay_seconds=5.0,
                max_delay_seconds=10.0,
                jitter_seconds=0.0,
                deadline=100.0,
            ),
            sleep=lambda d: sleeps.append(d),
            now=lambda: 99.0,
        )
    assert sleeps == []


def test_validate_endpoint_and_model_cache_and_query_model_validation(monkeypatch):
    settings = Settings(
        _env_file=None,
        neon_auth_jwks_url="https://auth.example.neon.tech/neondb/auth/.well-known/jwks.json",
        neon_auth_issuer="https://auth.example.neon.tech/neondb/auth",
        preload_embedding_model=False,
        key_validate_limit=2,
        llm_key_fingerprint_pepper="pepper-xyz",
        gemini_api_key="AIzaSyServerKey123456",
    )
    headers, _, _ = _make_auth(monkeypatch, settings=settings)

    list_calls = 0

    class FakeGenaiClient:
        class Models:
            @staticmethod
            def list():
                nonlocal list_calls
                list_calls += 1
                return [
                    SimpleNamespace(
                        name="models/gemini-2.5-flash",
                        display_name="Gemini 2.5 Flash",
                        supported_actions=["generateContent"],
                    ),
                    SimpleNamespace(
                        name="models/text-embedding-004",
                        display_name="Embedding 004",
                        supported_actions=["embedContent"],
                    ),
                ]

        models = Models()

    monkeypatch.setattr(
        "app.providers.gemini.GeminiProvider._build_genai_client",
        lambda self, api_key, timeout_ms=10_000: FakeGenaiClient(),
    )
    monkeypatch.setattr(
        "app.main.run_pipeline",
        lambda *args, **kwargs: QueryResult(
            status="success",
            sql="SELECT 1",
            table_markdown="| 1 |",
            columns=["1"],
            rows=[],
            attempts=1,
        ),
    )

    user_key = "AIzaSyValidUserKey123456"
    with TestClient(app) as client:
        # Unknown provider -> 400
        bad_prov = client.post(
            "/api/llm/validate",
            headers={**headers, "X-LLM-Api-Key": user_key, "X-LLM-Provider": "unknown-llm"},
        )
        assert bad_prov.status_code == 400

        # Valid key -> filtered to generateContent, cached by fingerprint
        val1 = client.post(
            "/api/llm/validate",
            headers={**headers, "X-LLM-Api-Key": user_key},
        )
        assert val1.status_code == 200
        body = val1.json()
        assert body == {
            "valid": True,
            "models": [{"id": "gemini-2.5-flash", "display_name": "Gemini 2.5 Flash"}],
        }
        assert user_key not in val1.text

        # Second validate hits cache (list_calls remains 1)
        val2 = client.post(
            "/api/llm/validate",
            headers={**headers, "X-LLM-Api-Key": user_key},
        )
        assert val2.status_code == 200
        assert list_calls == 1

        # Third validate hits KEY_VALIDATE_LIMIT=2 -> 429
        val3 = client.post(
            "/api/llm/validate",
            headers={**headers, "X-LLM-Api-Key": user_key},
        )
        assert val3.status_code == 429

        # /query rejects model not in user's list
        bad_model = client.post(
            "/query",
            headers={**headers, "X-LLM-Api-Key": user_key},
            json={"db": "chinook", "question": "List artists", "model": "nonexistent-model"},
        )
        assert bad_model.status_code == 400

        # /query rejects model selection on free tier
        free_model = client.post(
            "/query",
            headers=headers,
            json={"db": "chinook", "question": "List artists", "model": "gemini-2.5-flash"},
        )
        assert free_model.status_code == 400

        # /query succeeds with validated model
        ok_model = client.post(
            "/query",
            headers={**headers, "X-LLM-Api-Key": user_key},
            json={"db": "chinook", "question": "List artists", "model": "gemini-2.5-flash"},
        )
        assert ok_model.status_code == 200

    # Cache keys are HMAC fingerprints, never raw keys
    cache = ModelListCache(ttl_seconds=60, max_entries=2)
    cache.set(
        "gemini",
        SecretStr(user_key),
        SecretStr("pepper"),
        [ModelInfo(id="m1", display_name="M1")],
    )
    assert all(user_key not in k for k in cache._entries.keys())


def test_frontend_never_uses_local_storage_and_clears_key_on_signout_and_403():
    frontend_src = Path(__file__).resolve().parent.parent / "frontend" / "src"
    for path in frontend_src.rglob("*"):
        if path.is_file() and path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
            content = path.read_text(encoding="utf-8")
            assert "localStorage" not in content, f"Forbidden localStorage usage in {path}"
            assert "indexedDB" not in content, f"Forbidden indexedDB usage in {path}"

    auth_ts = (frontend_src / "auth.ts").read_text(encoding="utf-8")
    assert "clearLlmKey()" in auth_ts
    # Draft query must not contain apiKey
    draft_section = auth_ts.split("export function saveDraftQuery")[1]
    assert "apiKey" not in draft_section

    app_jsx = (frontend_src / "App.jsx").read_text(encoding="utf-8")
    assert 'eventType === "SIGNED_OUT"' in app_jsx
    assert "clearLlmKey()" in app_jsx

    api_ts = (frontend_src / "services" / "api.ts").read_text(encoding="utf-8")
    assert 'code === "invalid_llm_key"' in api_ts
    assert "clearLlmKey()" in api_ts
