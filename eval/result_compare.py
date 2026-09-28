from collections import Counter
from decimal import Decimal
from typing import Any


def _value_key(value: Any) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return ("number", Decimal(value).normalize())
    if isinstance(value, Decimal):
        return ("number", value.normalize())
    if isinstance(value, float):
        return ("number", Decimal(str(value)).normalize())
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ("bytes", bytes(value))
    return (type(value).__name__, value)


def compare_results(
    gold_columns: list[str],
    gold_rows: list[tuple[Any, ...]],
    generated_columns: list[str],
    generated_rows: list[tuple[Any, ...]],
) -> bool:
    """Compare PostgreSQL results using unordered, duplicate-preserving rows.

    This matches the result-set convention used by the SQL benchmarks here:
    column order remains meaningful, row order does not, and row multiplicity
    is retained rather than collapsed into sets.
    """
    if gold_columns != generated_columns:
        return False
    gold = Counter(tuple(_value_key(value) for value in row) for row in gold_rows)
    generated = Counter(tuple(_value_key(value) for value in row) for row in generated_rows)
    return gold == generated
