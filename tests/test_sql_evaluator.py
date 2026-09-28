from contextlib import contextmanager
from decimal import Decimal

from eval.evaluator import _error_category, evaluate_sql_pair
from eval.result_compare import compare_results


def test_result_comparison_is_unordered_but_preserves_duplicates_and_nulls():
    assert compare_results(
        ["name", "value"],
        [("A", None), ("A", Decimal("1.0")), ("B", Decimal("2.0"))],
        ["name", "value"],
        [("B", 2.0), ("A", 1), ("A", None)],
    )
    assert not compare_results(["name"], [("A",), ("A",)], ["name"], [("A",)])


def test_evaluator_separates_execution_success_from_result_match():
    responses = {
        "SELECT gold": (["name"], [("Engineering",)]),
        "SELECT wrong": (["name"], [("Sales",)]),
    }

    @contextmanager
    def connection_factory():
        yield FakeConnection(responses)

    result = evaluate_sql_pair("q_1", "SELECT gold", "SELECT wrong", connection_factory)

    assert result.gold_executed
    assert result.generated_executed
    assert not result.execution_match
    assert result.ast_match is False
    assert result.gold_row_count == result.generated_row_count == 1


def test_evaluator_records_generated_execution_failure():
    @contextmanager
    def connection_factory():
        yield FailingConnection()

    result = evaluate_sql_pair("q_2", "SELECT gold", "SELECT timeout", connection_factory)

    assert result.gold_executed
    assert not result.generated_executed
    assert not result.execution_match
    assert result.generated_error_category == "timeout"


def test_empty_results_require_matching_columns():
    assert compare_results(["id"], [], ["id"], [])
    assert not compare_results(["id"], [], ["name"], [])


def test_error_categories_prefer_specific_database_errors():
    assert _error_category('column "name" does not exist') == "missing column"
    assert _error_category('relation "artist" does not exist') == "missing table"


class FakeConnection:
    def __init__(self, responses):
        self.responses = responses

    def cursor(self, row_factory=None):
        return FakeCursor(self.responses)


class FailingConnection:
    def cursor(self, row_factory=None):
        return FailingCursor()


class FakeCursor:
    def __init__(self, responses):
        self.responses = responses
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        columns, self.rows = self.responses[sql]
        self.description = [type("Description", (), {"name": name}) for name in columns]

    def fetchall(self):
        return self.rows


class FailingCursor(FakeCursor):
    def __init__(self):
        super().__init__({})

    def execute(self, sql):
        if sql == "SELECT timeout":
            raise RuntimeError("query timeout")
        self.rows = [("Engineering",)]
        self.description = [type("Description", (), {"name": "name"})]
