from typing import Any

from pydantic import BaseModel, Field


class SQLEvalResult(BaseModel):
    question_id: str
    gold_sql: str
    generated_sql: str | None
    gold_executed: bool
    generated_executed: bool
    gold_error: str | None = None
    generated_error: str | None = None
    gold_error_category: str | None = None
    generated_error_category: str | None = None
    execution_match: bool = False
    ast_match: bool | None = None
    gold_row_count: int | None = None
    generated_row_count: int | None = None
    gold_columns: list[str] | None = None
    generated_columns: list[str] | None = None


class SQLEvalSummary(BaseModel):
    dataset: str
    total_examples: int = Field(ge=0)
    execution_accuracy: float = Field(ge=0, le=1)
    execution_success_rate: float = Field(ge=0, le=1)
    ast_match_rate: float = Field(ge=0, le=1)
    execution_failures: int = Field(ge=0)
    result_mismatches: int = Field(ge=0)
    records: list[SQLEvalResult] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
