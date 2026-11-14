import type { LlmModelInfo, LlmProvider } from "./types/api";

const SESSION_STORAGE_KEY = "schemadrill:byok_session";

/** Non-sensitive model and provider preferences persisted to sessionStorage. */
interface StoredPreferences {
  provider: LlmProvider;
  model: string;
  fallbackModel: string;
  models: LlmModelInfo[];
}

/** Active in-memory state. Sensitive apiKey is held in closure memory only (CWE-312 mitigation). */
interface ActiveKeyState extends StoredPreferences {
  apiKey: string;
}

let inMemoryState: ActiveKeyState | null = null;
let inMemoryPreferences: StoredPreferences | null = null;
const changeListeners = new Set<() => void>();

function notifyChange(): void {
  for (const listener of changeListeners) {
    listener();
  }
}

function readFromSessionStorage(): StoredPreferences | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredPreferences>;
    return {
      provider: parsed.provider === "gemini" ? "gemini" : "gemini",
      model: typeof parsed.model === "string" ? parsed.model : "",
      fallbackModel: typeof parsed.fallbackModel === "string" ? parsed.fallbackModel : "",
      models: Array.isArray(parsed.models) ? parsed.models : [],
    };
  } catch {
    return null;
  }
}

function writeToSessionStorage(prefs: StoredPreferences | null): void {
  if (typeof window === "undefined") return;
  try {
    if (!prefs) {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } else {
      // Store non-sensitive preferences only; apiKey is never persisted to web storage.
      window.sessionStorage.setItem(
        SESSION_STORAGE_KEY,
        JSON.stringify({
          provider: prefs.provider,
          model: prefs.model,
          fallbackModel: prefs.fallbackModel,
          models: prefs.models,
        })
      );
    }
  } catch {
    // Ignore sessionStorage failures in restricted browser contexts.
  }
}

function ensurePreferencesHydrated(): StoredPreferences | null {
  if (inMemoryPreferences !== null) return inMemoryPreferences;
  inMemoryPreferences = readFromSessionStorage();
  return inMemoryPreferences;
}

export function getLlmApiKey(): string | null {
  return inMemoryState?.apiKey ?? null;
}

export function getLlmProvider(): LlmProvider {
  return inMemoryState?.provider ?? ensurePreferencesHydrated()?.provider ?? "gemini";
}

export function getSelectedLlmModel(): string {
  return inMemoryState?.model ?? ensurePreferencesHydrated()?.model ?? "";
}

export function getSelectedFallbackModel(): string {
  return inMemoryState?.fallbackModel ?? ensurePreferencesHydrated()?.fallbackModel ?? "";
}

export function getAvailableLlmModels(): LlmModelInfo[] {
  return inMemoryState?.models ?? ensurePreferencesHydrated()?.models ?? [];
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
  const preferences: StoredPreferences = {
    provider: params.provider ?? "gemini",
    model: defaultModel,
    fallbackModel: params.fallbackModel ?? "",
    models: params.models,
  };
  inMemoryState = {
    ...preferences,
    apiKey: cleanKey,
  };
  inMemoryPreferences = preferences;
  writeToSessionStorage(preferences);
  notifyChange();
}

export function updateLlmModelSelection(model: string, fallbackModel: string): void {
  const currentPrefs = inMemoryPreferences ?? ensurePreferencesHydrated();
  const updatedPrefs: StoredPreferences = {
    provider: currentPrefs?.provider ?? "gemini",
    model,
    fallbackModel: fallbackModel === model ? "" : fallbackModel,
    models: currentPrefs?.models ?? [],
  };
  if (inMemoryState) {
    inMemoryState = {
      ...inMemoryState,
      ...updatedPrefs,
    };
  }
  inMemoryPreferences = updatedPrefs;
  writeToSessionStorage(updatedPrefs);
  notifyChange();
}

export function clearLlmKey(): void {
  inMemoryState = null;
  inMemoryPreferences = null;
  writeToSessionStorage(null);
  notifyChange();
}

export function onLlmKeyChange(listener: () => void): () => void {
  changeListeners.add(listener);
  return () => {
    changeListeners.delete(listener);
  };
}
