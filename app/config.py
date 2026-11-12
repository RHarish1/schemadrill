import logging
from contextvars import ContextVar
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REQUEST_ID_VAR: ContextVar[str] = ContextVar("request_id", default="-")
_ACTIVE_LLM_KEY_VAR: ContextVar[SecretStr | None] = ContextVar("active_llm_key", default=None)


def get_request_id() -> str:
    return _REQUEST_ID_VAR.get()


def set_request_id(request_id: str):
    return _REQUEST_ID_VAR.set(request_id)


def reset_request_id(token) -> None:
    _REQUEST_ID_VAR.reset(token)


def get_active_llm_key() -> SecretStr | None:
    return _ACTIVE_LLM_KEY_VAR.get()


def set_active_llm_key(api_key: SecretStr | None):
    return _ACTIVE_LLM_KEY_VAR.set(api_key)


def reset_active_llm_key(token) -> None:
    _ACTIVE_LLM_KEY_VAR.reset(token)


class Settings(BaseSettings):
    database_url: str = "postgresql://schemadrill_readonly:schemadrill@localhost:5432/schemadrill"
    ingest_database_url: str = (
        "postgresql://schemadrill_admin:schemadrill@localhost:5432/schemadrill"
    )
    gemini_api_key: SecretStr = SecretStr("")
    model_name: str = "gemini-3.1-flash-lite"
    free_tier_model: str = ""
    free_tier_hourly_limit: int = 5
    free_tier_daily_limit: int = 15
    free_tier_global_daily_limit: int = 300
    free_tier_max_retries: int = 2
    free_tier_provider_max_attempts: int = 2
    byok_rate_limit_per_minute: int = 30
    key_validate_limit: int = 5
    llm_key_fingerprint_pepper: SecretStr = SecretStr("")
    query_deadline_seconds: float = 90.0
    max_total_provider_calls: int = 12
    gemini_fallback_models: list[str] = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.5-pro",
        "gemini-3.1-flash-lite",
    ]
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    preload_embedding_model: bool = True
    full_schema_retrieval: bool = False
    retrieval_backend: str = "pgvector"
    enable_fk_expansion: bool = False
    top_k: int = 3
    max_retries: int = 3
    disable_self_correction: bool = False
    max_result_rows: int = 1000
    redis_url: str = ""
    redis_fail_open: bool = False
    neon_auth_jwks_url: str = ""
    neon_auth_issuer: str = ""
    neon_auth_audience: str = ""
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
