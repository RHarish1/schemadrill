import { useEffect, useMemo, useState } from "react";
import { ApiRequestError, checkHealth, listDatasets, queryDatabase } from "./services/api";
import "./styles.css";

function App() {
  const [datasets, setDatasets] = useState([]);
  const [dataset, setDataset] = useState("");
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [apiOnline, setApiOnline] = useState(null);
  const selectedDataset = datasets.find((item) => item.id === dataset);

  useEffect(() => {
    let cancelled = false;
    listDatasets()
      .then(({ datasets: availableDatasets }) => {
        if (cancelled) return;
        setDatasets(availableDatasets);
        setDataset((currentDataset) => currentDataset || availableDatasets[0]?.id || "");
      })
      .catch(() => {
        if (!cancelled) setDatasets([]);
      });
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

  async function submitQuery(event) {
    event?.preventDefault();
    const cleanQuestion = question.trim();
    if (!cleanQuestion || loading) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const payload = await queryDatabase({ db: dataset, question: cleanQuestion });
      if (payload.status === "failed") {
        setError({ message: payload.reason || "The query could not be executed.", details: payload });
      } else {
        setResult({ ...payload, question: cleanQuestion });
      }
    } catch (requestError) {
      setError({
        message: requestError instanceof ApiRequestError ? requestError.message : "Something went wrong.",
        details: requestError instanceof ApiRequestError ? requestError.technicalDetails : undefined,
      });
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(event) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submitQuery(event);
  }

  return (
    <div className="app-shell">
      <header className="nav">
        <a className="logo" href="/" aria-label="SchemaDrill home">
          <span className="logo-mark">S</span>
          <span>Schema<span className="lime">Drill</span></span>
        </a>
        <div className="nav-right">
          <span className={`status ${apiOnline === false ? "offline" : ""}`}>
            <i /> {apiOnline === null ? "Checking API" : apiOnline ? "API online" : "API offline"}
          </span>
          <a href={`${import.meta.env.VITE_API_BASE_URL || ""}/docs`} target="_blank" rel="noreferrer">API docs ↗</a>
        </div>
      </header>

      <main>
        <section className="hero">
          <div className="eyebrow"><span /> NATURAL LANGUAGE TO SQL</div>
          <h1>Ask your database<br /><span>better questions.</span></h1>
          <p>SchemaDrill turns plain English into safe, executable SQL while keeping the generated query and returned data visible.</p>
        </section>

        <section className="workspace">
          <form className="query-panel" onSubmit={submitQuery}>
            <div className="panel-heading"><span><b>01</b> Ask your data</span><em>READ ONLY</em></div>
            <label htmlFor="dataset">Dataset</label>
            <select id="dataset" value={dataset} onChange={(event) => setDataset(event.target.value)} disabled={!datasets.length}>
              {datasets.length ? datasets.map((item) => <option value={item.id} key={item.id}>{item.label} — {item.description}</option>) : <option value="">No datasets available</option>}
            </select>
            <label htmlFor="question">What do you want to know?</label>
            <textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={handleKeyDown} placeholder="Ask a question about your data..." rows="5" required />
            <div className="suggestions"><span>Try an example</span>
              {(selectedDataset?.examples || []).map((example) => <button type="button" key={example} onClick={() => setQuestion(example)}>{example}</button>)}
            </div>
            <button className="run-button" type="submit" disabled={loading || !question.trim()}>
              {loading ? <><span className="spinner" /> Running query...</> : <>Run query <b>↗</b></>}
            </button>
            <p className="shortcut">Press <kbd>Ctrl</kbd><span>↵</span> or <kbd>⌘</kbd><span>↵</span> to run</p>
          </form>
          <ResultsPanel loading={loading} result={result} error={error} />
        </section>
      </main>
      <footer><span>SchemaDrill <b>·</b> Safe by design</span><span>Queries are bounded and read-only</span></footer>
    </div>
  );
}

function ResultsPanel({ loading, result, error }) {
  if (loading) return <section className="answer-panel centered"><span className="loader" /><h2>Working on it...</h2><p>Generating and validating a read-only query.</p></section>;
  if (error) return <section className="answer-panel centered error-state"><span className="error-icon">!</span><h2>Couldn’t run that query</h2><p>{error.message}</p>{error.details && <details><summary>Technical details</summary><pre>{typeof error.details === "string" ? error.details : JSON.stringify(error.details, null, 2)}</pre></details>}</section>;
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
    <details open={detailsOpen} onToggle={(event) => setDetailsOpen(event.currentTarget.open)}><summary><span className="block-label">EXECUTION DETAILS</span><span>{detailsOpen ? "⌃" : "⌄"}</span></summary><div className="details-grid"><span>Dataset<strong>chinook</strong></span><span>Attempts<strong>{result.attempts}</strong></span><span>Status<strong>{result.status}</strong></span></div></details>
  </section>;
}

export default App;
