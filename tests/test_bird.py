import json

import pytest

from evaluation.bird.loader import find_split, load_questions


def test_loads_standard_bird_shape(tmp_path):
    path = tmp_path / "dev.json"
    path.write_text(
        json.dumps(
            [
                {
                    "question": "How many users?",
                    "evidence": "Count users",
                    "SQL": "SELECT COUNT(*) FROM users",
                    "db_id": "analytics",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert load_questions(path) == [
        {
            "question_id": "bird-0001",
            "db": "analytics",
            "question": "How many users?",
            "gold_sql": "SELECT COUNT(*) FROM users",
            "difficulty": None,
            "evidence": "Count users",
            "dataset": "bird-mini-dev",
        }
    ]


def test_loads_normalized_shape_and_limit(tmp_path):
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps(
            [
                {"question_id": "a", "question": "A", "gold_sql": "SELECT 1", "db": "a"},
                {"question_id": "b", "question": "B", "gold_sql": "SELECT 2", "db": "b"},
            ]
        ),
        encoding="utf-8",
    )

    assert [item["question_id"] for item in load_questions(path, limit=1)] == ["a"]


def test_rejects_missing_sql(tmp_path):
    path = tmp_path / "dev.json"
    path.write_text(json.dumps([{"question": "A", "db_id": "a"}]), encoding="utf-8")

    with pytest.raises(ValueError, match="missing SQL"):
        load_questions(path)


def test_finds_common_split_location(tmp_path):
    nested = tmp_path / "data"
    nested.mkdir()
    expected = nested / "dev.json"
    expected.write_text("[]", encoding="utf-8")

    assert find_split(tmp_path) == expected
