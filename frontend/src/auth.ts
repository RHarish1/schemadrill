import { createAuthClient } from "@neondatabase/auth";
import { BetterAuthReactAdapter } from "@neondatabase/auth/react/adapters";

const authUrl = import.meta.env.VITE_NEON_AUTH_URL;
const AUTH_CHANNEL_NAME = "schemadrill:auth";
const DRAFT_QUERY_STORAGE_KEY = "schemadrill:draft_query";

export interface AuthUser {
  id: string;
  email?: string | null;
  name?: string | null;
  image?: string | null;
}

export interface AuthSession {
  token?: string;
  expiresAt?: string | Date;
}

export interface AuthSessionData {
  session: AuthSession;
  user: AuthUser;
}

export type CrossTabAuthEvent = "SIGNED_IN" | "SIGNED_OUT";

export const authClient = authUrl
  ? createAuthClient(authUrl, { adapter: BetterAuthReactAdapter() })
  : null;

const sessionExpiredListeners = new Set<() => void>();

export function onSessionExpired(listener: () => void): () => void {
  sessionExpiredListeners.add(listener);
  return () => {
    sessionExpiredListeners.delete(listener);
  };
}

export function notifySessionExpired(): void {
  for (const listener of sessionExpiredListeners) {
    listener();
  }
}

export function broadcastAuthEvent(event: CrossTabAuthEvent): void {
  if (typeof BroadcastChannel === "undefined") return;
  try {
    const channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    channel.postMessage({ type: event });
    channel.close();
  } catch {
    // Ignore storage/channel restrictions in constrained browser contexts.
  }
}

export function onCrossTabAuthEvent(listener: (event: CrossTabAuthEvent) => void): () => void {
  if (typeof BroadcastChannel === "undefined") return () => {};
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    channel.onmessage = (message) => {
      const eventType = message.data?.type;
      if (eventType === "SIGNED_IN" || eventType === "SIGNED_OUT") {
        listener(eventType);
      }
    };
  } catch {
    return () => {};
  }
  return () => {
    channel?.close();
  };
}

function isExpiredTimestamp(expiresAt?: string | Date): boolean {
  if (!expiresAt) return false;
  const timestamp = expiresAt instanceof Date ? expiresAt.getTime() : new Date(expiresAt).getTime();
  if (Number.isNaN(timestamp)) return false;
  return timestamp <= Date.now();
}

function looksLikeJwt(token: string | null | undefined): token is string {
  return Boolean(token && token.split(".").length === 3);
}

export async function fetchAuthSession(options?: {
  forceRefresh?: boolean;
}): Promise<AuthSessionData | null> {
  if (!authClient) return null;
  try {
    const { data, error } = await authClient.getSession(
      options?.forceRefresh
        ? { fetchOptions: { headers: { "X-Force-Fetch": "true" } } }
        : undefined,
    );
    if (error || !data?.session || !data?.user) return null;
    if (isExpiredTimestamp(data.session.expiresAt)) return null;
    return {
      session: data.session,
      user: data.user,
    };
  } catch {
    return null;
  }
}

export async function getAuthToken(options?: { forceRefresh?: boolean }): Promise<string | null> {
  if (!authClient) return null;
  const sessionData = await fetchAuthSession(options);
  if (!sessionData) return null;
  const sessionToken = sessionData.session.token ?? null;
  if (looksLikeJwt(sessionToken)) return sessionToken;
  try {
    const { data } = await authClient.token();
    return data?.token ?? sessionToken;
  } catch {
    return sessionToken;
  }
}

export async function signOutUser(): Promise<void> {
  clearDraftQuery();
  try {
    await authClient?.signOut();
  } finally {
    broadcastAuthEvent("SIGNED_OUT");
  }
}

export interface DraftQuery {
  question: string;
  dataset: string;
  topK?: number;
  fkExpansion?: boolean;
  maxRetries?: number;
}

export function saveDraftQuery(draft: DraftQuery): void {
  if (typeof window === "undefined") return;
  const cleanQuestion = draft.question.trim();
  if (!cleanQuestion && !draft.dataset) return;
  try {
    window.sessionStorage.setItem(
      DRAFT_QUERY_STORAGE_KEY,
      JSON.stringify({
        question: draft.question,
        dataset: draft.dataset,
        ...(typeof draft.topK === "number" ? { topK: draft.topK } : {}),
        ...(typeof draft.fkExpansion === "boolean" ? { fkExpansion: draft.fkExpansion } : {}),
        ...(typeof draft.maxRetries === "number" ? { maxRetries: draft.maxRetries } : {}),
      }),
    );
  } catch {
    // Ignore sessionStorage failures.
  }
}

export function consumeDraftQuery(): DraftQuery | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(DRAFT_QUERY_STORAGE_KEY);
    if (!raw) return null;
    window.sessionStorage.removeItem(DRAFT_QUERY_STORAGE_KEY);
    const parsed = JSON.parse(raw) as Partial<DraftQuery>;
    return {
      question: typeof parsed.question === "string" ? parsed.question : "",
      dataset: typeof parsed.dataset === "string" ? parsed.dataset : "",
      ...(typeof parsed.topK === "number" ? { topK: parsed.topK } : {}),
      ...(typeof parsed.fkExpansion === "boolean" ? { fkExpansion: parsed.fkExpansion } : {}),
      ...(typeof parsed.maxRetries === "number" ? { maxRetries: parsed.maxRetries } : {}),
    };
  } catch {
    return null;
  }
}

export function clearDraftQuery(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(DRAFT_QUERY_STORAGE_KEY);
  } catch {
    // Ignore sessionStorage failures.
  }
}
