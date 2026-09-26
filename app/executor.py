import pandas as pd
from psycopg import Connection

from app.models import RowCapExceeded


def dry_run(connection: Connection, sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + sql)


def execute(connection: Connection, sql: str, max_rows: int) -> pd.DataFrame:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise RowCapExceeded(f"result exceeded {max_rows} rows")
        columns = [description.name for description in cursor.description or []]
    return pd.DataFrame(rows, columns=columns)
