import re
from dataclasses import dataclass

import sqlglot


@dataclass(frozen=True)
class SqlJudgeResult:
    matches: bool
    generated_sql: str
    gold_sql: str
    reason: str


def _canonical_sql(sql: str) -> str:
    expression = sqlglot.parse_one(sql, read="postgres")
    return re.sub(r"\s+", " ", expression.sql(dialect="postgres")).strip().lower()


def judge_sql(generated_sql: str, gold_sql: str) -> SqlJudgeResult:
    generated = sqlglot.parse_one(generated_sql, read="postgres")
    gold = sqlglot.parse_one(gold_sql, read="postgres")
    matches = generated == gold
    return SqlJudgeResult(
        matches=matches,
        generated_sql=_canonical_sql(generated_sql),
        gold_sql=_canonical_sql(gold_sql),
        reason="parsed SQL ASTs match" if matches else "parsed SQL ASTs differ",
    )
