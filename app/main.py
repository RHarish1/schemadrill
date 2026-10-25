import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from redis import asyncio as redis_asyncio

from app.config import get_settings
from app.db import close_pool
from app.llm_client import provider_metrics
from app.models import QueryRequest, QueryResult
from app.pipeline import run_pipeline
from app.reliability import AllowAllRateLimiter, RedisRateLimiter, ReliabilityMetrics
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
    yield
    if app.state.redis is not None:
        await app.state.redis.aclose()
    close_pool()


app = FastAPI(title="SchemaDrill", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/datasets")
def datasets() -> dict[str, list[dict[str, object]]]:
    return {"datasets": dataset_catalog()}


@app.get("/metrics")
def metrics() -> dict[str, int]:
    values = provider_metrics.snapshot()
    values.update(app.state.metrics.snapshot())
    return values


@app.post("/query", response_model=QueryResult)
async def query(request: Request, payload: QueryRequest) -> QueryResult:
    settings = get_settings()
    client_key = request.client.host if request.client else "unknown"
    application = request.app
    try:
        allowed, retry_after = await application.state.rate_limiter.allow(
            client_key,
            settings.rate_limit_requests,
            settings.rate_limit_window_seconds,
        )
    except Exception as error:
        logger.warning("rate_limiter_unavailable error_type=%s", type(error).__name__)
        if not settings.redis_fail_open:
            raise HTTPException(status_code=503, detail="Rate limiter unavailable") from error
        allowed, retry_after = True, settings.rate_limit_window_seconds
    if not allowed:
        application.state.metrics.increment("rate_limit_rejections")
        logger.info("rate_limit_rejected")
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
        logger.info("query_concurrency_rejected")
        raise HTTPException(status_code=503, detail="Query capacity is busy") from error
    slot_held = True
    task = asyncio.create_task(
        asyncio.to_thread(run_pipeline, payload.question, payload.db, settings=settings)
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=settings.provider_timeout_seconds * settings.max_retries,
        )
    except Exception as error:
        if isinstance(error, asyncio.TimeoutError):
            application.state.metrics.increment("query_timeouts")
            logger.warning("query_timeout")
            task.add_done_callback(lambda _: application.state.query_slots.release())
            slot_held = False
            raise HTTPException(status_code=504, detail="Query timed out") from error
        raise HTTPException(status_code=500, detail="Query failed") from error
    finally:
        if slot_held:
            application.state.query_slots.release()


frontend_directory = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
