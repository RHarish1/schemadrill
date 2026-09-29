from typing import Any

from pydantic import BaseModel, Field, model_validator


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
    result_match: bool = False
    execution_match: bool | None = None
    ast_match: bool | None = None
    gold_row_count: int | None = None
    generated_row_count: int | None = None
    gold_columns: list[str] | None = None
    generated_columns: list[str] | None = None
    gold_execution_time_ms: float | None = None
    generated_execution_time_ms: float | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_match(cls, values: Any) -> Any:
        if isinstance(values, dict) and "result_match" not in values:
            values["result_match"] = values.get("execution_match", False)
        return values

    @model_validator(mode="after")
    def sync_legacy_match(self) -> "SQLEvalResult":
        self.execution_match = self.result_match
        return self


class SQLEvalSummary(BaseModel):
    dataset: str
    total_examples: int = Field(ge=0)
    result_set_accuracy: float = Field(default=0, ge=0, le=1)
    execution_accuracy: float | None = None
    execution_success_rate: float = Field(ge=0, le=1)
    ast_match_rate: float = Field(ge=0, le=1)
    execution_failures: int = Field(ge=0)
    result_mismatches: int = Field(ge=0)
    records: list[SQLEvalResult] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_accuracy(cls, values: Any) -> Any:
        if isinstance(values, dict) and "result_set_accuracy" not in values:
            values["result_set_accuracy"] = values.get("execution_accuracy", 0)
        return values

    @model_validator(mode="after")
    def sync_legacy_accuracy(self) -> "SQLEvalSummary":
        self.execution_accuracy = self.result_set_accuracy
        return self
