from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SqlResponse(BaseModel):
    sql: str = Field(min_length=1)
    intent: Literal["read", "change"] = "read"


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
    model: str | None = Field(
        default=None,
        min_length=1,
        description="Optional model ID when using your own API key (BYOK).",
    )
    fallback_model: str | None = Field(
        default=None,
        min_length=1,
        description="Optional fallback model ID when using your own API key (BYOK).",
    )
    connection_id: str | None = Field(
        default=None,
        min_length=1,
        description="Optional UUID of a connected Neon database.",
    )


class ModelInfoResponse(BaseModel):
    id: str
    display_name: str


class ValidateKeyResponse(BaseModel):
    valid: bool
    models: list[ModelInfoResponse] = Field(default_factory=list)


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
    dataset_kind: Literal["demo", "user_db"] = "demo"
    intent: Literal["read", "change"] = "read"
    tier: Literal["free", "byok"] | None = None
    model: str | None = None
    fallback_used: bool = False
    retry_count: int = 0
    retry_reasons: list[str] = Field(default_factory=list)
    total_duration_ms: float = 0.0


class QueryResult(BaseModel):
    status: Literal["success", "failed"]
    kind: Literal["read", "change_script"] = "read"
    executed: bool = True
    sql: str | None = None
    table_markdown: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, object]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    message: str | None = None
    attempts: int
    reason: str | None = None
    hint_code: str | None = None
    hint_message: str | None = None
    execution_trace: ExecutionTraceResponse | None = None


class SQLGuardConfig(BaseModel):
    max_limit: int = Field(default=1000, gt=0)
    blocked_functions: set[str] = Field(default_factory=set)
    mode: Literal["demo", "user_db"] = "demo"
    allow_change_scripts: bool = False
    max_change_statements: int = Field(default=10, gt=0)


class SQLGuardResult(BaseModel):
    allowed: bool
    sql: str | None = None
    statement_type: str
    intent: Literal["read", "change"] = "read"
    referenced_tables: list[str] = Field(default_factory=list)
    blocked_functions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
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


class ByteCapExceeded(RowCapExceeded):
    pass


FeedbackKind = Literal["parse_error", "dry_run_error", "row_cap", "sql_guard"]


class InspectConnectionResponse(BaseModel):
    schemas: list[dict[str, Any]] = Field(
        description="List of schemas and table counts: [{'schema_name': str, 'table_count': int}]"
    )


class CreateConnectionRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    schemas: list[str] = Field(min_length=1)


class ConnectionResponse(BaseModel):
    id: str
    label: str
    host: str
    database: str
    role_name: str
    schemas: list[str]
    ingest_status: str
    table_count: int
    ingested_at: datetime | None = None
    expires_at: datetime
    created_at: datetime
    is_expired: bool
    needs_credentials: bool = True
