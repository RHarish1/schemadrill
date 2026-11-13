export type LlmProvider = "gemini";

export interface LlmModelInfo {
  id: string;
  display_name: string;
}

export interface ValidateKeyResponse {
  valid: boolean;
  models: LlmModelInfo[];
}

export interface QueryRequest {
  db: string;
  question: string;
  top_k?: number;
  fk_expansion?: boolean;
  max_retries?: number;
  model?: string;
  fallback_model?: string;
  connection_id?: string;
}

export interface DatasetOption {
  id: string;
  label: string;
  description: string;
  examples: string[];
}

export interface DatasetCatalogResponse {
  datasets: DatasetOption[];
}

export interface UserProfile {
  user_id: string;
  email: string | null;
  display_name: string | null;
}

export type StageStatus = "pending" | "completed" | "passed" | "failed";

export interface PublicStageTrace {
  name: string;
  status: StageStatus;
  duration_ms: number;
  details?: Record<string, unknown>;
}

export interface ExecutionTrace {
  stages: PublicStageTrace[];
  retrieved_tables: string[];
  top_k?: number | null;
  fk_expansion?: boolean | null;
  execution_mode?: string;
  dataset_kind?: "demo" | "user_db";
  intent?: "read" | "change";
  tier?: "free" | "byok" | null;
  model?: string | null;
  fallback_used?: boolean;
  retry_count: number;
  retry_reasons?: string[];
  total_duration_ms: number;
}

export interface QueryResult {
  status: "success" | "failed";
  kind?: "read" | "change_script";
  executed?: boolean;
  sql: string | null;
  table_markdown: string | null;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  warnings?: string[];
  message?: string | null;
  attempts: number;
  reason: string | null;
  hint_code?: string | null;
  hint_message?: string | null;
  execution_trace?: ExecutionTrace | null;
}

export interface SchemaTableSummary {
  schema_name: string;
  table_count: number;
}

export interface InspectConnectionResponse {
  schemas: SchemaTableSummary[];
}

export interface CreateConnectionRequest {
  label: string;
  schemas: string[];
}

export interface TenantConnectionSummary {
  id: string;
  label: string;
  host: string;
  database: string;
  role_name: string;
  schemas: string[];
  ingest_status: "running" | "ready" | "failed";
  table_count: number;
  ingested_at: string | null;
  expires_at: string;
  created_at: string;
  is_expired: boolean;
  needs_credentials: boolean;
}

export interface ApiError {
  detail?: string;
  execution_trace?: ExecutionTrace | null;
}
