from app.models import DDLBlock
from app.prompting import build_messages, feedback


def test_feedback_templates_are_distinct():
    parse_message = feedback("parse_error", "bad syntax")
    dry_run_message = feedback("dry_run_error", "missing column")

    assert "could not be parsed" in parse_message["content"]
    assert "EXPLAIN validation" in dry_run_message["content"]
    assert parse_message != dry_run_message


def test_gemini_prompt_is_logged(caplog):
    with caplog.at_level("INFO", logger="app.prompting"):
        build_messages(
            [DDLBlock(schema_name="chinook", table_name="artist", ddl_text="CREATE TABLE artist;")],
            "List artists",
        )

    assert "gemini_prompt" in caplog.text
    assert "List artists" in caplog.text
