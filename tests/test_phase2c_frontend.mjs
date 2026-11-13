import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = path.resolve(__dirname, "../frontend/src");

function createStorageMock() {
  const store = new Map();
  return {
    getItem(key) {
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      store.set(key, String(value));
    },
    removeItem(key) {
      store.delete(key);
    },
    clear() {
      store.clear();
    },
    _store: store,
  };
}

const sessionStorageMock = createStorageMock();
const forbiddenStorageProxy = new Proxy(
  {},
  {
    get() {
      throw new Error("localStorage must NEVER be accessed for DB credentials");
    },
  },
);

globalThis.window = {
  sessionStorage: sessionStorageMock,
  localStorage: forbiddenStorageProxy,
};

const {
  getDbCredential,
  hasDbCredential,
  setDbCredential,
  clearDbCredential,
  clearAllDbCredentials,
  onDbCredsChange,
} = await import("../frontend/src/dbCreds.js");

test("dbCreds mirrors to sessionStorage only and never touches localStorage", () => {
  clearAllDbCredentials();
  sessionStorageMock.clear();

  let events = 0;
  const unsub = onDbCredsChange(() => {
    events += 1;
  });

  setDbCredential("conn-1", "postgresql://ro:pw@ep-cool.neon.tech/neondb");
  assert.equal(hasDbCredential("conn-1"), true);
  assert.equal(
    getDbCredential("conn-1"),
    "postgresql://ro:pw@ep-cool.neon.tech/neondb",
  );
  assert.equal(events, 1);

  const rawSession = sessionStorageMock.getItem("schemadrill:db_creds_session");
  assert.ok(rawSession, "expected sessionStorage key to be populated");
  assert.equal(
    JSON.parse(rawSession)["conn-1"],
    "postgresql://ro:pw@ep-cool.neon.tech/neondb",
  );

  clearDbCredential("conn-1");
  assert.equal(hasDbCredential("conn-1"), false);
  assert.equal(getDbCredential("conn-1"), null);
  assert.equal(events, 2);

  setDbCredential("conn-2", "postgresql://ro:pw2@ep-cool.neon.tech/neondb");
  clearAllDbCredentials();
  assert.equal(hasDbCredential("conn-2"), false);
  assert.equal(sessionStorageMock.getItem("schemadrill:db_creds_session"), null);
  assert.equal(events, 4);

  unsub();
  setDbCredential("conn-3", "postgresql://ro:pw3@ep-cool.neon.tech/neondb");
  assert.equal(events, 4);
  clearAllDbCredentials();
});

test("storage security rules across frontend/src", () => {
  const dbCredsSrc = fs.readFileSync(path.join(FRONTEND_SRC, "dbCreds.js"), "utf8");
  assert.doesNotMatch(dbCredsSrc, /localStorage|indexedDB|document\.cookie|BroadcastChannel/i);

  const authSrc = fs.readFileSync(path.join(FRONTEND_SRC, "auth.ts"), "utf8");
  assert.match(authSrc, /clearAllDbCredentials\(\)/);
  assert.doesNotMatch(authSrc, /dbUrl|X-DB-Url|db_creds/i);

  const apiSrc = fs.readFileSync(path.join(FRONTEND_SRC, "services/api.ts"), "utf8");
  assert.match(apiSrc, /if\s*\(requestBody\.connection_id\)/);
  assert.match(apiSrc, /headers\["X-DB-Url"\]\s*=\s*encodeURIComponent/);
  assert.match(apiSrc, /db_credentials_rejected/);
  assert.match(apiSrc, /clearDbCredential/);
  // Ensure 403 handler does not trigger 401 session expiration
  const err403Block = apiSrc.slice(
    apiSrc.indexOf("db_credentials_rejected"),
    apiSrc.indexOf("db_credentials_rejected") + 400,
  );
  assert.doesNotMatch(err403Block, /notifySessionExpired/);
});

test("ConnectionsModal enforces password input rules, setup warning, and honest copy", () => {
  const modalSrc = fs.readFileSync(
    path.join(FRONTEND_SRC, "components/ConnectionsModal.jsx"),
    "utf8",
  );
  const normalizedModal = modalSrc.replaceAll("&apos;", "'").replace(/\s+/g, " ");
  assert.match(modalSrc, /type="password"/);
  assert.match(modalSrc, /autoComplete="off"/);
  assert.match(modalSrc, /setConnUrlInput\(""\)/);
  assert.match(modalSrc, /setReconnectUrlInput\(""\)/);
  assert.match(
    normalizedModal,
    /Create the role in the Neon SQL Editor, not the console\. Do not paste an owner string or a Neon API key\./,
  );
  assert.match(
    normalizedModal,
    /Your connection string stays in this tab's session and is never saved on our servers\./,
  );
  assert.match(
    normalizedModal,
    /Queries run on your Neon database and may wake it and use your compute hours\./,
  );
  assert.match(
    normalizedModal,
    /We never log query results\. Only your question, schema text and errors go to the LLM\./,
  );
});

test("App.jsx implements cross-tab clear, error routing, disabled guard, and change_script card", () => {
  const appSrc = fs.readFileSync(path.join(FRONTEND_SRC, "App.jsx"), "utf8");
  assert.match(appSrc, /clearAllDbCredentials\(\)/);
  assert.match(appSrc, /isQueryBlocked/);
  assert.match(appSrc, /db_credentials_rejected/);
  assert.match(appSrc, /db_credentials_required/);
  assert.match(appSrc, /connection_mismatch/);
  assert.match(appSrc, /schema_stale/);
  assert.match(appSrc, /schema_maybe_stale/);
  assert.match(appSrc, /byok_required/);
  assert.match(appSrc, /Neon wake-up|waking up from suspend/i);
  assert.match(appSrc, /IP Allow/);
  assert.match(appSrc, /change_script/);
  assert.match(appSrc, /Not executed\. Run it in the Neon SQL Editor\./);
});
