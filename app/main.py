import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from redis import asyncio as redis_asyncio

from app.auth import current_user, current_user_id
from app.config import get_request_id, get_settings, reset_request_id, set_request_id
from app.db import close_pool
from app.embedding import get_embedding_model
from app.llm_client import provider_metrics
from app.models import AuthenticatedUser, QueryRequest, QueryResult
from app.pipeline import run_pipeline
from app.reliability import (
    AllowAllRateLimiter,
    RedisRateLimiter,
    ReliabilityMetrics,
    classify_provider_error,
)
from app.tenant import get_user, provision_user, user_has_dataset
from evaluation.datasets import dataset_catalog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.query_slots = asyncio.Semaphore(settings.max_concurrent_queries)
    app.state.metrics = ReliabilityMetrics()
    app.state.rate_limiter = AllowAllRateLimiter()
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


app = FastAPI(title="SchemaDrill", version="0.2.3", lifespan=lifespan)


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


_PROVIDER_ERROR_RESPONSES: dict[str, tuple[str, str, int, str]] = {
    "rate_limit": (
        "provider_rate_limits",
        "query_provider_rate_limited",
        429,
        "The AI model limit was reached. Please wait a moment and try again.",
    ),
    "unavailable": (
        "provider_unavailable",
        "query_provider_unavailable",
        503,
        "The AI model is temporarily busy or unavailable. Please try again in a moment.",
    ),
}


def _raise_classified_query_error(
    error: Exception,
    *,
    request_id: str,
    db: str,
    elapsed_ms: float,
    metrics: ReliabilityMetrics,
) -> None:
    info = classify_provider_error(error)
    response_spec = _PROVIDER_ERROR_RESPONSES.get(info.category)
    if response_spec is not None:
        metric_name, log_event, status_code, detail = response_spec
        metrics.increment(metric_name)
        logger.warning(
            "%s request_id=%s db=%s duration_ms=%.0f error_type=%s "
            "root_error_type=%s status_code=%s retry_after=%s detail=%r",
            log_event,
            request_id,
            db,
            elapsed_ms,
            type(error).__name__,
            info.root_error_type,
            info.status_code,
            info.retry_after_seconds,
            info.detail,
        )
        headers = (
            {"Retry-After": str(info.retry_after_seconds)}
            if info.retry_after_seconds is not None
            else None
        )
        raise HTTPException(status_code=status_code, detail=detail, headers=headers) from error

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
    raise HTTPException(status_code=500, detail="Query failed") from error


@app.post("/query", response_model=QueryResult)
async def query(request: Request, payload: QueryRequest) -> QueryResult:
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
    client_key = f"user:{user_id}"
    application = request.app
    try:
        allowed, retry_after = await application.state.rate_limiter.allow(
            client_key,
            settings.rate_limit_requests,
            settings.rate_limit_window_seconds,
        )
    except Exception as error:
        logger.warning(
            "rate_limiter_unavailable request_id=%s error_type=%s",
            request_id,
            type(error).__name__,
        )
        if not settings.redis_fail_open:
            raise HTTPException(status_code=503, detail="Rate limiter unavailable") from error
        allowed, retry_after = True, settings.rate_limit_window_seconds
    if not allowed:
        application.state.metrics.increment("rate_limit_rejections")
        logger.info("rate_limit_rejected request_id=%s", request_id)
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
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
    logger.info("query_started request_id=%s db=%s", request_id, payload.db)
    task = asyncio.create_task(
        asyncio.to_thread(run_pipeline, payload.question, payload.db, settings=settings)
    )
    try:
        result = await asyncio.wait_for(
            asyncio.shield(task),
            timeout=settings.provider_timeout_seconds * settings.max_retries,
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
            raise HTTPException(status_code=504, detail="Query timed out") from error

        _raise_classified_query_error(
            error,
            request_id=request_id,
            db=payload.db,
            elapsed_ms=elapsed_ms,
            metrics=application.state.metrics,
        )
    finally:
        if slot_held:
            application.state.query_slots.release()

    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "query_completed request_id=%s db=%s status=%s attempts=%d rows=%d duration_ms=%.0f",
        request_id,
        payload.db,
        result.status,
        result.attempts,
        len(result.rows),
        elapsed_ms,
    )
    return result


frontend_directory = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
