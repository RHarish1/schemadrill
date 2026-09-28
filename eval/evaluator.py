import time
from collections.abc import Callable
from typing import Any

from psycopg.rows import tuple_row

from app.sql_judge import judge_sql
from eval.models import SQLEvalResult
from eval.result_compare import compare_results


def execute_for_evaluation(
    connection: Any, sql: str
) -> tuple[list[str], list[tuple[Any, ...]], float]:
    started = time.perf_counter()
    with connection.cursor(row_factory=tuple_row) as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [description.name for description in cursor.description or []]
    return columns, rows, (time.perf_counter() - started) * 1000


def _error_text(error: BaseException) -> str:
    return str(error).splitlines()[0] or type(error).__name__


def _error_category(error_text: str | None) -> str | None:
    if error_text is None:
        return None
    lowered = error_text.lower()
    if "column " in lowered and " does not exist" in lowered:
        return "missing column"
    if "relation " in lowered and " does not exist" in lowered:
        return "missing table"
    categories = {
        "syntax error": "syntax error",
        "undefined table": "missing table",
        "undefined column": "missing column",
        "function": "invalid function",
        "operator does not exist": "type error",
        "does not exist": "missing object",
        "timeout": "timeout",
        "connection": "connection/database error",
    }
    for marker, category in categories.items():
        if marker in lowered:
            return category
    return "other execution error"


def evaluate_sql_pair(
    question_id: str,
    gold_sql: str,
    generated_sql: str | None,
    connection_factory: Callable[[], Any],
) -> SQLEvalResult:
    gold_columns = generated_columns = None
    gold_rows = generated_rows = None
    gold_time_ms = generated_time_ms = None
    gold_error = generated_error = None

    try:
        with connection_factory() as connection:
            gold_columns, gold_rows, gold_time_ms = execute_for_evaluation(connection, gold_sql)
    except Exception as error:
        gold_error = _error_text(error)

    if generated_sql:
        try:
            with connection_factory() as connection:
                generated_columns, generated_rows, generated_time_ms = execute_for_evaluation(
                    connection, generated_sql
                )
        except Exception as error:
            generated_error = _error_text(error)
    else:
        generated_error = "pipeline did not produce SQL"

    ast_match = None
    if generated_sql:
        try:
            ast_match = judge_sql(generated_sql, gold_sql).matches
        except Exception:
            ast_match = False

    gold_executed = gold_error is None
    generated_executed = generated_error is None
    execution_match = (
        gold_executed
        and generated_executed
        and compare_results(
            gold_columns or [],
            gold_rows or [],
            generated_columns or [],
            generated_rows or [],
        )
    )
    return SQLEvalResult(
        question_id=question_id,
        gold_sql=gold_sql,
        generated_sql=generated_sql,
        gold_executed=gold_executed,
        generated_executed=generated_executed,
        gold_error=gold_error,
        generated_error=generated_error,
        gold_error_category=_error_category(gold_error),
        generated_error_category=_error_category(generated_error),
        execution_match=execution_match,
        ast_match=ast_match,
        gold_row_count=len(gold_rows) if gold_rows is not None else None,
        generated_row_count=len(generated_rows) if generated_rows is not None else None,
        gold_columns=gold_columns,
        generated_columns=generated_columns,
        gold_execution_time_ms=gold_time_ms,
        generated_execution_time_ms=generated_time_ms,
    )
