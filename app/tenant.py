from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import psycopg

from app.config import Settings, get_settings
from app.models import AuthenticatedUser

_SCRIPTS_DIRECTORY = Path(__file__).resolve().parent.parent / "scripts"
_TENANT_SCHEMA = (_SCRIPTS_DIRECTORY / "tenant.sql").read_text(encoding="utf-8")
_TENANT_CONNECTIONS_SCHEMA = (_SCRIPTS_DIRECTORY / "tenant_connections.sql").read_text(
    encoding="utf-8"
)
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


def _provisioning_database_url(settings: Settings) -> str:
    return settings.control_plane_database_url


def _is_readonly_role(database_url: str) -> bool:
    return urlparse(database_url).username == "schemadrill_readonly"


@lru_cache
def _ensure_tenant_schema(database_url: str) -> None:
    if _is_readonly_role(database_url):
        return
    with psycopg.connect(database_url) as connection:
        connection.execute(_TENANT_SCHEMA)
        connection.execute(_TENANT_CONNECTIONS_SCHEMA)
        connection.commit()


def _normalize_optional_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def provision_user(
    user_id: str,
    settings: Settings | None = None,
    *,
    email: str | None = None,
    display_name: str | None = None,
) -> bool:
    settings = settings or get_settings()
    clean_email = _normalize_optional_text(email)
    clean_display_name = _normalize_optional_text(display_name)
    database_url = _provisioning_database_url(settings)
    _ensure_tenant_schema(database_url)
    with psycopg.connect(database_url) as connection:
        cursor = connection.execute(
            """
            INSERT INTO public.users (user_id, email, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
            """,
            (user_id, clean_email, clean_display_name),
        )
        is_new_user = cursor.fetchone() is not None
        if is_new_user:
            if not _is_readonly_role(database_url):
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
        elif clean_email is not None or clean_display_name is not None:
            connection.execute(
                """
                UPDATE public.users
                SET
                    email = COALESCE(%s::text, email),
                    display_name = COALESCE(%s::text, display_name)
                WHERE user_id = %s::text
                  AND (
                      email IS DISTINCT FROM COALESCE(%s::text, email)
                      OR display_name IS DISTINCT FROM COALESCE(%s::text, display_name)
                  )
                """,
                (
                    clean_email,
                    clean_display_name,
                    user_id,
                    clean_email,
                    clean_display_name,
                ),
            )
        connection.commit()
    return is_new_user


def get_user(user_id: str, settings: Settings | None = None) -> AuthenticatedUser | None:
    settings = settings or get_settings()
    with psycopg.connect(settings.database_url) as connection:
        row = connection.execute(
            """
            SELECT user_id, email, display_name
            FROM public.users
            WHERE user_id = %s
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return AuthenticatedUser(
        user_id=row[0],
        email=row[1],
        display_name=row[2],
    )


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
