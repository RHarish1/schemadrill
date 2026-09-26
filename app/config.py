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
    top_k: int = 3
    max_retries: int = 3
    max_result_rows: int = 1000
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
