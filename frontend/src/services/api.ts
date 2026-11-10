import type {
  DatasetCatalogResponse,
  ExecutionTrace,
  QueryRequest,
  QueryResult,
  UserProfile,
} from "../types/api";
import { authClient, getAuthToken, notifySessionExpired } from "../auth";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const REQUEST_TIMEOUT_MS = 30_000;
const SESSION_EXPIRED_MESSAGE = "Your session expired. Sign in with Google to continue.";

class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly technicalDetails?: string,
    public readonly status?: number,
    public readonly executionTrace?: ExecutionTrace,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

function sendAuthenticatedFetch(
  path: string,
  init: RequestInit | undefined,
  signal: AbortSignal,
  token: string | null,
): Promise<Response> {
  return fetch(`${API_BASE_URL}${path}`, {
    ...init,
    signal,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers,
    },
  });
}

async function request<T>(path: string, init?: RequestInit, includeAuth = true): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    let token: string | null = null;
    if (includeAuth) {
      token = await getAuthToken();
      if (authClient && !token) {
        notifySessionExpired();
        throw new ApiRequestError(SESSION_EXPIRED_MESSAGE, undefined, 401);
      }
    }
    let response = await sendAuthenticatedFetch(path, init, controller.signal, token);
    if (includeAuth && authClient && response.status === 401) {
      const refreshedToken = await getAuthToken({ forceRefresh: true });
      if (refreshedToken && refreshedToken !== token) {
        response = await sendAuthenticatedFetch(path, init, controller.signal, refreshedToken);
      }
    }
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      if (includeAuth && response.status === 401) {
        notifySessionExpired();
        throw new ApiRequestError(
          SESSION_EXPIRED_MESSAGE,
          payload ? JSON.stringify(payload) : undefined,
          401,
        );
      }
      const detail =
        payload && typeof payload === "object" && "detail" in payload
          ? String(payload.detail)
          : `Request failed with status ${response.status}.`;
      const executionTrace =
        payload &&
        typeof payload === "object" &&
        "execution_trace" in payload &&
        payload.execution_trace &&
        typeof payload.execution_trace === "object"
          ? (payload.execution_trace as ExecutionTrace)
          : undefined;
      throw new ApiRequestError(detail, JSON.stringify(payload), response.status, executionTrace);
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
      throw new ApiRequestError(
        `Request failed with status ${response.status}.`,
        undefined,
        response.status,
      );
    }
    return (await response.json()) as { status: string };
  });
}

export function listDatasets() {
  return request<DatasetCatalogResponse>("/api/datasets");
}

export function getCurrentUser() {
  return request<UserProfile>("/api/me");
}

export { ApiRequestError };
