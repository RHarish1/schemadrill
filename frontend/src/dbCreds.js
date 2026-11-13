const SESSION_STORAGE_KEY = "schemadrill:db_creds_session";

/** @type {Record<string, string> | null} */
let inMemoryCreds = null;
/** @type {Set<() => void>} */
const changeListeners = new Set();

function notifyChange() {
  for (const listener of changeListeners) {
    listener();
  }
}

function readFromSessionStorage() {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    /** @type {Record<string, string>} */
    const clean = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof k === "string" && typeof v === "string" && v.trim()) {
        clean[k] = v;
      }
    }
    return clean;
  } catch {
    return {};
  }
}

function writeToSessionStorage(creds) {
  if (typeof window === "undefined") return;
  try {
    if (!creds || Object.keys(creds).length === 0) {
      window.sessionStorage.removeItem(SESSION_STORAGE_KEY);
    } else {
      window.sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(creds));
    }
  } catch {
    // Ignore sessionStorage failures in restricted browser contexts.
  }
}

function ensureHydrated() {
  if (inMemoryCreds !== null) return inMemoryCreds;
  inMemoryCreds = readFromSessionStorage();
  return inMemoryCreds;
}

export function getDbCredential(connectionId) {
  if (!connectionId) return null;
  return ensureHydrated()[connectionId] ?? null;
}

export function hasDbCredential(connectionId) {
  return Boolean(getDbCredential(connectionId));
}

export function setDbCredential(connectionId, dbUrl) {
  const cleanUrl = (dbUrl || "").trim();
  if (!connectionId || !cleanUrl) {
    clearDbCredential(connectionId);
    return;
  }
  const current = { ...ensureHydrated() };
  current[connectionId] = cleanUrl;
  inMemoryCreds = current;
  writeToSessionStorage(inMemoryCreds);
  notifyChange();
}

export function clearDbCredential(connectionId) {
  if (!connectionId) return;
  const current = { ...ensureHydrated() };
  if (!(connectionId in current)) return;
  delete current[connectionId];
  inMemoryCreds = current;
  writeToSessionStorage(inMemoryCreds);
  notifyChange();
}

export function clearAllDbCredentials() {
  inMemoryCreds = {};
  writeToSessionStorage(inMemoryCreds);
  notifyChange();
}

export function onDbCredsChange(listener) {
  changeListeners.add(listener);
  return () => {
    changeListeners.delete(listener);
  };
}
