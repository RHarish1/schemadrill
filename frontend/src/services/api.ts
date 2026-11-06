import type { QueryRequest, QueryResult } from "../types/api";
import type { DatasetCatalogResponse } from "../types/api";
import { getAuthToken } from "../auth";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const REQUEST_TIMEOUT_MS = 30_000;

class ApiRequestError extends Error {
  constructor(message: string, public readonly technicalDetails?: string) {
    super(message);
    this.name = "ApiRequestError";
  }
}

async function request<T>(path: string, init?: RequestInit, includeAuth = true): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const token = includeAuth ? await getAuthToken() : null;
    const response = await fetch(`${API_BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? String(payload.detail)
          : `Request failed with status ${response.status}.`;
      throw new ApiRequestError(detail, JSON.stringify(payload));
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiRequestError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiRequestError("The request timed out. Please try again.");
    }
    throw new ApiRequestError("The API could not be reached.", String(error));
  } finally {
    window.clearTimeout(timeout);
  }
}

export function queryDatabase(requestBody: QueryRequest) {
  return request<QueryResult>("/query", {
    method: "POST",
    body: JSON.stringify(requestBody),
  });
}

export function checkHealth() {
  return fetch(`${API_BASE_URL}/health`, {
    headers: { Accept: "application/json" },
  }).then(async (response) => {
    if (!response.ok) {
      throw new ApiRequestError(`Request failed with status ${response.status}.`);
    }
    return (await response.json()) as { status: string };
  });
}

export function listDatasets() {
  return request<DatasetCatalogResponse>("/api/datasets");
}

export { ApiRequestError };
