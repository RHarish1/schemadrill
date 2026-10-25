import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://schemadrill_readonly:schemadrill@localhost:5432/schemadrill"
    ingest_database_url: str = (
        "postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill"
    )
    gemini_api_key: str = ""
    model_name: str = "gemini-3.1-flash-lite"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    retrieval_backend: str = "pgvector"
    enable_fk_expansion: bool = False
    top_k: int = 3
    max_retries: int = 3
    disable_self_correction: bool = False
    max_result_rows: int = 1000
    redis_url: str = ""
    redis_fail_open: bool = False
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    max_concurrent_queries: int = 8
    concurrency_wait_seconds: float = 0.05
    provider_max_attempts: int = 3
    provider_backoff_base_seconds: float = 0.5
    provider_backoff_max_seconds: float = 8.0
    provider_backoff_jitter_seconds: float = 0.1
    provider_timeout_seconds: float = 30.0
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return settings
