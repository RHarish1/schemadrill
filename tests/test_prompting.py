from app.models import DDLBlock, RetryState
from app.prompting import build_messages, estimate_prompt_tokens, feedback, messages_for_retry


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


def test_retry_prompt_token_count_does_not_accumulate_history():
    ddl = [DDLBlock(schema_name="chinook", table_name="artist", ddl_text="CREATE TABLE artist;")]
    attempt_one = messages_for_retry(
        RetryState(
            question="List artists",
            retrieved_ddl=ddl,
            attempt=1,
            last_sql="SELECT foo FROM artist",
            last_error="column does not exist",
        )
    )
    attempt_three = messages_for_retry(
        RetryState(
            question="List artists",
            retrieved_ddl=ddl,
            attempt=3,
            last_sql="SELECT bar FROM artist",
            last_error="column does not exist",
        )
    )

    assert estimate_prompt_tokens(attempt_three) <= estimate_prompt_tokens(attempt_one)
