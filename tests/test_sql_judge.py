import pytest
from sqlglot.errors import ParseError

from app.sql_judge import judge_sql


def test_judge_matches_equivalent_postgres_sql():
    result = judge_sql("select name from chinook.artist;", "SELECT name FROM chinook.artist")

    assert result.matches
    assert result.reason == "parsed SQL ASTs match"


def test_judge_rejects_different_parsed_sql():
    result = judge_sql(
        "SELECT name FROM chinook.artist;",
        "SELECT COUNT(*) FROM chinook.artist;",
    )

    assert not result.matches
    assert result.reason == "parsed SQL ASTs differ"


def test_judge_rejects_invalid_sql():
    with pytest.raises(ParseError):
        judge_sql("SELECT FROM", "SELECT 1")
