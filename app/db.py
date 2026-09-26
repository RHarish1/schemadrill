from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=get_settings().database_url, open=True, min_size=1, max_size=5
        )
    return _pool


@contextmanager
def readonly_connection() -> Iterator[Connection]:
    with get_pool().connection() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        yield connection


def close_pool() -> None:
    if _pool is not None:
        _pool.close()
