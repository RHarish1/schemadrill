import asyncio
import inspect
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr
from redis import asyncio as redis_asyncio

from app.auth import current_user, current_user_id
from app.config import (
    get_request_id,
    get_settings,
    reset_active_llm_key,
    reset_request_id,
    set_active_llm_key,
    set_request_id,
)
from app.db import close_pool
from app.embedding import get_embedding_model
from app.llm_client import ModelRoutingState, provider_metrics
from app.models import (
    AuthenticatedUser,
    ExecutionTraceResponse,
    ModelInfoResponse,
    QueryRequest,
    QueryResult,
    ValidateKeyResponse,
)
from app.pipeline import run_pipeline
from app.providers import (
    ModelInfo,
    ModelListCache,
    ProviderUnavailableError,
    UnknownProviderError,
    get_llm_provider,
)
from app.reliability import (
    InMemoryRateLimiter,
    RedisRateLimiter,
    ReliabilityMetrics,
    classify_provider_error,
    is_retryable_provider_error,
)
from app.tenant import get_user, provision_user, user_has_dataset
from app.trace import InternalTrace, build_public_execution_trace
from evaluation.datasets import dataset_catalog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.query_slots = asyncio.Semaphore(settings.max_concurrent_queries)
    app.state.metrics = ReliabilityMetrics()
    app.state.rate_limiter = InMemoryRateLimiter()
    app.state.model_cache = ModelListCache()
    app.state.redis = None
    if settings.redis_url:
        client = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
        app.state.redis = client
        app.state.rate_limiter = RedisRateLimiter(client)
    if settings.preload_embedding_model:
        logger.info("embedding_model_preload_started model=%s", settings.embedding_model)
        await asyncio.to_thread(get_embedding_model)
        logger.info("embedding_model_preload_completed model=%s", settings.embedding_model)
    yield
    if app.state.redis is not None:
        await app.state.redis.aclose()
    close_pool()


app = FastAPI(title="SchemaDrill", version="0.5.0", lifespan=lifespan)


def _derive_request_id(request: Request) -> str:
    explicit = request.headers.get("X-Request-Id", "").strip()
    if explicit:
        return explicit[:64]
    cloud_trace = request.headers.get("X-Cloud-Trace-Context", "").strip()
    if cloud_trace:
        trace_id = cloud_trace.split("/", 1)[0].strip()
        if trace_id:
            return trace_id[:32]
    return uuid.uuid4().hex[:12]


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = _derive_request_id(request)
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    except Exception as error:
        logger.exception(
            "unhandled_request_error request_id=%s method=%s path=%s error_type=%s",
            request_id,
            request.method,
            request.url.path,
            type(error).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers={"X-Request-Id": request_id},
        )
    finally:
        reset_request_id(token)
    response.headers.setdefault("X-Request-Id", request_id)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me", response_model=AuthenticatedUser)
def me(request: Request) -> AuthenticatedUser:
    settings = get_settings()
    user = current_user(request)
    provision_user(
        user.user_id,
        settings,
        email=user.email,
        display_name=user.display_name,
    )
    return get_user(user.user_id, settings) or user


@app.get("/api/datasets")
def datasets(request: Request) -> dict[str, list[dict[str, object]]]:
    settings = get_settings()
    user = current_user(request)
    provision_user(
        user.user_id,
        settings,
        email=user.email,
        display_name=user.display_name,
    )
    return {"datasets": dataset_catalog()}


@app.get("/metrics")
def metrics(request: Request) -> dict[str, int]:
    current_user_id(request)
    values = provider_metrics.snapshot()
    values.update(app.state.metrics.snapshot())
    return values


_INVALID_KEY_DETAIL = "Your Gemini key was rejected. Update it in Settings."
_FREE_TIER_EXHAUSTED_DETAIL = (
    "Free tier limit reached. Add your own Gemini API key in Settings to continue "
    "with your own quota."
)


def _extract_llm_credentials(request: Request) -> tuple[str, SecretStr | None, bool]:
    raw_provider = request.headers.get("X-LLM-Provider")
    provider_specified = raw_provider is not None and bool(raw_provider.strip())
    provider_name = (raw_provider or "gemini").strip().lower()
    raw_key = (request.headers.get("X-LLM-Api-Key") or "").strip()
    api_key = SecretStr(raw_key) if raw_key else None
    return provider_name, api_key, provider_specified


async def _check_rate_limit(limiter, key: str, limit: int, window_seconds: int):
    check_fn = getattr(limiter, "check", None)
    if callable(check_fn):
        return await check_fn(key, limit, window_seconds)
    allowed, retry_after = await limiter.allow(key, limit, window_seconds)
    from app.reliability import RateLimitResult

    return RateLimitResult(
        allowed=allowed,
        remaining=limit if allowed else 0,
        retry_after=retry_after,
    )


def _static_fallback_models(settings) -> list[ModelInfo]:
    seen: set[str] = set()
    models: list[ModelInfo] = []
    for model_id in [*settings.gemini_fallback_models, settings.model_name]:
        clean = (model_id or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            models.append(ModelInfo(id=clean, display_name=clean))
    return models


async def _resolve_allowed_models(
    *,
    application,
    provider,
    api_key: SecretStr,
    settings,
) -> tuple[list[ModelInfo], JSONResponse | None]:
    cached = application.state.model_cache.get(
        provider.name,
        api_key,
        settings.llm_key_fingerprint_pepper,
    )
    if cached is not None:
        return cached, None
    try:
        models = await asyncio.to_thread(provider.validate_key, api_key)
    except Exception as error:
        info = classify_provider_error(error)
        if info.category == "invalid_key":
            application.state.metrics.increment("invalid_key_errors")
            return [], JSONResponse(
                status_code=403,
                content={"code": "invalid_llm_key", "detail": _INVALID_KEY_DETAIL},
            )
        if is_retryable_provider_error(error):
            return _static_fallback_models(settings), None
        raise HTTPException(
            status_code=503,
            detail="Could not validate API key with provider. Please try again.",
        ) from error
    application.state.model_cache.set(
        provider.name,
        api_key,
        settings.llm_key_fingerprint_pepper,
        models,
    )
    return models, None


def _error_response_with_trace(
    *,
    status_code: int,
    detail: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    execution_trace: ExecutionTraceResponse | None = None,
) -> JSONResponse | None:
    if execution_trace is None and code is None:
        return None
    content: dict[str, object] = {"detail": detail}
    if code is not None:
        content["code"] = code
    if execution_trace is not None:
        content["execution_trace"] = execution_trace.model_dump()
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=headers,
    )


def _handle_classified_query_error(
    error: Exception,
    *,
    request_id: str,
    db: str,
    elapsed_ms: float,
    metrics: ReliabilityMetrics,
    tier: Literal["free", "byok"] = "free",
    execution_trace: ExecutionTraceResponse | None = None,
    rate_limit_remaining: int | None = None,
) -> JSONResponse:
    info = classify_provider_error(error)
    extra_headers: dict[str, str] = {}
    if rate_limit_remaining is not None:
        extra_headers["X-RateLimit-Remaining"] = str(rate_limit_remaining)

    if info.category == "invalid_key":
        metrics.increment("invalid_key_errors")
        logger.warning(
            "query_invalid_llm_key request_id=%s db=%s duration_ms=%.0f error_type=%s "
            "root_error_type=%s status_code=%s detail=%r",
            request_id,
            db,
            elapsed_ms,
            type(error).__name__,
            info.root_error_type,
            info.status_code,
            info.detail,
        )
        return _error_response_with_trace(
            status_code=403,
            detail=_INVALID_KEY_DETAIL,
            code="invalid_llm_key",
            headers=extra_headers or None,
            execution_trace=execution_trace,
        )

    if info.category == "rate_limit":
        metrics.increment("provider_rate_limits")
        logger.warning(
            "query_provider_rate_limited request_id=%s db=%s tier=%s duration_ms=%.0f "
            "error_type=%s root_error_type=%s status_code=%s retry_after=%s detail=%r",
            request_id,
            db,
            tier,
            elapsed_ms,
            type(error).__name__,
            info.root_error_type,
            info.status_code,
            info.retry_after_seconds,
            info.detail,
        )
        if info.retry_after_seconds is not None:
            extra_headers["Retry-After"] = str(info.retry_after_seconds)
        if tier == "byok":
            code: str | None = "llm_quota_exceeded"
            detail = (
                "Your Gemini API key quota was exceeded. Wait a moment, switch models, "
                "or check your Google AI quota."
            )
        else:
            code = None
            detail = (
                "The free tier AI model is temporarily busy. Please try again in a moment "
                "or add your own Gemini API key in Settings."
            )
        response = _error_response_with_trace(
            status_code=429,
            detail=detail,
            code=code,
            headers=extra_headers or None,
            execution_trace=execution_trace,
        )
        if response is not None:
            return response
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers=extra_headers or None,
        ) from error

    if info.category == "unavailable":
        metrics.increment("provider_unavailable")
        logger.warning(
            "query_provider_unavailable request_id=%s db=%s duration_ms=%.0f error_type=%s "
            "root_error_type=%s status_code=%s retry_after=%s detail=%r",
            request_id,
            db,
            elapsed_ms,
            type(error).__name__,
            info.root_error_type,
            info.status_code,
            info.retry_after_seconds,
            info.detail,
        )
        if info.retry_after_seconds is not None:
            extra_headers["Retry-After"] = str(info.retry_after_seconds)
        detail = "The AI model is temporarily busy or unavailable. Please try again in a moment."
        response = _error_response_with_trace(
            status_code=503,
            detail=detail,
            headers=extra_headers or None,
            execution_trace=execution_trace,
        )
        if response is not None:
            return response
        raise HTTPException(
            status_code=503,
            detail=detail,
            headers=extra_headers or None,
        ) from error

    metrics.increment("query_failures")
    logger.exception(
        "query_failed request_id=%s db=%s duration_ms=%.0f error_type=%s "
        "root_error_type=%s status_code=%s",
        request_id,
        db,
        elapsed_ms,
        type(error).__name__,
        info.root_error_type,
        info.status_code,
    )
    response = _error_response_with_trace(
        status_code=500,
        detail="Query failed",
        headers=extra_headers or None,
        execution_trace=execution_trace,
    )
    if response is not None:
        return response
    raise HTTPException(status_code=500, detail="Query failed") from error


@app.post("/api/llm/validate", response_model=ValidateKeyResponse)
async def validate_llm_key(request: Request) -> ValidateKeyResponse | JSONResponse:
    settings = get_settings()
    request_id = get_request_id()
    user = current_user(request)
    provider_name, api_key, _ = _extract_llm_credentials(request)
    key_token = set_active_llm_key(api_key)
    try:
        if api_key is None:
            raise HTTPException(status_code=400, detail="Missing X-LLM-Api-Key header.")
        try:
            provider = get_llm_provider(provider_name)
        except (UnknownProviderError, ProviderUnavailableError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        application = request.app
        try:
            rate_result = await _check_rate_limit(
                application.state.rate_limiter,
                f"validate:{user.user_id}",
                settings.key_validate_limit,
                60,
            )
        except Exception as error:
            logger.warning(
                "rate_limiter_unavailable request_id=%s error_type=%s",
                request_id,
                type(error).__name__,
            )
            if not settings.redis_fail_open:
                raise HTTPException(status_code=503, detail="Rate limiter unavailable") from error
            rate_result = None

        if rate_result is not None and not rate_result.allowed:
            application.state.metrics.increment("key_validate_rejections")
            raise HTTPException(
                status_code=429,
                detail="Too many key validation attempts. Please wait a minute and try again.",
                headers={"Retry-After": str(rate_result.retry_after)},
            )

        models, error_response = await _resolve_allowed_models(
            application=application,
            provider=provider,
            api_key=api_key,
            settings=settings,
        )
        if error_response is not None:
            return error_response
        return ValidateKeyResponse(
            valid=True,
            models=[ModelInfoResponse(id=m.id, display_name=m.display_name) for m in models],
        )
    finally:
        reset_active_llm_key(key_token)


@app.post("/query", response_model=QueryResult)
async def query(
    request: Request,
    payload: QueryRequest,
    response: Response,
) -> QueryResult | JSONResponse:
    settings = get_settings()
    request_id = get_request_id()
    user = current_user(request)
    user_id = user.user_id
    provision_user(
        user_id,
        settings,
        email=user.email,
        display_name=user.display_name,
    )
    if not user_has_dataset(user_id, payload.db, settings):
        raise HTTPException(status_code=404, detail="Dataset not found")

    provider_name, api_key, provider_specified = _extract_llm_credentials(request)
    tier: Literal["free", "byok"] = "byok" if api_key is not None else "free"
    key_token = set_active_llm_key(api_key)
    try:
        provider = None
        if tier == "byok" or provider_specified:
            try:
                provider = get_llm_provider(provider_name)
            except (UnknownProviderError, ProviderUnavailableError) as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

        application = request.app
        remaining_quota: int
        if tier == "free":
            today_key = datetime.now(UTC).strftime("%Y%m%d")
            free_buckets = (
                (f"free:hour:{user_id}", settings.free_tier_hourly_limit, 3600),
                (f"free:day:{user_id}", settings.free_tier_daily_limit, 86400),
                (f"free:global:{today_key}", settings.free_tier_global_daily_limit, 86400),
            )
            bucket_results = []
            blocked = None
            try:
                for bucket_key, limit, window in free_buckets:
                    res = await _check_rate_limit(
                        application.state.rate_limiter,
                        bucket_key,
                        limit,
                        window,
                    )
                    bucket_results.append(res)
                    if not res.allowed:
                        blocked = res
                        break
            except Exception as error:
                logger.warning(
                    "rate_limiter_unavailable request_id=%s tier=free error_type=%s",
                    request_id,
                    type(error).__name__,
                )
                raise HTTPException(status_code=503, detail="Rate limiter unavailable") from error

            if blocked is not None:
                application.state.metrics.increment("free_tier_rejections")
                application.state.metrics.increment("rate_limit_rejections")
                logger.info("free_tier_rate_limit_rejected request_id=%s", request_id)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": _FREE_TIER_EXHAUSTED_DETAIL,
                        "code": "free_tier_exhausted",
                    },
                    headers={
                        "Retry-After": str(blocked.retry_after),
                        "X-RateLimit-Remaining": "0",
                    },
                )
            remaining_quota = min(res.remaining for res in bucket_results)
        else:
            try:
                byok_result = await _check_rate_limit(
                    application.state.rate_limiter,
                    f"byok:{user_id}",
                    settings.byok_rate_limit_per_minute,
                    60,
                )
            except Exception as error:
                logger.warning(
                    "rate_limiter_unavailable request_id=%s tier=byok error_type=%s",
                    request_id,
                    type(error).__name__,
                )
                if not settings.redis_fail_open:
                    raise HTTPException(
                        status_code=503, detail="Rate limiter unavailable"
                    ) from error
                remaining_quota = settings.byok_rate_limit_per_minute
                byok_result = None
            if byok_result is not None:
                if not byok_result.allowed:
                    application.state.metrics.increment("rate_limit_rejections")
                    logger.info("byok_rate_limit_rejected request_id=%s", request_id)
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "Rate limit exceeded"},
                        headers={
                            "Retry-After": str(byok_result.retry_after),
                            "X-RateLimit-Remaining": "0",
                        },
                    )
                remaining_quota = byok_result.remaining

        response.headers["X-RateLimit-Remaining"] = str(remaining_quota)

        if tier == "free":
            if payload.model is not None or payload.fallback_model is not None:
                raise HTTPException(
                    status_code=400,
                    detail="Model selection requires your own API key.",
                )
            selected_model = settings.free_tier_model or settings.model_name
            selected_fallback = None
            effective_max_retries = min(payload.max_retries, settings.free_tier_max_retries)
            provider_max_attempts = settings.free_tier_provider_max_attempts
        else:
            assert api_key is not None and provider is not None
            effective_max_retries = payload.max_retries
            provider_max_attempts = settings.provider_max_attempts
            if payload.model is not None or payload.fallback_model is not None:
                allowed_models, validation_error = await _resolve_allowed_models(
                    application=application,
                    provider=provider,
                    api_key=api_key,
                    settings=settings,
                )
                if validation_error is not None:
                    validation_error.headers["X-RateLimit-Remaining"] = str(remaining_quota)
                    return validation_error
                allowed_ids = {m.id for m in allowed_models}
                if payload.model is not None and payload.model not in allowed_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="Selected model is not available for your API key.",
                    )
                if payload.fallback_model is not None and payload.fallback_model not in allowed_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="Selected fallback model is not available for your API key.",
                    )
            selected_model = payload.model or settings.model_name
            selected_fallback = (
                payload.fallback_model
                if payload.fallback_model and payload.fallback_model != selected_model
                else None
            )

        try:
            await asyncio.wait_for(
                application.state.query_slots.acquire(),
                timeout=settings.concurrency_wait_seconds,
            )
        except TimeoutError as error:
            application.state.metrics.increment("concurrency_rejections")
            logger.info("query_concurrency_rejected request_id=%s", request_id)
            raise HTTPException(status_code=503, detail="Query capacity is busy") from error

        slot_held = True
        started = time.perf_counter()
        deadline = time.monotonic() + settings.query_deadline_seconds
        effective_attempt_budget = 1 + effective_max_retries
        effective_settings = settings.model_copy(
            update={
                "model_name": selected_model,
                "top_k": payload.top_k,
                "enable_fk_expansion": payload.fk_expansion,
                "max_retries": effective_attempt_budget,
                "disable_self_correction": effective_max_retries == 0,
            }
        )
        routing = ModelRoutingState(
            provider_name=provider_name,
            model_name=selected_model,
            fallback_model=selected_fallback,
            max_total_provider_calls=settings.max_total_provider_calls,
            provider_max_attempts=provider_max_attempts,
            deadline=deadline,
        )
        logger.info(
            "query_started request_id=%s db=%s tier=%s model=%s top_k=%d "
            "fk_expansion=%s max_retries=%d",
            request_id,
            payload.db,
            tier,
            selected_model,
            payload.top_k,
            payload.fk_expansion,
            effective_max_retries,
        )
        internal_trace = InternalTrace(request_id=request_id, db=payload.db, tier=tier)
        pipeline_params = inspect.signature(run_pipeline).parameters
        pipeline_kwargs: dict[str, object] = {"settings": effective_settings}
        if "trace" in pipeline_params:
            pipeline_kwargs["trace"] = internal_trace
        if "api_key" in pipeline_params:
            pipeline_kwargs["api_key"] = api_key
        if "routing" in pipeline_params:
            pipeline_kwargs["routing"] = routing
        if "tier" in pipeline_params:
            pipeline_kwargs["tier"] = tier

        task = asyncio.create_task(
            asyncio.to_thread(run_pipeline, payload.question, payload.db, **pipeline_kwargs)
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=settings.query_deadline_seconds,
            )
        except Exception as error:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if isinstance(error, TimeoutError) and not task.done():
                application.state.metrics.increment("query_timeouts")
                logger.warning(
                    "query_timeout request_id=%s db=%s duration_ms=%.0f",
                    request_id,
                    payload.db,
                    elapsed_ms,
                )
                task.add_done_callback(lambda _: application.state.query_slots.release())
                slot_held = False
                timeout_trace = (
                    build_public_execution_trace(internal_trace)
                    if internal_trace.has_stages()
                    else None
                )
                timeout_response = _error_response_with_trace(
                    status_code=504,
                    detail="Query timed out",
                    headers={"X-RateLimit-Remaining": str(remaining_quota)},
                    execution_trace=timeout_trace,
                )
                if timeout_response is not None:
                    return timeout_response
                raise HTTPException(
                    status_code=504,
                    detail="Query timed out",
                    headers={"X-RateLimit-Remaining": str(remaining_quota)},
                ) from error

            execution_trace = getattr(error, "_execution_trace", None)
            if execution_trace is None and internal_trace.has_stages():
                execution_trace = build_public_execution_trace(internal_trace, finalize=True)
            return _handle_classified_query_error(
                error,
                request_id=request_id,
                db=payload.db,
                elapsed_ms=elapsed_ms,
                metrics=application.state.metrics,
                tier=tier,
                execution_trace=execution_trace,
                rate_limit_remaining=remaining_quota,
            )
        finally:
            if slot_held:
                application.state.query_slots.release()

        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "query_completed request_id=%s db=%s tier=%s status=%s attempts=%d "
            "rows=%d duration_ms=%.0f",
            request_id,
            payload.db,
            tier,
            result.status,
            result.attempts,
            len(result.rows),
            elapsed_ms,
        )
        return result
    finally:
        reset_active_llm_key(key_token)


frontend_directory = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
