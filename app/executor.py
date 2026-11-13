import pandas as pd
from psycopg import Connection

from app.models import ByteCapExceeded, RowCapExceeded


def dry_run(connection: Connection, sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN " + sql)


def _cell_bytes(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return len(str(value).encode("utf-8"))


def execute(
    connection: Connection,
    sql: str,
    max_rows: int,
    *,
    max_result_bytes: int | None = None,
    max_cell_bytes: int | None = None,
) -> pd.DataFrame:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        if len(rows) > max_rows:
            raise RowCapExceeded(f"result exceeded {max_rows} rows")
        if max_cell_bytes is not None or max_result_bytes is not None:
            total_bytes = 0
            for row in rows:
                for cell in row:
                    size = _cell_bytes(cell)
                    if max_cell_bytes is not None and size > max_cell_bytes:
                        raise ByteCapExceeded(f"result cell exceeded {max_cell_bytes} bytes")
                    total_bytes += size
                    if max_result_bytes is not None and total_bytes > max_result_bytes:
                        raise ByteCapExceeded(f"result exceeded {max_result_bytes} bytes")
        columns = [description.name for description in cursor.description or []]
    return pd.DataFrame(rows, columns=columns)
