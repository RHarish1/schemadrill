import { useState } from "react";

function formatStageDuration(durationMs, exact = false) {
  const value = Number(durationMs);
  if (!Number.isFinite(value)) return "—";
  if (exact) return `${value.toFixed(2)} ms`;
  return `${Math.round(value)} ms`;
}

function formatTotalLatency(durationMs) {
  const value = Number(durationMs);
  if (!Number.isFinite(value)) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} sec`;
}

function stageIcon(status) {
  if (status === "completed" || status === "passed") return "✓";
  if (status === "failed") return "✗";
  return "•";
}

function getDefaultSubtitle(stage) {
  const details = stage.details || {};
  if (stage.status === "failed" && details.summary) {
    return String(details.summary);
  }
  if (stage.name === "Schema retrieval" && typeof details.tables_identified === "number") {
    const count = details.tables_identified;
    return `Identified ${count} relevant table${count === 1 ? "" : "s"}`;
  }
  if (stage.name === "SQL generation" && details.provider) {
    return String(details.provider);
  }
  return null;
}

export default function ExecutionTraceDrawer({ trace, dataset = null, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const [showDetails, setShowDetails] = useState(false);

  if (!trace) return null;

  const stages = Array.isArray(trace.stages) ? trace.stages : [];
  const retrievedTables = Array.isArray(trace.retrieved_tables) ? trace.retrieved_tables : [];
  const retryReasons = Array.isArray(trace.retry_reasons) ? trace.retry_reasons : [];
  const retryCount = typeof trace.retry_count === "number" ? trace.retry_count : 0;

  return (
    <details
      className="trace-drawer"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="trace-summary">
        <span className="trace-summary-left">
          <span className="block-label">EXECUTION TRACE</span>
          <span className="trace-latency-pill">
            {formatTotalLatency(trace.total_duration_ms)}
          </span>
        </span>
        <span className="trace-chevron" aria-hidden="true">
          {open ? "⌃" : "⌄"}
        </span>
      </summary>

      <div className="trace-body">
        <div className="trace-stage-list">
          {stages.map((stage) => {
            const details = stage.details || {};
            const defaultSubtitle = getDefaultSubtitle(stage);
            const showTableChips =
              showDetails && stage.name === "Schema retrieval" && retrievedTables.length > 0;
            const showModel =
              showDetails && stage.name === "SQL generation" && Boolean(details.model);
            const showValidationNote =
              showDetails &&
              stage.name === "SQL validation" &&
              stage.status !== "failed" &&
              Boolean(details.summary);
            const showRowCount =
              showDetails &&
              stage.name === "Query execution" &&
              typeof details.rows_returned === "number";

            return (
              <div className={`trace-stage trace-stage--${stage.status}`} key={stage.name}>
                <div className="trace-stage-row">
                  <span className="trace-stage-title">
                    <span className="trace-stage-icon" aria-hidden="true">
                      {stageIcon(stage.status)}
                    </span>
                    <span>{stage.name}</span>
                  </span>
                  <span className="trace-stage-duration">
                    {formatStageDuration(stage.duration_ms, showDetails)}
                  </span>
                </div>

                {defaultSubtitle && (
                  <p className={`trace-stage-note ${stage.status === "failed" ? "is-error" : ""}`}>
                    {defaultSubtitle}
                  </p>
                )}

                {showTableChips && (
                  <div className="trace-chips" aria-label="Retrieved tables">
                    {retrievedTables.map((table) => (
                      <span className="trace-chip" key={table}>
                        {table}
                      </span>
                    ))}
                  </div>
                )}

                {showModel && (
                  <p className="trace-stage-extra">Model: {String(details.model)}</p>
                )}

                {showValidationNote && (
                  <p className="trace-stage-extra">{String(details.summary)}</p>
                )}

                {showRowCount && (
                  <p className="trace-stage-extra">
                    {details.rows_returned} row{details.rows_returned === 1 ? "" : "s"} returned
                  </p>
                )}
              </div>
            );
          })}
        </div>

        <div className="trace-footer">
          <div className="trace-meta-row">
            <span>Total latency</span>
            <strong>{formatTotalLatency(trace.total_duration_ms)}</strong>
          </div>
          <div className="trace-meta-row">
            <span>Retry attempts</span>
            <strong>{retryCount}</strong>
          </div>
          {showDetails && retryReasons.length > 0 && (
            <div className="trace-meta-row trace-meta-row--stack">
              <span>Retry categories</span>
              <div className="trace-chips">
                {retryReasons.map((reason) => (
                  <span className="trace-chip trace-chip--warn" key={reason}>
                    {reason}
                  </span>
                ))}
              </div>
            </div>
          )}
          {showDetails && dataset && (
            <div className="trace-meta-row">
              <span>Dataset</span>
              <strong>{dataset}</strong>
            </div>
          )}
          <div className="trace-actions">
            <button
              type="button"
              className="trace-details-toggle"
              onClick={() => setShowDetails((prev) => !prev)}
            >
              {showDetails ? "Hide details" : "Show details"}
            </button>
          </div>
        </div>
      </div>
    </details>
  );
}
