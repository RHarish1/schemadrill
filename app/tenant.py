from functools import lru_cache
from pathlib import Path

import psycopg

from app.config import Settings, get_settings

_SCRIPTS_DIRECTORY = Path(__file__).resolve().parent.parent / "scripts"
_TENANT_SCHEMA = (_SCRIPTS_DIRECTORY / "tenant.sql").read_text(encoding="utf-8")
_DEFAULT_DATASET_SCRIPTS = (
    (_SCRIPTS_DIRECTORY / "load_demo_dbs.sql").read_text(encoding="utf-8"),
    (_SCRIPTS_DIRECTORY / "load_benchmark_datasets.sql").read_text(encoding="utf-8"),
)
_DEFAULT_DATASETS = (
    ("chinook", "Chinook", "chinook"),
    ("ecommerce", "E-commerce", "ecommerce"),
    ("organization", "Organization", "organization"),
    ("complex_synthetic", "Complex Synthetic", "complex_synthetic"),
)


@lru_cache
def _ensure_tenant_schema(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        connection.execute(_TENANT_SCHEMA)
        connection.commit()


def provision_user(user_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    _ensure_tenant_schema(settings.database_url)
    with psycopg.connect(settings.database_url) as connection:
        cursor = connection.execute(
            """
            INSERT INTO public.users (user_id)
            VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
            """,
            (user_id,),
        )
        is_new_user = cursor.fetchone() is not None
        if is_new_user:
            for script in _DEFAULT_DATASET_SCRIPTS:
                connection.execute(script)
            connection.cursor().executemany(
                """
                INSERT INTO public.user_datasets (dataset_id, user_id, label, schema_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dataset_id, user_id) DO NOTHING
                """,
                [
                    (dataset_id, user_id, label, schema_name)
                    for dataset_id, label, schema_name in _DEFAULT_DATASETS
                ],
            )
        connection.commit()
    return is_new_user


def user_has_dataset(user_id: str, dataset_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    with psycopg.connect(settings.database_url) as connection:
        row = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM public.user_datasets
                WHERE user_id = %s AND dataset_id = %s
            )
            """,
            (user_id, dataset_id),
        ).fetchone()
    return bool(row[0])
