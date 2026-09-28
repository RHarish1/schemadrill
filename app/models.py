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


class SQLGuardConfig(BaseModel):
    max_limit: int = Field(default=1000, gt=0)
    blocked_functions: set[str] = Field(default_factory=set)


class SQLGuardResult(BaseModel):
    allowed: bool
    sql: str | None = None
    statement_type: str
    referenced_tables: list[str] = Field(default_factory=list)
    blocked_functions: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    user_facing_message: str | None = None
    limit_injected: bool = False


class DDLBlock(BaseModel):
    schema_name: str
    table_name: str
    ddl_text: str


class RetryState(BaseModel):
    question: str = Field(min_length=1)
    retrieved_ddl: list[DDLBlock] = Field(default_factory=list)
    attempt: int = Field(ge=1)
    last_sql: str | None = None
    last_error: str | None = None


class PipelineFailure(Exception):
    def __init__(self, reason: str, attempts: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts


class RowCapExceeded(Exception):
    pass


FeedbackKind = Literal["parse_error", "dry_run_error", "row_cap", "sql_guard"]
