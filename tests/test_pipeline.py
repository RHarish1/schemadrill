from contextlib import contextmanager

import pytest
from psycopg import Error as PsycopgError

from app.models import DDLBlock, RowCapExceeded, SqlResponse
from app.pipeline import run_pipeline


class FakeDataFrame:
    def to_markdown(self, index=False):
        return "| value |\n|---|\n| 1 |"


@pytest.fixture(autouse=True)
def stub_dry_run(monkeypatch):
    monkeypatch.setattr("app.pipeline.dry_run", lambda connection, sql: None)


def _settings(max_retries=3, disable_self_correction=False):
    from app.config import Settings

    return Settings(
        max_retries=max_retries,
        disable_self_correction=disable_self_correction,
        top_k=3,
    )


def _retrieve(question, db, top_k):
    return [DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist;")]


def test_parse_error_retries_then_executes(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT FROM"), SqlResponse(sql="SELECT 1")])
    feedback_messages = []
    executed = []

    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())
    monkeypatch.setattr(
        "app.pipeline.execute",
        lambda connection, sql, max_rows: executed.append(sql) or FakeDataFrame(),
    )
    monkeypatch.setattr(
        "app.pipeline.feedback",
        lambda kind, detail: feedback_messages.append(kind) or {"role": "user", "content": detail},
    )

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: next(generated),
    )

    assert result.status == "success"
    assert result.attempts == 2
    assert feedback_messages == ["parse_error"]
    assert executed == ["SELECT 1 LIMIT 1000"]


def test_dry_run_error_retries_with_specific_feedback(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT missing"), SqlResponse(sql="SELECT 1")])
    feedback_messages = []

    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())
    monkeypatch.setattr("app.pipeline.dry_run", _dry_run_fails_once)
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: FakeDataFrame())
    monkeypatch.setattr(
        "app.pipeline.feedback",
        lambda kind, detail: feedback_messages.append(kind) or {"role": "user", "content": detail},
    )

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: next(generated),
    )

    assert result.status == "success"
    assert result.attempts == 2
    assert feedback_messages == ["dry_run_error"]


def test_retry_budget_returns_failure(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT FROM")] * 3)
    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(max_retries=3),
        retrieve=_retrieve,
        generate_sql=lambda messages: next(generated),
    )

    assert result.status == "failed"
    assert result.attempts == 3


def test_disable_self_correction_stops_after_first_failure(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT FROM"), SqlResponse(sql="SELECT 1")])
    generate_calls = []

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(max_retries=3, disable_self_correction=True),
        retrieve=_retrieve,
        generate_sql=lambda messages: generate_calls.append(messages) or next(generated),
    )

    assert result.status == "failed"
    assert result.attempts == 1
    assert len(generate_calls) == 1


def test_self_correction_enabled_preserves_second_generation(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT FROM"), SqlResponse(sql="SELECT 1")])
    generate_calls = []

    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())
    monkeypatch.setattr("app.pipeline.execute", lambda connection, sql, max_rows: FakeDataFrame())

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(max_retries=3, disable_self_correction=False),
        retrieve=_retrieve,
        generate_sql=lambda messages: generate_calls.append(messages) or next(generated),
    )

    assert result.status == "success"
    assert result.attempts == 2
    assert len(generate_calls) == 2


def test_row_cap_gets_one_regeneration_and_checks_before_execute(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT 1"), SqlResponse(sql="SELECT 1 LIMIT 1")])
    executions = []
    checks = []
    outcomes = iter(["row_cap", FakeDataFrame()])

    def execute_with_row_cap(connection, sql, max_rows):
        executions.append(sql)
        outcome = next(outcomes)
        if outcome == "row_cap":
            raise RowCapExceeded("too many")
        return outcome

    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())
    monkeypatch.setattr("app.pipeline.execute", execute_with_row_cap)

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: next(generated),
        pre_execute_check=checks.append,
    )

    assert result.status == "success"
    assert result.attempts == 2
    assert checks == ["SELECT 1 LIMIT 1000", "SELECT 1 LIMIT 1"]
    assert executions == ["SELECT 1 LIMIT 1000", "SELECT 1 LIMIT 1"]


def test_real_execution_error_does_not_regenerate(monkeypatch):
    generated = iter([SqlResponse(sql="SELECT 1")])
    generate_calls = []

    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _connection())
    monkeypatch.setattr("app.pipeline.execute", _execute_fails)

    result = run_pipeline(
        "question",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: generate_calls.append(messages) or next(generated),
    )

    assert result.status == "failed"
    assert result.attempts == 1
    assert len(generate_calls) == 1


@contextmanager
def _connection():
    yield object()


def _dry_run_fails_once(connection, sql):
    if sql.startswith("SELECT missing"):
        raise PsycopgError("missing column")


def _execute_fails(connection, sql, max_rows):
    raise PsycopgError("connection failed")
