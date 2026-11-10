export interface QueryRequest {
  db: string;
  question: string;
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
  retry_count: number;
  retry_reasons?: string[];
  total_duration_ms: number;
}

export interface QueryResult {
  status: "success" | "failed";
  sql: string | null;
  table_markdown: string | null;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  attempts: number;
  reason: string | null;
  execution_trace?: ExecutionTrace | null;
}

export interface ApiError {
  detail?: string;
  execution_trace?: ExecutionTrace | null;
}
