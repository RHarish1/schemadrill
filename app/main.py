from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import close_pool
from app.models import QueryRequest, QueryResult
from app.pipeline import run_pipeline


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    close_pool()


app = FastAPI(title="SchemaDrill", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResult)
def query(request: QueryRequest) -> QueryResult:
    try:
        return run_pipeline(request.question, request.db, settings=get_settings())
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


frontend_directory = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_directory.is_dir():
    app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="frontend")
