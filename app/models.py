from typing import Literal

from pydantic import BaseModel, Field


class SqlResponse(BaseModel):
    sql: str = Field(min_length=1)


class AuthenticatedUser(BaseModel):
    user_id: str = Field(min_length=1)
    email: str | None = None
    display_name: str | None = None


class QueryRequest(BaseModel):
    question: str = Field(
        min_length=1,
        description="The natural language question to translate to SQL.",
    )
    db: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="The database identifier.",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of relevant tables to retrieve for schema context. Max is 10.",
    )
    fk_expansion: bool = Field(
        default=False,
        description=(
            "Whether to automatically expand one-hop foreign key relationships "
            "during schema retrieval."
        ),
    )
    max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description=(
            "Maximum number of LLM self-correction retries if generated SQL fails "
            "validation. Max is 3."
        ),
    )


class PublicStageTrace(BaseModel):
    name: str
    status: Literal["pending", "completed", "passed", "failed"]
    duration_ms: float
    details: dict[str, object] = Field(default_factory=dict)


class ExecutionTraceResponse(BaseModel):
    stages: list[PublicStageTrace] = Field(default_factory=list)
    retrieved_tables: list[str] = Field(default_factory=list)
    top_k: int | None = None
    fk_expansion: bool | None = None
    execution_mode: str = "read_only"
    retry_count: int = 0
    retry_reasons: list[str] = Field(default_factory=list)
    total_duration_ms: float = 0.0


class QueryResult(BaseModel):
    status: Literal["success", "failed"]
    sql: str | None = None
    table_markdown: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, object]] = Field(default_factory=list)
    attempts: int
    reason: str | None = None
    execution_trace: ExecutionTraceResponse | None = None


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


class RetrievalResult(BaseModel):
    blocks: list[DDLBlock]
    fk_added: list[str] = Field(default_factory=list)


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
