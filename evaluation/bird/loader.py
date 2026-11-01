"""Load BIRD Mini-Dev question files into SchemaDrill benchmark records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _value(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def load_questions(path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Load a standard BIRD JSON split or a normalized SchemaDrill JSON file.

    BIRD uses ``question``, ``db_id`` and ``SQL``. The normalized form accepts
    the existing ``gold_sql`` and ``db`` field names as well.
    """
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"BIRD question file must contain a JSON list: {source}")

    questions: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"BIRD question {index} is not a JSON object")
        question = _value(item, "question", "Question")
        gold_sql = _value(item, "gold_sql", "SQL", "sql")
        db = _value(item, "db", "db_id", "database")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"BIRD question {index} is missing a question")
        if not isinstance(gold_sql, str) or not gold_sql.strip():
            raise ValueError(f"BIRD question {index} is missing SQL")
        if not isinstance(db, str) or not db.strip():
            raise ValueError(f"BIRD question {index} is missing db_id")
        questions.append(
            {
                "question_id": str(_value(item, "question_id", "id") or f"bird-{index:04d}"),
                "db": db,
                "question": question,
                "gold_sql": gold_sql,
                "difficulty": item.get("difficulty"),
                "evidence": item.get("evidence", ""),
                "dataset": "bird-mini-dev",
            }
        )
    return questions[:limit] if limit is not None else questions


def find_split(root: str | Path, split: str = "dev") -> Path:
    """Find a BIRD split using the common Mini-Dev filenames."""
    directory = Path(root)
    candidates = (
        directory / f"{split}.json",
        directory / "data" / f"{split}.json",
        directory / "mini_dev" / f"{split}.json",
        directory / "mini_dev" / f"{split}_mini.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Could not find BIRD {split!r} split; searched: {searched}")
