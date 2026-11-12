import type {
  DatasetCatalogResponse,
  ExecutionTrace,
  LlmProvider,
  QueryRequest,
  QueryResult,
  UserProfile,
  ValidateKeyResponse,
} from "../types/api";
import { authClient, getAuthToken, notifySessionExpired } from "../auth";
import {
  clearLlmKey,
  getLlmApiKey,
  getLlmProvider,
  getSelectedFallbackModel,
  getSelectedLlmModel,
} from "../llmKey";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const REQUEST_TIMEOUT_MS = 30_000;
const SESSION_EXPIRED_MESSAGE = "Your session expired. Sign in with Google to continue.";

const invalidLlmKeyListeners = new Set<(detail: string) => void>();
const rateLimitRemainingListeners = new Set<(remaining: number) => void>();

export function onInvalidLlmKey(listener: (detail: string) => void): () => void {
  invalidLlmKeyListeners.add(listener);
  return () => {
    invalidLlmKeyListeners.delete(listener);
  };
}

function notifyInvalidLlmKey(detail: string): void {
  for (const listener of invalidLlmKeyListeners) {
    listener(detail);
  }
}

export function onRateLimitRemaining(listener: (remaining: number) => void): () => void {
  rateLimitRemainingListeners.add(listener);
  return () => {
    rateLimitRemainingListeners.delete(listener);
  };
}

function notifyRateLimitRemaining(remaining: number): void {
  for (const listener of rateLimitRemainingListeners) {
    listener(remaining);
  }
}

class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly technicalDetails?: string,
    public readonly status?: number,
    public readonly executionTrace?: ExecutionTrace,
    public readonly code?: string,
    public readonly rateLimitRemaining?: number,
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

async function request<T>(
  path: string,
  init?: RequestInit,
  includeAuth = true,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
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
    const rawRemaining = response.headers.get("X-RateLimit-Remaining");
    const parsedRemaining =
      rawRemaining !== null && /^\d+$/.test(rawRemaining) ? Number(rawRemaining) : undefined;
    if (parsedRemaining !== undefined) {
      notifyRateLimitRemaining(parsedRemaining);
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
      const code =
        payload && typeof payload === "object" && "code" in payload && typeof payload.code === "string"
          ? payload.code
          : undefined;
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

      if (response.status === 403 && code === "invalid_llm_key") {
        clearLlmKey();
        notifyInvalidLlmKey(detail);
      }

      throw new ApiRequestError(
        detail,
        JSON.stringify(payload),
        response.status,
        executionTrace,
        code,
        parsedRemaining,
      );
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
  const maxRetries = requestBody.max_retries ?? 1;
  const timeoutMs = Math.max(REQUEST_TIMEOUT_MS, (1 + maxRetries) * 35_000, 95_000);
  const apiKey = getLlmApiKey();
  const provider = getLlmProvider();
  const selectedModel = getSelectedLlmModel();
  const selectedFallback = getSelectedFallbackModel();

  const headers: Record<string, string> = {};
  const body: QueryRequest = { ...requestBody };
  if (apiKey) {
    headers["X-LLM-Api-Key"] = apiKey;
    headers["X-LLM-Provider"] = provider;
    if (selectedModel) {
      body.model = selectedModel;
    }
    if (selectedFallback && selectedFallback !== selectedModel) {
      body.fallback_model = selectedFallback;
    }
  }

  return request<QueryResult>(
    "/query",
    {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    },
    true,
    timeoutMs,
  );
}

export function validateLlmKey(apiKey: string, provider: LlmProvider = "gemini") {
  return request<ValidateKeyResponse>(
    "/api/llm/validate",
    {
      method: "POST",
      headers: {
        "X-LLM-Api-Key": apiKey.trim(),
        "X-LLM-Provider": provider,
      },
    },
    true,
    15_000,
  );
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
