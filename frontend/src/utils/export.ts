import type { ExecutionTrace, PublicStageTrace } from "../types/api";

export interface PdfQueryOptions {
  topK?: number;
  fkExpansion?: boolean;
  maxRetries?: number;
  tier?: string;
  requestedModel?: string | null;
}

export interface PdfReportInput {
  question?: string;
  dataset?: string;
  sql?: string;
  columns: string[];
  rows: Record<string, unknown>[];
  attempts?: number;
  status?: string;
  generatedAt?: Date;
  queryOptions?: PdfQueryOptions | null;
  executionTrace?: ExecutionTrace | null;
}

export const PDF_MAX_ROWS = 500;

const FORMULA_TRIGGER_PREFIX = /^[=+\-@\t\r]/;

export function sanitizeCsvCell(value: unknown): string {
  if (value === null || value === undefined) {
    return '""';
  }
  const raw = String(value);
  const safe = FORMULA_TRIGGER_PREFIX.test(raw) ? `'${raw}` : raw;
  return `"${safe.replaceAll('"', '""')}"`;
}

export function buildCsvContent(
  columns: string[],
  rows: Record<string, unknown>[],
): string {
  const headerLine = columns.map((col) => sanitizeCsvCell(col)).join(",");
  const dataLines = rows.map((row) =>
    columns.map((col) => sanitizeCsvCell(row[col])).join(","),
  );
  return [headerLine, ...dataLines].join("\n");
}

export function downloadCsvFile(
  columns: string[],
  rows: Record<string, unknown>[],
  filename = "schemadrill-results.csv",
): void {
  const csv = buildCsvContent(columns, rows);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  triggerBrowserDownload(blob, filename);
}

function triggerBrowserDownload(blob: Blob, filename: string): void {
  const link = document.createElement("a");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function escapePdfText(input: string): string {
  let out = "";
  for (let i = 0; i < input.length; i += 1) {
    const code = input.charCodeAt(i);
    const ch = input[i];
    if (ch === "\\" || ch === "(" || ch === ")") {
      out += `\\${ch}`;
    } else if (code >= 32 && code <= 126) {
      out += ch;
    } else if (code === 9) {
      out += "  ";
    } else if (code >= 160 && code <= 255) {
      out += ch;
    } else {
      out += "?";
    }
  }
  return out;
}

function wrapTextByChars(text: string, maxChars: number): string[] {
  const limit = Math.max(8, Math.floor(maxChars));
  const rawLines = (text || "").replace(/\r\n/g, "\n").split("\n");
  const wrapped: string[] = [];

  for (const rawLine of rawLines) {
    const trimmedRight = rawLine.replace(/\s+$/, "");
    if (!trimmedRight) {
      wrapped.push("");
      continue;
    }
    let remaining = trimmedRight;
    while (remaining.length > limit) {
      let splitAt = remaining.lastIndexOf(" ", limit);
      if (splitAt <= Math.floor(limit * 0.4)) {
        splitAt = limit;
      }
      wrapped.push(remaining.slice(0, splitAt).trimEnd());
      remaining = remaining.slice(splitAt).trimStart();
    }
    if (remaining.length > 0) {
      wrapped.push(remaining);
    }
  }
  return wrapped.length ? wrapped : [""];
}

function truncateCellText(text: string, maxChars: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  const limit = Math.max(4, Math.floor(maxChars));
  if (clean.length <= limit) {
    return clean;
  }
  if (limit <= 4) {
    return clean.slice(0, limit);
  }
  return `${clean.slice(0, limit - 3)}...`;
}

function formatExactDuration(durationMs: unknown): string {
  const value = Number(durationMs);
  if (!Number.isFinite(value)) return "-";
  return `${value.toFixed(2)} ms`;
}

function formatTotalLatency(durationMs: unknown): string {
  const value = Number(durationMs);
  if (!Number.isFinite(value)) return "-";
  if (value < 1000) return `${value.toFixed(2)} ms`;
  return `${(value / 1000).toFixed(2)} sec (${value.toFixed(1)} ms)`;
}

function formatExecutionMode(mode?: string | null): string {
  if (mode === "read_only_role_and_transaction") {
    return "Read-only (PostgreSQL role + transaction)";
  }
  if (mode === "read_only_transaction") {
    return "Read-only (transaction)";
  }
  if (mode === "read_only") {
    return "Read-only";
  }
  return mode || "Read-only";
}

function formatCheckStatus(status: unknown): string | null {
  if (status === "passed") return "Passed";
  if (status === "failed") return "Failed";
  if (status === "skipped") return "Skipped";
  return null;
}

function formatStageStatusBadge(status: string): string {
  if (status === "completed" || status === "passed") return "[PASS]";
  if (status === "failed") return "[FAIL]";
  return "[INFO]";
}

function describeStageDetails(
  stage: PublicStageTrace,
  trace: ExecutionTrace,
): string[] {
  const details = (stage.details || {}) as Record<string, unknown>;
  const lines: string[] = [];

  if (stage.status === "failed" && details.summary) {
    lines.push(`Failure reason: ${String(details.summary)}`);
  }

  if (stage.name === "Schema retrieval") {
    if (typeof details.tables_identified === "number") {
      const count = details.tables_identified;
      lines.push(`Identified ${count} relevant table${count === 1 ? "" : "s"}`);
    }
    if (Array.isArray(trace.retrieved_tables) && trace.retrieved_tables.length > 0) {
      lines.push(`Retrieved tables: ${trace.retrieved_tables.join(", ")}`);
    }
    const topKText =
      typeof trace.top_k === "number" ? `Top-k: ${trace.top_k}` : "Top-k: Full schema";
    const fkText =
      typeof trace.fk_expansion === "boolean"
        ? `FK expansion: ${trace.fk_expansion ? "Enabled" : "Disabled"}`
        : "";
    lines.push(fkText ? `${topKText}  |  ${fkText}` : topKText);
  } else if (stage.name === "SQL generation") {
    const provider = details.provider ? String(details.provider) : "Google Gemini";
    const model = details.model || trace.model;
    lines.push(model ? `Provider: ${provider}  |  Model: ${String(model)}` : `Provider: ${provider}`);
  } else if (stage.name === "SQL validation") {
    if (stage.status !== "failed" && details.summary) {
      lines.push(String(details.summary));
    }
    const checks = [
      { label: "Read-only AST policy", value: formatCheckStatus(details.read_only_policy) },
      { label: "SQL syntax", value: formatCheckStatus(details.sql_syntax) },
      { label: "EXPLAIN dry-run", value: formatCheckStatus(details.dry_run) },
    ].filter((c) => c.value !== null);
    if (checks.length > 0) {
      lines.push(checks.map((c) => `${c.label}: ${c.value}`).join("  |  "));
    }
  } else if (stage.name === "Query execution") {
    if (typeof details.rows_returned === "number") {
      const r = details.rows_returned;
      lines.push(`Returned ${r} row${r === 1 ? "" : "s"}`);
    }
  }

  return lines;
}

function computeColumnWidths(
  columns: string[],
  rows: Record<string, unknown>[],
  totalWidth: number,
): number[] {
  if (!columns.length) return [];
  const sampleRows = rows.slice(0, 40);
  const weights = columns.map((col) => {
    let maxLen = Math.max(col.length, 6);
    for (const row of sampleRows) {
      const val = row[col] === null || row[col] === undefined ? "NULL" : String(row[col]);
      maxLen = Math.max(maxLen, Math.min(val.length, 36));
    }
    return Math.min(Math.max(maxLen, 6), 36);
  });
  const totalWeight = weights.reduce((acc, w) => acc + w, 0) || 1;
  return weights.map((w) => (w / totalWeight) * totalWidth);
}

export function buildPdfBytes(input: PdfReportInput): Uint8Array {
  const columns = input.columns || [];
  const allRows = input.rows || [];
  const cappedRows = allRows.slice(0, PDF_MAX_ROWS);
  const isTruncated = allRows.length > PDF_MAX_ROWS;
  const isLandscape = columns.length > 5;

  const pageWidth = isLandscape ? 841.89 : 595.28;
  const pageHeight = isLandscape ? 595.28 : 841.89;
  const marginX = 36;
  const marginTop = 38;
  const marginBottom = 42;
  const contentWidth = pageWidth - marginX * 2;

  const pages: string[][] = [];
  let currentCommands: string[] = [];
  let cursorY = pageHeight - marginTop;

  const startNewPage = () => {
    if (currentCommands.length > 0) {
      pages.push(currentCommands);
    }
    currentCommands = [];
    cursorY = pageHeight - marginTop;
  };

  const ensureSpace = (neededHeight: number, onNewPage?: () => void) => {
    if (cursorY - neededHeight < marginBottom) {
      startNewPage();
      onNewPage?.();
    }
  };

  const drawRect = (
    x: number,
    yTop: number,
    width: number,
    height: number,
    fillRgb: [number, number, number],
    strokeRgb?: [number, number, number],
  ) => {
    const yBottom = yTop - height;
    currentCommands.push("q");
    currentCommands.push(
      `${fillRgb[0].toFixed(3)} ${fillRgb[1].toFixed(3)} ${fillRgb[2].toFixed(3)} rg`,
    );
    if (strokeRgb) {
      currentCommands.push(
        `${strokeRgb[0].toFixed(3)} ${strokeRgb[1].toFixed(3)} ${strokeRgb[2].toFixed(3)} RG`,
      );
      currentCommands.push("0.6 w");
      currentCommands.push(
        `${x.toFixed(2)} ${yBottom.toFixed(2)} ${width.toFixed(2)} ${height.toFixed(2)} B`,
      );
    } else {
      currentCommands.push(
        `${x.toFixed(2)} ${yBottom.toFixed(2)} ${width.toFixed(2)} ${height.toFixed(2)} re f`,
      );
    }
    currentCommands.push("Q");
  };

  const drawLine = (
    x1: number,
    y1: number,
    x2: number,
    y2: number,
    strokeRgb: [number, number, number],
    lineWidth = 0.5,
  ) => {
    currentCommands.push("q");
    currentCommands.push(
      `${strokeRgb[0].toFixed(3)} ${strokeRgb[1].toFixed(3)} ${strokeRgb[2].toFixed(3)} RG`,
    );
    currentCommands.push(`${lineWidth.toFixed(2)} w`);
    currentCommands.push(
      `${x1.toFixed(2)} ${y1.toFixed(2)} m ${x2.toFixed(2)} ${y2.toFixed(2)} l S`,
    );
    currentCommands.push("Q");
  };

  const drawText = (
    text: string,
    x: number,
    yBaseline: number,
    font: "F1" | "F2" | "F3",
    size: number,
    rgb: [number, number, number] = [0.08, 0.11, 0.14],
  ) => {
    currentCommands.push("BT");
    currentCommands.push(`/${font} ${size.toFixed(1)} Tf`);
    currentCommands.push(`${rgb[0].toFixed(3)} ${rgb[1].toFixed(3)} ${rgb[2].toFixed(3)} rg`);
    currentCommands.push(`${x.toFixed(2)} ${yBaseline.toFixed(2)} Td`);
    currentCommands.push(`(${escapePdfText(text)}) Tj`);
    currentCommands.push("ET");
  };

  // 1. Report Banner
  const bannerHeight = 48;
  drawRect(marginX, cursorY, contentWidth, bannerHeight, [0.071, 0.102, 0.122]);
  drawText("SchemaDrill Query Report", marginX + 14, cursorY - 21, "F2", 15, [0.847, 0.953, 0.42]);

  const timestamp =
    (input.generatedAt || new Date()).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  const datasetLabel = input.dataset || "default";
  const attemptsLabel =
    typeof input.attempts === "number"
      ? `${input.attempts} attempt${input.attempts === 1 ? "" : "s"}`
      : "1 attempt";
  const metaLine = `Dataset: ${datasetLabel}   |   Rows: ${allRows.length}   |   Columns: ${columns.length}   |   ${attemptsLabel}   |   Generated: ${timestamp}`;
  drawText(
    truncateCellText(metaLine, Math.floor(contentWidth / 4.6)),
    marginX + 14,
    cursorY - 37,
    "F1",
    8.5,
    [0.78, 0.83, 0.85],
  );
  cursorY -= bannerHeight + 16;

  // 2. Question Section
  const charsPerLineBody = Math.floor((contentWidth - 20) / 5.2);
  if (input.question) {
    drawText("NATURAL LANGUAGE QUESTION", marginX, cursorY - 9, "F2", 8.5, [0.28, 0.35, 0.38]);
    cursorY -= 14;
    const questionLines = wrapTextByChars(input.question, charsPerLineBody);
    const questionBoxHeight = questionLines.length * 13 + 12;
    ensureSpace(questionBoxHeight);
    drawRect(
      marginX,
      cursorY,
      contentWidth,
      questionBoxHeight,
      [0.965, 0.976, 0.98],
      [0.83, 0.87, 0.89],
    );
    questionLines.forEach((line, idx) => {
      drawText(line, marginX + 10, cursorY - 14 - idx * 13, "F1", 9.5, [0.1, 0.14, 0.16]);
    });
    cursorY -= questionBoxHeight + 14;
  }

  // 3. Query Options & Execution Trace ("Show details" full report)
  const trace = input.executionTrace || null;
  const opts = input.queryOptions || null;
  const effectiveTopK =
    typeof trace?.top_k === "number"
      ? trace.top_k
      : typeof opts?.topK === "number"
        ? opts.topK
        : 3;
  const effectiveFk =
    typeof trace?.fk_expansion === "boolean"
      ? trace.fk_expansion
      : typeof opts?.fkExpansion === "boolean"
        ? opts.fkExpansion
        : false;
  const effectiveMaxRetries = typeof opts?.maxRetries === "number" ? opts.maxRetries : 1;
  const generationStage = trace?.stages?.find((s) => s.name === "SQL generation");
  const effectiveModel =
    trace?.model ||
    (generationStage?.details?.model ? String(generationStage.details.model) : null) ||
    opts?.requestedModel ||
    "Default (Server)";
  const effectiveTier =
    trace?.tier === "byok"
      ? "BYOK (User API Key)"
      : trace?.tier === "free"
        ? "Free tier"
        : opts?.tier || "Free tier";

  ensureSpace(64);
  drawText(
    "QUERY OPTIONS & EXECUTION DETAILS",
    marginX,
    cursorY - 9,
    "F2",
    8.5,
    [0.28, 0.35, 0.38],
  );
  cursorY -= 14;

  const optionSummaryLines: string[] = [
    `Selected Options:   Top-k = ${effectiveTopK}   |   FK Expansion = ${effectiveFk ? "Enabled" : "Disabled"}   |   Max Retries = ${effectiveMaxRetries}`,
    `Execution Context:  Dataset = ${datasetLabel}   |   Tier = ${effectiveTier}   |   Model = ${effectiveModel}${trace?.fallback_used ? " (Fallback used)" : ""}`,
  ];
  if (trace) {
    const modeLabel = formatExecutionMode(trace.execution_mode);
    const retryCount = typeof trace.retry_count === "number" ? trace.retry_count : 0;
    const retryCats =
      Array.isArray(trace.retry_reasons) && trace.retry_reasons.length > 0
        ? ` (${trace.retry_reasons.join(", ")})`
        : "";
    optionSummaryLines.push(
      `Pipeline Summary:   Total Latency = ${formatTotalLatency(trace.total_duration_ms)}   |   Retries = ${retryCount}${retryCats}   |   Mode = ${modeLabel}`,
    );
  } else {
    optionSummaryLines.push(
      `Pipeline Summary:   Status = ${input.status || "success"}   |   Attempts = ${input.attempts ?? 1}   |   Mode = Read-only`,
    );
  }

  const optionsBoxHeight = optionSummaryLines.length * 13 + 12;
  ensureSpace(optionsBoxHeight);
  drawRect(
    marginX,
    cursorY,
    contentWidth,
    optionsBoxHeight,
    [0.955, 0.968, 0.975],
    [0.82, 0.86, 0.89],
  );
  optionSummaryLines.forEach((line, idx) => {
    drawText(
      truncateCellText(line, Math.floor((contentWidth - 20) / 4.4)),
      marginX + 10,
      cursorY - 14 - idx * 13,
      "F1",
      8.5,
      [0.12, 0.16, 0.2],
    );
  });
  cursorY -= optionsBoxHeight + 10;

  if (trace && Array.isArray(trace.stages) && trace.stages.length > 0) {
    ensureSpace(36);
    drawText(
      "STAGE-BY-STAGE EXECUTION TRACE (EXPANDED DETAILS)",
      marginX,
      cursorY - 9,
      "F2",
      8.2,
      [0.28, 0.35, 0.38],
    );
    cursorY -= 13;

    trace.stages.forEach((stage, stageIdx) => {
      const detailLines = describeStageDetails(stage, trace);
      const wrappedDetailLines: string[] = [];
      for (const dLine of detailLines) {
        wrappedDetailLines.push(
          ...wrapTextByChars(dLine, Math.floor((contentWidth - 34) / 4.6)),
        );
      }
      const stageBlockHeight = 16 + wrappedDetailLines.length * 11.5 + 6;
      ensureSpace(stageBlockHeight);

      const stageBg: [number, number, number] =
        stageIdx % 2 === 0 ? [0.976, 0.984, 0.988] : [0.945, 0.96, 0.968];
      drawRect(
        marginX,
        cursorY,
        contentWidth,
        stageBlockHeight,
        stageBg,
        [0.85, 0.89, 0.91],
      );

      const badge = formatStageStatusBadge(stage.status);
      const badgeColor: [number, number, number] =
        stage.status === "failed" ? [0.78, 0.22, 0.18] : [0.16, 0.46, 0.22];
      drawText(badge, marginX + 8, cursorY - 12, "F3", 8.0, badgeColor);
      drawText(stage.name, marginX + 46, cursorY - 12, "F2", 8.6, [0.1, 0.14, 0.18]);

      const durationStr = formatExactDuration(stage.duration_ms);
      const durationX = marginX + contentWidth - durationStr.length * 4.8 - 10;
      drawText(durationStr, durationX, cursorY - 12, "F3", 8.0, [0.28, 0.35, 0.4]);

      wrappedDetailLines.forEach((dLine, dIdx) => {
        drawText(
          dLine,
          marginX + 20,
          cursorY - 24 - dIdx * 11.5,
          "F1",
          8.0,
          [0.25, 0.31, 0.35],
        );
      });

      cursorY -= stageBlockHeight + 4;
    });
    cursorY -= 8;
  }

  // 4. Generated SQL Section
  if (input.sql) {
    const sqlCharsPerLine = Math.floor((contentWidth - 20) / 5.0);
    const sqlLines = wrapTextByChars(input.sql, sqlCharsPerLine);
    ensureSpace(36);
    drawText("GENERATED SQL (READ-ONLY)", marginX, cursorY - 9, "F2", 8.5, [0.28, 0.35, 0.38]);
    cursorY -= 14;

    for (let i = 0; i < sqlLines.length; i += 1) {
      ensureSpace(14);
      drawRect(marginX, cursorY, contentWidth, 13, [0.09, 0.13, 0.15]);
      drawText(sqlLines[i], marginX + 10, cursorY - 9.5, "F3", 8.2, [0.847, 0.953, 0.42]);
      cursorY -= 13;
    }
    cursorY -= 14;
  }

  // 5. Results Table Section
  ensureSpace(44);
  const resultsHeading = isTruncated
    ? `QUERY RESULTS (SHOWING FIRST ${PDF_MAX_ROWS} OF ${allRows.length} ROWS)`
    : `QUERY RESULTS (${allRows.length} ROW${allRows.length === 1 ? "" : "S"})`;
  drawText(resultsHeading, marginX, cursorY - 9, "F2", 8.5, [0.28, 0.35, 0.38]);
  cursorY -= 15;

  if (!columns.length || !cappedRows.length) {
    drawRect(marginX, cursorY, contentWidth, 26, [0.965, 0.976, 0.98], [0.83, 0.87, 0.89]);
    drawText(
      "The query completed successfully but returned no rows.",
      marginX + 10,
      cursorY - 16,
      "F1",
      9,
      [0.35, 0.42, 0.45],
    );
    cursorY -= 32;
  } else {
    const colWidths = computeColumnWidths(columns, cappedRows, contentWidth);
    const headerRowHeight = 20;
    const dataRowHeight = 16.5;

    const drawTableHeader = () => {
      drawRect(marginX, cursorY, contentWidth, headerRowHeight, [0.094, 0.137, 0.161]);
      let colX = marginX;
      columns.forEach((col, idx) => {
        const w = colWidths[idx];
        const maxChars = Math.max(4, Math.floor((w - 10) / 4.8));
        drawText(
          truncateCellText(col, maxChars),
          colX + 5,
          cursorY - 13.5,
          "F2",
          8.2,
          [0.847, 0.953, 0.42],
        );
        colX += w;
      });
      cursorY -= headerRowHeight;
    };

    ensureSpace(headerRowHeight + dataRowHeight);
    drawTableHeader();

    cappedRows.forEach((row, rowIndex) => {
      ensureSpace(dataRowHeight, drawTableHeader);
      const bg: [number, number, number] =
        rowIndex % 2 === 0 ? [0.988, 0.992, 0.995] : [0.949, 0.965, 0.973];
      drawRect(marginX, cursorY, contentWidth, dataRowHeight, bg);
      drawLine(
        marginX,
        cursorY - dataRowHeight,
        marginX + contentWidth,
        cursorY - dataRowHeight,
        [0.86, 0.89, 0.91],
        0.4,
      );

      let colX = marginX;
      columns.forEach((col, colIdx) => {
        const w = colWidths[colIdx];
        const rawVal = row[col];
        const isNull = rawVal === null || rawVal === undefined;
        const displayVal = isNull ? "NULL" : String(rawVal);
        const maxChars = Math.max(4, Math.floor((w - 10) / 4.5));
        drawText(
          truncateCellText(displayVal, maxChars),
          colX + 5,
          cursorY - 11.5,
          isNull ? "F3" : "F1",
          7.8,
          isNull ? [0.48, 0.54, 0.58] : [0.11, 0.15, 0.18],
        );
        colX += w;
      });
      cursorY -= dataRowHeight;
    });

    if (isTruncated) {
      ensureSpace(22);
      cursorY -= 6;
      drawText(
        `Note: PDF output is capped at ${PDF_MAX_ROWS} rows for readability. Use Export CSV to download all ${allRows.length} rows.`,
        marginX,
        cursorY - 10,
        "F1",
        8,
        [0.42, 0.48, 0.52],
      );
      cursorY -= 16;
    }
  }

  if (currentCommands.length > 0) {
    pages.push(currentCommands);
  }

  // Add footer with page numbers to every page
  const totalPages = pages.length;
  pages.forEach((pageCmds, idx) => {
    const footerY = 22;
    pageCmds.push("BT");
    pageCmds.push("/F1 8.0 Tf");
    pageCmds.push("0.450 0.510 0.540 rg");
    pageCmds.push(`${marginX.toFixed(2)} ${footerY.toFixed(2)} Td`);
    pageCmds.push("(SchemaDrill  -  Safe Read-Only SQL Analytics) Tj");
    pageCmds.push("ET");

    const pageStr = `Page ${idx + 1} of ${totalPages}`;
    const pageStrX = pageWidth - marginX - pageStr.length * 4.2;
    pageCmds.push("BT");
    pageCmds.push("/F1 8.0 Tf");
    pageCmds.push("0.450 0.510 0.540 rg");
    pageCmds.push(`${pageStrX.toFixed(2)} ${footerY.toFixed(2)} Td`);
    pageCmds.push(`(${escapePdfText(pageStr)}) Tj`);
    pageCmds.push("ET");
  });

  // Assemble PDF 1.4 objects
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const offsets: number[] = [0]; // index 0 is free object
  let byteLength = 0;

  const pushStr = (str: string) => {
    const bytes = encoder.encode(str);
    chunks.push(bytes);
    byteLength += bytes.byteLength;
  };

  const startObj = (id: number) => {
    offsets[id] = byteLength;
    pushStr(`${id} 0 obj\n`);
  };

  const endObj = () => {
    pushStr("endobj\n");
  };

  pushStr("%PDF-1.4\n");

  // 1: Catalog, 2: Pages, 3: Helvetica, 4: Helvetica-Bold, 5: Courier
  const firstPageObjId = 6;
  const pageObjIds: number[] = [];
  for (let i = 0; i < totalPages; i += 1) {
    pageObjIds.push(firstPageObjId + i * 2);
  }

  startObj(1);
  pushStr("<< /Type /Catalog /Pages 2 0 R >>\n");
  endObj();

  startObj(2);
  pushStr(
    `<< /Type /Pages /Kids [ ${pageObjIds.map((id) => `${id} 0 R`).join(" ")} ] /Count ${totalPages} >>\n`,
  );
  endObj();

  startObj(3);
  pushStr("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>\n");
  endObj();

  startObj(4);
  pushStr(
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>\n",
  );
  endObj();

  startObj(5);
  pushStr("<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>\n");
  endObj();

  pages.forEach((pageCmds, idx) => {
    const pageObjId = firstPageObjId + idx * 2;
    const contentObjId = pageObjId + 1;
    const streamContent = pageCmds.join("\n") + "\n";
    const streamByteLen = encoder.encode(streamContent).byteLength;

    startObj(pageObjId);
    pushStr(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth.toFixed(2)} ${pageHeight.toFixed(2)}] /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents ${contentObjId} 0 R >>\n`,
    );
    endObj();

    startObj(contentObjId);
    pushStr(`<< /Length ${streamByteLen} >>\nstream\n${streamContent}endstream\n`);
    endObj();
  });

  const totalObjects = offsets.length;
  const xrefOffset = byteLength;
  pushStr(`xref\n0 ${totalObjects}\n`);
  pushStr("0000000000 65535 f \n");
  for (let id = 1; id < totalObjects; id += 1) {
    const padded = String(offsets[id]).padStart(10, "0");
    pushStr(`${padded} 00000 n \n`);
  }
  pushStr(
    `trailer\n<< /Size ${totalObjects} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
  );

  const output = new Uint8Array(byteLength);
  let pos = 0;
  for (const chunk of chunks) {
    output.set(chunk, pos);
    pos += chunk.byteLength;
  }
  return output;
}

export function downloadPdfReport(
  input: PdfReportInput,
  filename = "schemadrill-results.pdf",
): void {
  const bytes = buildPdfBytes(input);
  const blob = new Blob([bytes.buffer as ArrayBuffer], { type: "application/pdf" });
  triggerBrowserDownload(blob, filename);
}
