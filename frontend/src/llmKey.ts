import type { LlmModelInfo, LlmProvider } from "./types/api";

const SESSION_STORAGE_KEY = "schemadrill:byok_session";

interface StoredKeyState {
  apiKey: string;
  provider: LlmProvider;
  model: string;
  fallbackModel: string;
  models: LlmModelInfo[];
}

let inMemoryState: StoredKeyState | null = null;
const changeListeners = new Set<() => void>();

function notifyChange(): void {
  for (const listener of changeListeners) {
    listener();
  }
}

function readFromSessionStorage(): StoredKeyState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredKeyState>;
    if (typeof parsed.apiKey !== "string" || !parsed.apiKey.trim()) {
      return null;
    }
    return {
      apiKey: parsed.apiKey,
      provider: parsed.provider === "gemini" ? "gemini" : "gemini",
      model: typeof parsed.model === "string" ? parsed.model : "",
      fallbackModel: typeof parsed.fallbackModel === "string" ? parsed.fallbackModel : "",
      models: Array.isArray(parsed.models) ? parsed.models : [],
    };
  } catch {
    return null;
  }
}

function writeToSessionStorage(state: StoredKeyState | null): void {
  if (typeof window === "undefined") return;
  try {
    if (!state) {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(state));
    }
  } catch {
    // Ignore sessionStorage failures in restricted browser contexts.
  }
}

function ensureHydrated(): StoredKeyState | null {
  if (inMemoryState !== null) return inMemoryState;
  inMemoryState = readFromSessionStorage();
  return inMemoryState;
}

export function getLlmApiKey(): string | null {
  return ensureHydrated()?.apiKey ?? null;
}

export function getLlmProvider(): LlmProvider {
  return ensureHydrated()?.provider ?? "gemini";
}

export function getSelectedLlmModel(): string {
  return ensureHydrated()?.model ?? "";
}

export function getSelectedFallbackModel(): string {
  return ensureHydrated()?.fallbackModel ?? "";
}

export function getAvailableLlmModels(): LlmModelInfo[] {
  return ensureHydrated()?.models ?? [];
}

export function getKeyLast4(key?: string | null): string | null {
  const value = key ?? getLlmApiKey();
  if (!value) return null;
  return value.length >= 4 ? value.slice(-4) : value;
}

export function setLlmKeySession(params: {
  apiKey: string;
  provider?: LlmProvider;
  models: LlmModelInfo[];
  model?: string;
  fallbackModel?: string;
}): void {
  const cleanKey = params.apiKey.trim();
  if (!cleanKey) {
    clearLlmKey();
    return;
  }
  const defaultModel = params.model || params.models[0]?.id || "";
  inMemoryState = {
    apiKey: cleanKey,
    provider: params.provider ?? "gemini",
    model: defaultModel,
    fallbackModel: params.fallbackModel ?? "",
    models: params.models,
  };
  writeToSessionStorage(inMemoryState);
  notifyChange();
}

export function updateLlmModelSelection(model: string, fallbackModel: string): void {
  const current = ensureHydrated();
  if (!current) return;
  inMemoryState = {
    ...current,
    model,
    fallbackModel: fallbackModel === model ? "" : fallbackModel,
  };
  writeToSessionStorage(inMemoryState);
  notifyChange();
}

export function clearLlmKey(): void {
  inMemoryState = null;
  writeToSessionStorage(null);
  notifyChange();
}

export function onLlmKeyChange(listener: () => void): () => void {
  changeListeners.add(listener);
  return () => {
    changeListeners.delete(listener);
  };
}
