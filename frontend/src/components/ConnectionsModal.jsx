import { useEffect, useState } from "react";
import {
  ApiRequestError,
  createDbConnection,
  deleteDbConnection,
  inspectDbConnection,
  reingestDbConnection,
} from "../services/api";
import {
  getDbCredential,
  hasDbCredential,
  setDbCredential,
} from "../dbCreds";

const READONLY_ROLE_SQL = `CREATE ROLE schemadrill_reader WITH LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE neondb TO schemadrill_reader;
GRANT USAGE ON SCHEMA public TO schemadrill_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO schemadrill_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO schemadrill_reader;`;

const REVOKE_ROLE_SQL = `REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM schemadrill_reader;
REVOKE USAGE ON SCHEMA public FROM schemadrill_reader;
REVOKE CONNECT ON DATABASE neondb FROM schemadrill_reader;
DROP ROLE IF EXISTS schemadrill_reader;`;

function formatTimestamp(value) {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return String(value);
  }
}

export default function ConnectionsModal({
  open,
  onClose,
  connections,
  onConnectionsChanged,
  initialReconnectId = null,
  initialError = null,
}) {
  const [connUrlInput, setConnUrlInput] = useState("");
  const [labelInput, setLabelInput] = useState("");
  const [inspecting, setInspecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [inspectedSchemas, setInspectedSchemas] = useState(null);
  const [selectedSchemas, setSelectedSchemas] = useState([]);
  const [inspectedUrl, setInspectedUrl] = useState("");
  const [flowError, setFlowError] = useState(initialError);
  const [flowNotice, setFlowNotice] = useState(null);

  const [reconnectTargetId, setReconnectTargetId] = useState(initialReconnectId);
  const [reconnectUrlInput, setReconnectUrlInput] = useState("");
  const [reconnecting, setReconnecting] = useState(false);
  const [copiedSql, setCopiedSql] = useState(null);
  const [guideOpen, setGuideOpen] = useState(false);

  useEffect(() => {
    if (open) {
      setFlowError(initialError);
      setFlowNotice(null);
      setReconnectTargetId(initialReconnectId);
      setReconnectUrlInput("");
      if (!initialReconnectId) {
        setConnUrlInput("");
        setInspectedUrl("");
        setInspectedSchemas(null);
        setSelectedSchemas([]);
        setLabelInput("");
      }
    }
  }, [open, initialError, initialReconnectId]);

  if (!open) return null;

  async function copySnippet(kind, text) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedSql(kind);
      window.setTimeout(() => setCopiedSql(null), 1600);
    } catch {
      // Ignore clipboard failures.
    }
  }

  async function handleInspect(event) {
    event.preventDefault();
    const trimmed = connUrlInput.trim();
    if (!trimmed || inspecting) return;
    setInspecting(true);
    setFlowError(null);
    setFlowNotice(null);
    // Clear password input from component state right after submit.
    setConnUrlInput("");
    try {
      const res = await inspectDbConnection(trimmed);
      const schemas = res.schemas || [];
      setInspectedSchemas(schemas);
      setInspectedUrl(trimmed);
      const defaults = schemas.filter((s) => s.table_count > 0).map((s) => s.schema_name);
      setSelectedSchemas(defaults.length ? defaults : schemas.map((s) => s.schema_name));
      if (!labelInput) {
        setLabelInput("My Neon DB");
      }
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setFlowError(err instanceof ApiRequestError ? err.message : "Could not inspect database.");
    } finally {
      setInspecting(false);
    }
  }

  function toggleSchema(schemaName) {
    setSelectedSchemas((prev) =>
      prev.includes(schemaName) ? prev.filter((s) => s !== schemaName) : [...prev, schemaName],
    );
  }

  async function handleConnect(event) {
    event.preventDefault();
    if (!inspectedUrl || !selectedSchemas.length || connecting) return;
    const cleanLabel = labelInput.trim() || "My Neon DB";
    const urlToUse = inspectedUrl;
    // Clear secret from component state right after submit.
    setInspectedUrl("");
    setConnUrlInput("");
    setConnecting(true);
    setFlowError(null);
    setFlowNotice(null);
    try {
      const created = await createDbConnection(urlToUse, {
        label: cleanLabel,
        schemas: selectedSchemas,
      });
      setDbCredential(created.id, urlToUse);
      setInspectedSchemas(null);
      setSelectedSchemas([]);
      setLabelInput("");
      setFlowNotice(`Connected "${created.label}" (${created.table_count} tables ingested).`);
      await onConnectionsChanged?.();
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setFlowError(err instanceof ApiRequestError ? err.message : "Could not ingest schema.");
    } finally {
      setConnecting(false);
    }
  }

  async function handleReconnectSubmit(event, conn) {
    event.preventDefault();
    const trimmed = reconnectUrlInput.trim();
    if (!trimmed || reconnecting) return;
    // Clear password input from component state right after submit.
    setReconnectUrlInput("");
    setReconnecting(true);
    setFlowError(null);
    setFlowNotice(null);
    try {
      if (conn.is_expired || conn.ingest_status !== "ready") {
        await reingestDbConnection(conn.id, trimmed);
        setFlowNotice(`Reconnected and re-ingested "${conn.label}".`);
      } else {
        setFlowNotice(`Credentials saved for this tab session for "${conn.label}".`);
      }
      setDbCredential(conn.id, trimmed);
      setReconnectTargetId(null);
      await onConnectionsChanged?.();
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setFlowError(err instanceof ApiRequestError ? err.message : "Could not reconnect.");
    } finally {
      setReconnecting(false);
    }
  }

  async function handleReingestExisting(conn) {
    const existingCred = getDbCredential(conn.id);
    if (!existingCred) {
      setReconnectTargetId(conn.id);
      return;
    }
    setReconnecting(true);
    setFlowError(null);
    setFlowNotice(null);
    try {
      await reingestDbConnection(conn.id, existingCred);
      setFlowNotice(`Re-ingested "${conn.label}".`);
      await onConnectionsChanged?.();
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setFlowError(err instanceof ApiRequestError ? err.message : "Could not re-ingest schema.");
    } finally {
      setReconnecting(false);
    }
  }

  async function handleDisconnect(conn) {
    const confirmed = window.confirm(
      `Disconnect "${conn.label}"? This deletes stored schema metadata and clears session credentials.`,
    );
    if (!confirmed) return;
    setFlowError(null);
    setFlowNotice(null);
    try {
      await deleteDbConnection(conn.id);
      setFlowNotice(`Disconnected "${conn.label}".`);
      await onConnectionsChanged?.();
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 401) {
        onClose();
        return;
      }
      setFlowError(err instanceof ApiRequestError ? err.message : "Could not disconnect.");
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="settings-modal connections-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="connections-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="settings-modal-head">
          <div>
            <h2 id="connections-modal-title">Neon Database Connections</h2>
            <p className="settings-modal-sub">
              Connect your own Neon Postgres database with a read-only credential.
            </p>
          </div>
          <button type="button" className="modal-close-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="honest-copy-box">
          <p>
            Your connection string stays in this tab&apos;s session and is never saved on our
            servers. It passes through server memory while a query runs. We do store your
            table/column structure (DDL) for up to 24h and delete it when you disconnect.
          </p>
          <p>Queries run on your Neon database and may wake it and use your compute hours.</p>
          <p>We never log query results. Only your question, schema text and errors go to the LLM.</p>
        </div>

        <div className="setup-guide">
          <button
            type="button"
            className="guide-toggle-btn"
            onClick={() => setGuideOpen((prev) => !prev)}
          >
            {guideOpen ? "Hide read-only role setup guide ⌃" : "Read-only role setup guide (SQL) ⌄"}
          </button>
          {guideOpen && (
            <div className="guide-body">
              <p className="guide-warning">
                Create the role in the Neon SQL Editor, not the console. Do not paste an owner
                string or a Neon API key.
              </p>
              <div className="sql-snippet-block">
                <div className="sql-snippet-head">
                  <span>Create read-only role</span>
                  <button
                    type="button"
                    className="btn-secondary btn-xs"
                    onClick={() => copySnippet("create", READONLY_ROLE_SQL)}
                  >
                    {copiedSql === "create" ? "Copied" : "Copy SQL"}
                  </button>
                </div>
                <pre>{READONLY_ROLE_SQL}</pre>
              </div>
              <div className="sql-snippet-block">
                <div className="sql-snippet-head">
                  <span>Revoke &amp; drop role</span>
                  <button
                    type="button"
                    className="btn-secondary btn-xs"
                    onClick={() => copySnippet("revoke", REVOKE_ROLE_SQL)}
                  >
                    {copiedSql === "revoke" ? "Copied" : "Copy SQL"}
                  </button>
                </div>
                <pre>{REVOKE_ROLE_SQL}</pre>
              </div>
            </div>
          )}
        </div>

        {flowError && (
          <div className="modal-alert is-error" role="alert">
            {flowError}
          </div>
        )}
        {flowNotice && (
          <div className="modal-alert is-success" role="status">
            {flowNotice}
          </div>
        )}

        <form className="settings-form" onSubmit={inspectedSchemas ? handleConnect : handleInspect}>
          {!inspectedSchemas ? (
            <>
              <label htmlFor="db-conn-url-input">Neon Read-Only Connection String</label>
              <input
                id="db-conn-url-input"
                type="password"
                autoComplete="off"
                placeholder="postgresql://schemadrill_reader:...@ep-...neon.tech/neondb"
                value={connUrlInput}
                onChange={(e) => setConnUrlInput(e.target.value)}
                disabled={inspecting}
              />
              <div className="settings-actions">
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={!connUrlInput.trim() || inspecting}
                >
                  {inspecting ? "Inspecting..." : "Inspect schemas"}
                </button>
              </div>
            </>
          ) : (
            <div className="inspect-results">
              <label htmlFor="db-conn-label-input">Connection Label</label>
              <input
                id="db-conn-label-input"
                type="text"
                value={labelInput}
                onChange={(e) => setLabelInput(e.target.value)}
                placeholder="My Neon DB"
                disabled={connecting}
              />
              <span className="block-label" style={{ marginTop: 10, display: "block" }}>
                Select schemas to ingest
              </span>
              <div className="schema-checklist">
                {inspectedSchemas.map((s) => (
                  <label key={s.schema_name} className="schema-check-item">
                    <input
                      type="checkbox"
                      checked={selectedSchemas.includes(s.schema_name)}
                      onChange={() => toggleSchema(s.schema_name)}
                      disabled={connecting}
                    />
                    <span>
                      <strong>{s.schema_name}</strong> ({s.table_count} tables)
                    </span>
                  </label>
                ))}
              </div>
              <div className="settings-actions">
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={!selectedSchemas.length || connecting}
                >
                  {connecting ? "Ingesting schema..." : "Connect & Ingest"}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    setInspectedSchemas(null);
                    setInspectedUrl("");
                  }}
                  disabled={connecting}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </form>

        <div className="connections-list-section">
          <h3>Connected Databases</h3>
          {!connections.length ? (
            <p className="connections-empty">No Neon databases connected yet.</p>
          ) : (
            <ul className="connections-list">
              {connections.map((conn) => {
                const hasCreds = hasDbCredential(conn.id);
                const statusLabel = conn.is_expired ? "expired" : conn.ingest_status;
                const isReconnectingThis = reconnectTargetId === conn.id;
                return (
                  <li key={conn.id} className="connection-item">
                    <div className="connection-item-head">
                      <div>
                        <strong>{conn.label}</strong>
                        <span className={`conn-status-chip is-${statusLabel}`}>{statusLabel}</span>
                        {!hasCreds && (
                          <span className="conn-status-chip is-missing-creds">
                            needs credentials
                          </span>
                        )}
                      </div>
                      <div className="connection-item-actions">
                        {!hasCreds ? (
                          <button
                            type="button"
                            className="btn-secondary btn-xs"
                            onClick={() => {
                              setReconnectTargetId(conn.id);
                              setReconnectUrlInput("");
                            }}
                          >
                            Reconnect
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="btn-secondary btn-xs"
                            onClick={() => handleReingestExisting(conn)}
                            disabled={reconnecting}
                          >
                            {reconnecting ? "Re-ingesting..." : "Re-ingest"}
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn-danger btn-xs"
                          onClick={() => handleDisconnect(conn)}
                        >
                          Disconnect
                        </button>
                      </div>
                    </div>
                    <div className="connection-item-meta">
                      <span>
                        {conn.role_name}@{conn.host}/{conn.database}
                      </span>
                      <span>
                        Schemas: {conn.schemas.join(", ")} · {conn.table_count} tables
                      </span>
                      <span>
                        Ingested: {formatTimestamp(conn.ingested_at)} · Expires:{" "}
                        {formatTimestamp(conn.expires_at)}
                      </span>
                    </div>
                    {isReconnectingThis && (
                      <form
                        className="reconnect-form"
                        onSubmit={(e) => handleReconnectSubmit(e, conn)}
                      >
                        <input
                          type="password"
                          autoComplete="off"
                          placeholder="Paste connection string to reconnect..."
                          value={reconnectUrlInput}
                          onChange={(e) => setReconnectUrlInput(e.target.value)}
                          disabled={reconnecting}
                        />
                        <button
                          type="submit"
                          className="btn-primary btn-xs"
                          disabled={!reconnectUrlInput.trim() || reconnecting}
                        >
                          {reconnecting ? "Working..." : conn.is_expired ? "Reconnect & Re-ingest" : "Save for session"}
                        </button>
                        <button
                          type="button"
                          className="btn-secondary btn-xs"
                          onClick={() => setReconnectTargetId(null)}
                        >
                          Cancel
                        </button>
                      </form>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
