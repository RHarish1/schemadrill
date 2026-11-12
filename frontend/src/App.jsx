import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiRequestError,
  checkHealth,
  getCurrentUser,
  listDatasets,
  onInvalidLlmKey,
  onRateLimitRemaining,
  queryDatabase,
} from "./services/api";
import {
  authClient,
  broadcastAuthEvent,
  consumeDraftQuery,
  fetchAuthSession,
  onCrossTabAuthEvent,
  onSessionExpired,
  saveDraftQuery,
  signOutUser,
} from "./auth";
import { clearLlmKey, getKeyLast4, getSelectedLlmModel, onLlmKeyChange } from "./llmKey";
import ExecutionTraceDrawer from "./components/ExecutionTraceDrawer";
import SettingsModal from "./components/SettingsModal";
import "./styles.css";

const MAX_TIMEOUT_MS = 2_147_483_647;
const TOP_K_OPTIONS = [1, 2, 3, 4, 5];
const MAX_RETRIES_OPTIONS = [0, 1, 2, 3];
const DEFAULT_TOP_K = 3;
const DEFAULT_FK_EXPANSION = false;
const DEFAULT_MAX_RETRIES = 1;

function clampInt(value, min, max, fallback) {
  return Number.isInteger(value) && value >= min && value <= max ? value : fallback;
}

function getUserInitial(user) {
  const source = (user?.name || user?.email || "U").trim();
  return (source[0] || "U").toUpperCase();
}

function InfoTooltip({ id, label, text, align = "center" }) {
  return (
    <span className={`info-tip info-tip--${align}`}>
      <button
        type="button"
        className="info-tip-btn"
        aria-label={label}
        aria-describedby={id}
      >
        i
      </button>
      <span role="tooltip" id={id} className="info-tip-popover">
        {text}
      </span>
    </span>
  );
}

function App() {
  const initialDraft = useMemo(() => consumeDraftQuery(), []);
  const [datasets, setDatasets] = useState([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [dataset, setDataset] = useState(initialDraft?.dataset || "");
  const [question, setQuestion] = useState(initialDraft?.question || "");
  const [topK, setTopK] = useState(() => clampInt(initialDraft?.topK, 1, 5, DEFAULT_TOP_K));
  const [fkExpansion, setFkExpansion] = useState(() => Boolean(initialDraft?.fkExpansion));
  const [maxRetries, setMaxRetries] = useState(() =>
    clampInt(initialDraft?.maxRetries, 0, 3, DEFAULT_MAX_RETRIES),
  );
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [apiOnline, setApiOnline] = useState(null);
  const [session, setSession] = useState(null);
  const [user, setUser] = useState(null);
  const [authLoading, setAuthLoading] = useState(Boolean(authClient));
  const [signingIn, setSigningIn] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [authError, setAuthError] = useState(null);
  const [avatarBroken, setAvatarBroken] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialError, setSettingsInitialError] = useState(null);
  const [rateLimitRemaining, setRateLimitRemaining] = useState(null);
  const [, setLlmKeyVersion] = useState(0);
  const hadActiveSessionRef = useRef(false);

  const keyLast4 = getKeyLast4();
  const activeByokModel = getSelectedLlmModel();
  const usingByok = Boolean(keyLast4);

  const selectedDataset = datasets.find((item) => item.id === dataset);
  const signedInUserId = user?.id || (session ? "authenticated" : "");

  const markSessionExpired = useCallback(() => {
    hadActiveSessionRef.current = false;
    setSession(null);
    setUser(null);
    setDatasets([]);
    setDataset("");
    setSessionExpired(true);
    setAuthError(null);
  }, []);

  const applyAuthenticatedSession = useCallback((sessionData) => {
    hadActiveSessionRef.current = true;
    setSession(sessionData.session);
    setUser(sessionData.user);
    setAvatarBroken(false);
    setSessionExpired(false);
    setAuthError(null);
  }, []);

  const revalidateSession = useCallback(
    async (forceRefresh = true) => {
      if (!authClient) return;
      const nextSession = await fetchAuthSession({ forceRefresh });
      if (nextSession) {
        applyAuthenticatedSession(nextSession);
      } else if (hadActiveSessionRef.current) {
        markSessionExpired();
      }
    },
    [applyAuthenticatedSession, markSessionExpired],
  );

  useEffect(() => {
    let cancelled = false;
    const checkApiHealth = () => {
      checkHealth()
        .then(() => {
          if (!cancelled) setApiOnline(true);
        })
        .catch(() => {
          if (!cancelled) setApiOnline(false);
        });
    };
    checkApiHealth();
    const healthCheckInterval = window.setInterval(checkApiHealth, 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(healthCheckInterval);
    };
  }, []);

  useEffect(() => {
    if (!authClient) {
      setAuthLoading(false);
      return;
    }
    let cancelled = false;
    fetchAuthSession()
      .then((sessionData) => {
        if (cancelled) return;
        if (sessionData) {
          applyAuthenticatedSession(sessionData);
          broadcastAuthEvent("SIGNED_IN");
        }
      })
      .finally(() => {
        if (!cancelled) setAuthLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyAuthenticatedSession]);

  useEffect(() => {
    const unsubscribeExpired = onSessionExpired(() => {
      markSessionExpired();
    });
    const unsubscribeCrossTab = onCrossTabAuthEvent((eventType) => {
      if (eventType === "SIGNED_OUT") {
        clearLlmKey();
        hadActiveSessionRef.current = false;
        setSession(null);
        setUser(null);
        setDatasets([]);
        setDataset("");
        setTopK(DEFAULT_TOP_K);
        setFkExpansion(DEFAULT_FK_EXPANSION);
        setMaxRetries(DEFAULT_MAX_RETRIES);
        setResult(null);
        setError(null);
        setSessionExpired(false);
        setAuthError(null);
        setRateLimitRemaining(null);
      } else if (eventType === "SIGNED_IN" && !hadActiveSessionRef.current) {
        fetchAuthSession({ forceRefresh: true }).then((sessionData) => {
          if (sessionData) applyAuthenticatedSession(sessionData);
        });
      }
    });
    const unsubscribeLlmKey = onLlmKeyChange(() => {
      setLlmKeyVersion((v) => v + 1);
    });
    const unsubscribeInvalidKey = onInvalidLlmKey((detail) => {
      setSettingsInitialError(detail);
      setSettingsOpen(true);
    });
    const unsubscribeRateLimit = onRateLimitRemaining((remaining) => {
      setRateLimitRemaining(remaining);
    });
    return () => {
      unsubscribeExpired();
      unsubscribeCrossTab();
      unsubscribeLlmKey();
      unsubscribeInvalidKey();
      unsubscribeRateLimit();
    };
  }, [applyAuthenticatedSession, markSessionExpired]);

  useEffect(() => {
    if (!session || !authClient) return;
    const handleVisibilityOrFocus = () => {
      if (document.visibilityState === "hidden") return;
      revalidateSession(true);
    };
    window.addEventListener("focus", handleVisibilityOrFocus);
    document.addEventListener("visibilitychange", handleVisibilityOrFocus);

    let expiryTimer = null;
    if (session.expiresAt) {
      const expiresMs = new Date(session.expiresAt).getTime();
      if (!Number.isNaN(expiresMs)) {
        const delayMs = Math.min(Math.max(expiresMs - Date.now() + 250, 0), MAX_TIMEOUT_MS);
        expiryTimer = window.setTimeout(() => {
          revalidateSession(true);
        }, delayMs);
      }
    }

    return () => {
      window.removeEventListener("focus", handleVisibilityOrFocus);
      document.removeEventListener("visibilitychange", handleVisibilityOrFocus);
      if (expiryTimer !== null) window.clearTimeout(expiryTimer);
    };
  }, [revalidateSession, session]);

  useEffect(() => {
    if (!signedInUserId) {
      setDatasets([]);
      setDatasetsLoading(false);
      return;
    }
    let cancelled = false;
    setDatasetsLoading(true);
    Promise.allSettled([listDatasets(), getCurrentUser()])
      .then(([datasetsResult, profileResult]) => {
        if (cancelled) return;
        if (datasetsResult.status === "fulfilled") {
          const availableDatasets = datasetsResult.value.datasets;
          setDatasets(availableDatasets);
          setDataset((currentDataset) =>
            availableDatasets.some((item) => item.id === currentDataset)
              ? currentDataset
              : availableDatasets[0]?.id || "",
          );
        } else {
          setDatasets([]);
        }
        if (profileResult.status === "fulfilled" && profileResult.value) {
          const profile = profileResult.value;
          setUser((currentUser) => ({
            id: currentUser?.id || profile.user_id,
            name: currentUser?.name || profile.display_name || null,
            email: currentUser?.email || profile.email || null,
            image: currentUser?.image || null,
          }));
        }
      })
      .finally(() => {
        if (!cancelled) setDatasetsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [signedInUserId]);

  async function signIn() {
    if (!authClient) {
      setAuthError("Google sign-in is not configured for this environment.");
      return;
    }
    if (signingIn) return;
    saveDraftQuery({ question, dataset, topK, fkExpansion, maxRetries });
    setSigningIn(true);
    setAuthError(null);
    try {
      const response = await authClient.signIn.social({
        provider: "google",
        callbackURL: window.location.origin,
      });
      if (response?.error) {
        setAuthError(response.error.message || "Could not start Google sign-in. Please try again.");
        setSigningIn(false);
      }
    } catch (signInError) {
      setAuthError(
        signInError instanceof Error && signInError.message
          ? signInError.message
          : "Could not start Google sign-in. Please try again.",
      );
      setSigningIn(false);
    }
  }

  async function handleSignOut() {
    if (signingOut) return;
    setSigningOut(true);
    setAuthError(null);
    hadActiveSessionRef.current = false;
    try {
      await signOutUser();
    } finally {
      setSession(null);
      setUser(null);
      setDatasets([]);
      setDataset("");
      setTopK(DEFAULT_TOP_K);
      setFkExpansion(DEFAULT_FK_EXPANSION);
      setMaxRetries(DEFAULT_MAX_RETRIES);
      setResult(null);
      setError(null);
      setSessionExpired(false);
      setSigningOut(false);
    }
  }

  async function submitQuery(event) {
    event?.preventDefault();
    const cleanQuestion = question.trim();
    if (!cleanQuestion || loading) return;
    if (!session) {
      setAuthError("Sign in with Google to run queries.");
      return;
    }
    if (!dataset) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const payload = await queryDatabase({
        db: dataset,
        question: cleanQuestion,
        top_k: topK,
        fk_expansion: fkExpansion,
        max_retries: maxRetries,
      });
      if (payload.status === "failed") {
        setError({
          message: payload.reason || "The query could not be executed.",
          details: payload,
          executionTrace: payload.execution_trace || null,
        });
      } else {
        setApiOnline(true);
        setResult({
          ...payload,
          question: cleanQuestion,
          dataset: selectedDataset?.label || dataset,
        });
      }
    } catch (requestError) {
      if (requestError instanceof ApiRequestError && requestError.status === 401) {
        return;
      }
      setError({
        message:
          requestError instanceof ApiRequestError ? requestError.message : "Something went wrong.",
        code: requestError instanceof ApiRequestError ? requestError.code : undefined,
        details:
          requestError instanceof ApiRequestError ? requestError.technicalDetails : undefined,
        executionTrace:
          requestError instanceof ApiRequestError ? requestError.executionTrace || null : null,
      });
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(event) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submitQuery(event);
  }

  const displayName = user?.name || user?.email || "Signed in";
  const secondaryEmail = user?.name && user?.email && user.name !== user.email ? user.email : null;

  return (
    <div className="app-shell">
      <header className="nav">
        <a className="logo" href="/" aria-label="SchemaDrill home">
          <span className="logo-mark">S</span>
          <span>Schema<span className="lime">Drill</span></span>
        </a>
        <div className="nav-right">
          {authLoading ? (
            <span className="auth-pending">Checking session...</span>
          ) : session ? (
            <div className="auth-group">
              <div
                className="user-chip"
                title={secondaryEmail ? `${displayName} (${secondaryEmail})` : displayName}
              >
                {user?.image && !avatarBroken ? (
                  <img
                    className="user-avatar-img"
                    src={user.image}
                    alt=""
                    referrerPolicy="no-referrer"
                    onError={() => setAvatarBroken(true)}
                  />
                ) : (
                  <span className="user-avatar" aria-hidden="true">{getUserInitial(user)}</span>
                )}
                <span className="user-meta">
                  <strong className="user-name">{displayName}</strong>
                  {secondaryEmail && <small className="user-email">{secondaryEmail}</small>}
                </span>
              </div>
              <button
                className="nav-signout-btn"
                type="button"
                onClick={handleSignOut}
                disabled={signingOut}
              >
                {signingOut ? "Signing out..." : "Sign out"}
              </button>
            </div>
          ) : (
            <button
              className="nav-auth-btn"
              type="button"
              onClick={signIn}
              disabled={signingIn}
            >
              {signingIn ? "Redirecting..." : "Sign in with Google"}
            </button>
          )}
          {session && (
            <button
              type="button"
              className={`tier-badge-btn ${usingByok ? "is-byok" : "is-free"}`}
              onClick={() => {
                setSettingsInitialError(null);
                setSettingsOpen(true);
              }}
              title={
                usingByok
                  ? `Using your Gemini key ending •••• ${keyLast4}`
                  : "Add your key to use your own quota"
              }
            >
              <i />
              {usingByok
                ? `Using your key${activeByokModel ? ` · ${activeByokModel}` : ""}`
                : rateLimitRemaining !== null
                  ? `Free tier: ${rateLimitRemaining} left`
                  : "Free tier"}
            </button>
          )}
          <span className={`status ${apiOnline === false ? "offline" : ""}`}>
            <i /> {apiOnline === null ? "Checking API" : apiOnline ? "API online" : "API offline"}
          </span>
          <a href={`${import.meta.env.VITE_API_BASE_URL || ""}/docs`} target="_blank" rel="noreferrer">API docs ↗</a>
        </div>
      </header>

      <main>
        {(sessionExpired || authError) && (
          <div className="auth-banner" role="alert">
            <div className="auth-banner-copy">
              <span className="auth-banner-label">
                <i /> {sessionExpired ? "SESSION EXPIRED" : "AUTHENTICATION"}
              </span>
              <p>
                {sessionExpired
                  ? "Your session ended. Sign in again with Google to continue — your draft question will be kept."
                  : authError}
              </p>
            </div>
            <div className="auth-banner-actions">
              {!session && authClient && (
                <button
                  className="banner-signin-btn"
                  type="button"
                  onClick={signIn}
                  disabled={signingIn}
                >
                  {signingIn ? "Redirecting..." : "Sign in with Google"}
                </button>
              )}
              <button
                className="banner-dismiss-btn"
                type="button"
                onClick={() => {
                  setSessionExpired(false);
                  setAuthError(null);
                }}
                aria-label="Dismiss authentication notice"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        <section className="hero">
          <div className="eyebrow"><span /> NATURAL LANGUAGE TO SQL</div>
          <h1>Ask your database<br /><span>better questions.</span></h1>
          <p>SchemaDrill turns plain English into safe, executable SQL while keeping the generated query and returned data visible.</p>
        </section>

        <section className="workspace">
          <form className="query-panel" onSubmit={submitQuery}>
            <div className="panel-heading"><span><b>01</b> Ask your data</span><em>READ ONLY</em></div>
            <label htmlFor="dataset">Dataset</label>
            <select
              id="dataset"
              value={dataset}
              onChange={(event) => setDataset(event.target.value)}
              disabled={!datasets.length}
            >
              {datasets.length ? (
                datasets.map((item) => (
                  <option value={item.id} key={item.id}>{item.label} — {item.description}</option>
                ))
              ) : (
                <option value="">
                  {!session
                    ? "Sign in with Google to load datasets"
                    : datasetsLoading
                      ? "Loading datasets..."
                      : "No datasets available"}
                </option>
              )}
            </select>
            <label htmlFor="question">What do you want to know?</label>
            <textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={handleKeyDown} placeholder="Ask a question about your data..." rows="5" required />
            <div className="suggestions"><span>Try an example</span>
              {(selectedDataset?.examples || []).map((example) => <button type="button" key={example} onClick={() => setQuestion(example)}>{example}</button>)}
            </div>
            <div className="query-settings" aria-label="Query tuning options">
              <div className="query-settings-head">
                <span className="block-label">Query options</span>
                {!session && <span className="query-settings-lock">Sign in to customize</span>}
              </div>
              <div className="query-settings-grid">
                <div className="setting-field">
                  <div className="setting-label-row">
                    <label htmlFor="top-k">Top-k</label>
                    <InfoTooltip
                      id="top-k-tooltip"
                      label="Explain top-k tradeoffs"
                      align="left"
                      text="Tables retrieved for SQL context. Low k (1–2) is faster with fewer tokens but may miss tables; high k (4–5) improves multi-table recall at higher latency and token cost."
                    />
                  </div>
                  <select
                    id="top-k"
                    value={topK}
                    onChange={(event) => setTopK(Number(event.target.value))}
                    disabled={!session || loading}
                  >
                    {TOP_K_OPTIONS.map((value) => (
                      <option key={value} value={value}>
                        {value === DEFAULT_TOP_K ? `${value} (default)` : value}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="setting-field">
                  <div className="setting-label-row">
                    <label htmlFor="max-retries">Max retries</label>
                    <InfoTooltip
                      id="max-retries-tooltip"
                      label="Explain max retries tradeoffs"
                      align="center"
                      text="LLM self-correction retries on SQL validation failure. 0 is fastest; higher values (1–3) improve recovery at added latency."
                    />
                  </div>
                  <select
                    id="max-retries"
                    value={maxRetries}
                    onChange={(event) => setMaxRetries(Number(event.target.value))}
                    disabled={!session || loading}
                  >
                    {MAX_RETRIES_OPTIONS.map((value) => (
                      <option key={value} value={value}>
                        {value === DEFAULT_MAX_RETRIES
                          ? `${value} (default)`
                          : value === 0
                            ? "0 (none)"
                            : value}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="setting-field">
                  <div className="setting-label-row">
                    <span className="setting-label-text" id="fk-expansion-label">FK expansion</span>
                    <InfoTooltip
                      id="fk-expansion-tooltip"
                      label="Explain foreign key expansion"
                      align="right"
                      text="Include one-hop foreign-key neighbor tables automatically. Helps multi-table joins at slightly higher retrieval latency."
                    />
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={fkExpansion}
                    aria-labelledby="fk-expansion-label"
                    className={`toggle-switch ${fkExpansion ? "is-enabled" : ""}`}
                    onClick={() => setFkExpansion((prev) => !prev)}
                    disabled={!session || loading}
                  >
                    <span className="toggle-track" aria-hidden="true">
                      <span className="toggle-thumb" />
                    </span>
                    <span className="toggle-text">{fkExpansion ? "Enabled" : "Disabled"}</span>
                  </button>
                </div>
              </div>
            </div>
            <button className="run-button" type="submit" disabled={loading || !question.trim() || !session || !dataset}>
              {loading ? <><span className="spinner" /> Running query...</> : <>Run query <b>↗</b></>}
            </button>
            <p className="shortcut">Press <kbd>Ctrl</kbd><span>↵</span> or <kbd>⌘</kbd><span>↵</span> to run</p>
            {session && !usingByok && (
              <p className="byok-hint">
                <button
                  type="button"
                  className="byok-hint-btn"
                  onClick={() => {
                    setSettingsInitialError(null);
                    setSettingsOpen(true);
                  }}
                >
                  Add your key to use your own quota
                </button>
              </p>
            )}
          </form>
          <ResultsPanel
            loading={loading}
            result={result}
            error={error}
            sessionExpired={sessionExpired}
            signingIn={signingIn}
            onSignIn={signIn}
            onOpenSettings={() => {
              setSettingsInitialError(null);
              setSettingsOpen(true);
            }}
          />
        </section>
      </main>
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        initialError={settingsInitialError}
      />
      <footer><span>SchemaDrill <b>·</b> Safe by design</span><span>Queries are bounded and read-only</span></footer>
    </div>
  );
}

function ResultsPanel({ loading, result, error, sessionExpired, signingIn, onSignIn, onOpenSettings }) {
  if (loading) return <section className="answer-panel centered"><span className="loader" /><h2>Working on it...</h2><p>Generating and validating a read-only query.</p></section>;
  if (sessionExpired && !result) {
    return (
      <section className="answer-panel centered error-state">
        <span className="error-icon">!</span>
        <h2>Session expired</h2>
        <p>Sign in again with Google to load your datasets and run this query. Your draft question is saved.</p>
        {authClient && (
          <button
            className="panel-action-btn"
            type="button"
            onClick={onSignIn}
            disabled={signingIn}
          >
            {signingIn ? "Redirecting..." : "Sign in with Google"}
          </button>
        )}
      </section>
    );
  }
  if (error) {
    return (
      <section className={`answer-panel centered error-state ${error.executionTrace ? "has-trace" : ""}`}>
        <span className="error-icon">!</span>
        <h2>Couldn’t run that query</h2>
        <p>{error.message}</p>
        {error.code === "free_tier_exhausted" && (
          <button
            className="panel-action-btn"
            type="button"
            onClick={onOpenSettings}
          >
            Add Gemini API key
          </button>
        )}
        {error.executionTrace ? (
          <ExecutionTraceDrawer trace={error.executionTrace} defaultOpen={true} />
        ) : (
          error.details && (
            <details>
              <summary>Technical details</summary>
              <pre>{typeof error.details === "string" ? error.details : JSON.stringify(error.details, null, 2)}</pre>
            </details>
          )
        )}
      </section>
    );
  }
  if (!result) return <section className="answer-panel centered"><div className="empty-icon">⌁</div><h2>Your answer will appear here</h2><p>Ask a question to generate a query and explore your data.</p><div className="empty-line" /></section>;
  return <Result result={result} />;
}

function Result({ result }) {
  const columns = result.columns?.length ? result.columns : Object.keys(result.rows?.[0] || {});
  const rows = result.rows || [];
  const [copied, setCopied] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const sql = result.sql || "";
  const csv = useMemo(() => [columns, ...rows.map((row) => columns.map((column) => row[column]))]
    .map((line) => line.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")).join("\n"), [columns, rows]);

  async function copySql() {
    if (!sql) return;
    await navigator.clipboard.writeText(sql);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  function downloadCsv() {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    link.download = "schemadrill-results.csv";
    link.click();
    URL.revokeObjectURL(link.href);
  }

  return <section className="answer-panel result-panel">
    <div className="answer-heading"><div><span className="success-label"><i /> QUERY COMPLETE</span><h2>Here’s what we found</h2></div><span className="attempts">{result.attempts} attempt{result.attempts === 1 ? "" : "s"}</span></div>
    <div className="result-toolbar"><span>{rows.length} row{rows.length === 1 ? "" : "s"} · {columns.length} column{columns.length === 1 ? "" : "s"}</span>{rows.length > 0 && <button onClick={downloadCsv}>↓ Export CSV</button>}</div>
    {rows.length ? <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column} className={row[column] === null ? "null" : ""}>{row[column] === null ? "NULL" : String(row[column])}</td>)}</tr>)}</tbody></table></div> : <div className="no-rows">The query completed successfully but returned no rows.</div>}
    <div className="sql-heading"><span className="block-label">GENERATED SQL</span><button onClick={copySql} disabled={!sql}>{copied ? "Copied" : "Copy SQL"}</button></div>
    <pre className="sql">{sql || "SQL was not returned by the API."}</pre>
    {result.execution_trace ? (
      <ExecutionTraceDrawer
        trace={result.execution_trace}
        dataset={result.dataset || "chinook"}
        defaultOpen={false}
      />
    ) : (
      <details open={detailsOpen} onToggle={(event) => setDetailsOpen(event.currentTarget.open)}><summary><span className="block-label">EXECUTION DETAILS</span><span>{detailsOpen ? "⌃" : "⌄"}</span></summary><div className="details-grid"><span>Dataset<strong>{result.dataset || "chinook"}</strong></span><span>Attempts<strong>{result.attempts}</strong></span><span>Status<strong>{result.status}</strong></span></div></details>
    )}
  </section>;
}

export default App;
