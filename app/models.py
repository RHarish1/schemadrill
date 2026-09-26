from typing import Literal

from pydantic import BaseModel, Field


class SqlResponse(BaseModel):
    sql: str = Field(min_length=1)


class QueryRequest(BaseModel):
    question: str = Field(min_length=1)
    db: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class QueryResult(BaseModel):
    status: Literal["success", "failed"]
    sql: str | None = None
    table_markdown: str | None = None
    attempts: int
    reason: str | None = None


class DDLBlock(BaseModel):
    schema_name: str
    table_name: str
    ddl_text: str


class PipelineFailure(Exception):
    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class RowCapExceeded(Exception):
    pass


FeedbackKind = Literal["parse_error", "dry_run_error", "row_cap"]
