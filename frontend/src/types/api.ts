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

export interface QueryResult {
  status: "success" | "failed";
  sql: string | null;
  table_markdown: string | null;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  attempts: number;
  reason: string | null;
}

export interface ApiError {
  detail?: string;
}
