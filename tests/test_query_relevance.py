from contextlib import contextmanager

from app.config import Settings
from app.models import DDLBlock, SqlResponse
from app.pipeline import run_pipeline
from app.prompting import SYSTEM_PROMPT, USER_DB_SYSTEM_PROMPT


@contextmanager
def _dummy_conn():
    yield object()


def _settings():
    return Settings(max_retries=1, disable_self_correction=False, top_k=3)


def _retrieve(question: str, db: str, top_k: int) -> list[DDLBlock]:
    return [
        DDLBlock(schema_name=db, table_name="artist", ddl_text="CREATE TABLE artist (id int);"),
        DDLBlock(schema_name=db, table_name="album", ddl_text="CREATE TABLE album (id int);"),
    ]


def test_irrelevant_intent_stops_without_execution(monkeypatch):
    executed = []
    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _dummy_conn())
    monkeypatch.setattr(
        "app.pipeline.execute",
        lambda conn, sql, max_rows: executed.append(sql),
    )

    result = run_pipeline(
        "who tf is u",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: SqlResponse(
            intent="irrelevant",
            sql="",
            message="Please ask a question about the Chinook music catalogue.",
        ),
    )

    assert result.status == "failed"
    assert result.hint_code == "irrelevant_query"
    assert "Chinook" in (result.reason or "")
    assert "artist, album" in (result.hint_message or "")
    assert result.sql is None
    assert executed == []


def test_needs_detail_intent_stops_without_retry(monkeypatch):
    calls = 0

    def gen(messages):
        nonlocal calls
        calls += 1
        return SqlResponse(
            intent="needs_detail",
            sql="",
            message="Please clarify what data you want to look up.",
        )

    result = run_pipeline(
        "why is what it",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=gen,
    )

    assert calls == 1
    assert result.status == "failed"
    assert result.hint_code == "needs_detail"
    assert result.sql is None


def test_tableless_string_literal_fallback_is_blocked(monkeypatch):
    executed = []
    monkeypatch.setattr("app.pipeline.readonly_connection", lambda: _dummy_conn())
    monkeypatch.setattr(
        "app.pipeline.execute",
        lambda conn, sql, max_rows: executed.append(sql),
    )

    result = run_pipeline(
        "who tf is u",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=lambda messages: SqlResponse(
            sql="SELECT 'I am an AI assistant designed to help you.' AS response"
        ),
    )

    assert result.status == "failed"
    assert result.hint_code == "irrelevant_query"
    assert result.sql is None
    assert executed == []


def test_pure_punctuation_short_circuits_before_llm():
    called = False

    def gen(messages):
        nonlocal called
        called = True
        return SqlResponse(sql="SELECT name FROM artist")

    result = run_pipeline(
        "???",
        "chinook",
        settings=_settings(),
        retrieve=_retrieve,
        generate_sql=gen,
    )

    assert not called
    assert result.status == "failed"
    assert result.attempts == 0
    assert result.hint_code == "needs_detail"


def test_prompts_balance_permissiveness_and_relevance():
    for prompt in (SYSTEM_PROMPT, USER_DB_SYSTEM_PROMPT):
        assert "Be permissive with short, casual, or broad database questions" in prompt
        assert 'intent="irrelevant"' in prompt
        assert 'intent="needs_detail"' in prompt
