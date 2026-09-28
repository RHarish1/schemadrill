# SchemaDrill Implementation Notes

This document records the implementation decisions, problems encountered, and validation results from the retry-state, pipeline-stage, and SQL-evaluation work.

## Scope And Sequence

The work was completed in three slices:

1. Retry context handling and retrieval-provider boundary.
2. Pydantic-validated pipeline stages.
3. SQL semantic evaluation and benchmark reporting.

The first two slices were committed separately:

- `d4a8c1f` - `Refactor retry state and retrieval boundary`
- `b7e14ec` - `Refactor pipeline into validated stages`

The SQL evaluator changes are currently in the worktree with the tests described below.

## Initial Retry-Loop Finding

The original `app/pipeline.py` created one Gemini `messages` list, passed it to every generation call, and appended feedback after parse and dry-run failures. That was conversational message accumulation, not stateless reconstruction. Attempt 3 therefore contained the schema/question plus all earlier failure messages.

The chosen fix was a Pydantic `RetryState` containing only:

- `question`
- `retrieved_ddl`, retrieved once
- `attempt`
- `last_sql`
- `last_error`

`messages_for_retry()` reconstructs the system and user messages from that state for every attempt. The prior SQL and error are included only as the latest retry context. The Gemini client also exposes a provider token-count helper; tests use a deterministic local estimate so they do not require an API key.

### Retry Problems And Resolutions

- **State declaration order:** `RetryState` initially referenced `DDLBlock` before it was declared. It was moved below `DDLBlock` so Pydantic can evaluate the annotation.
- **Existing test seam:** removing the imported `feedback` symbol broke tests that monkeypatched `app.pipeline.feedback`. The symbol was retained as a compatibility/diagnostic hook; it is not appended to the outgoing prompt.
- **Token-test ambiguity:** comparing a pristine attempt with a retry is not meaningful because a retry must contain one latest SQL/error pair. The regression test compares equal-sized latest retry contexts, which detects accumulated history without conflating expected retry context with accumulation.
- **Malformed patching:** an intermediate retrieval edit left the function body and provider declarations nested or mis-indented. The module was corrected and immediately checked with focused tests and Ruff.

## Pipeline Stage Refactor

The original runtime was a plain retry waterfall, with no provider health checks or scoring. `app/pipeline.py` now exposes a common `PipelineStage` protocol:

```text
(state: RetryState) -> RetryState
```

`build_pipeline_stages()` returns the ordered list:

```text
retrieve -> generate -> gate -> execute
```

Each stage boundary calls `RetryState.model_validate()` before and after execution. Generation validates the response through `SqlResponse`; gate validates SQL parsing and PostgreSQL dry-run; execution stores the dataframe in the run context and returns the state.

Typed internal signals distinguish retryable failures (`parse_error`, `dry_run_error`, `row_cap`) from terminal execution/database failures. A retry re-enters the same stage list, while retrieval remains fixed for the whole run, including when the provider returns an empty result.

### Stage-Refactor Problems And Resolutions

- **Accidental nested replacement:** an initial patch inserted the new stage implementation inside the old `run_pipeline()` body, causing `RetryState` to become a local name and producing `UnboundLocalError`. The malformed module was deleted and recreated cleanly, then the five pipeline tests were rerun.
- **Behavior preservation:** the existing parse-error, dry-run-error, retry-budget, row-cap, and terminal-execution tests all remained green after the stage conversion.
- **State ownership:** mutable execution output is kept in a small per-run result context; retry state itself remains the serializable Pydantic contract passed between stages.

## Retrieval Abstraction

`app/retrieval.py` defines a `RetrievalProvider` protocol and a configuration-selected provider registry. `Settings.retrieval_backend` selects `pgvector`, `qdrant`, or `faiss` without branching in the pipeline. pgvector is the active v0 implementation. Qdrant and FAISS are explicit unavailable providers rather than pretending to be implemented.

The embedding model remains configuration-selected through `Settings.embedding_model`; the embedding loader already consumes that setting directly. No ablation behavior was added.

## SQL Evaluation Design

The previous benchmark called successful pipeline execution `execution_accuracy`, which could mark an incorrect query as correct. The new evaluation layer separates:

- `generated_executed` / execution success
- `execution_match` / primary semantic correctness
- `ast_match` / secondary sqlglot diagnostic
- row counts and result column names
- gold and generated execution errors

`eval/models.py` contains the Pydantic `SQLEvalResult` and `SQLEvalSummary` boundary models. `eval/evaluator.py` executes gold and generated SQL independently against the same database and records each outcome. `eval/run_benchmark.py` reports execution accuracy from result comparison and keeps the legacy `sql_accuracy` field as an AST-match alias for compatibility.

### Result Comparison Semantics

`eval/result_compare.py` currently uses these explicit rules:

- column order is significant;
- row order is ignored;
- duplicate rows are preserved with a multiset counter;
- SQL NULL is represented explicitly and is not stringified;
- integer, decimal, and float numeric representations are normalized where their numeric values match;
- byte-like values are compared as bytes;
- empty result sets compare equal when their column metadata also matches.

This avoids the common incorrect `set(rows)` implementation, which loses duplicate multiplicity. The result comparator is isolated so benchmark-specific reference semantics can be swapped or verified without changing pipeline execution.

### Evaluator Problems And Resolutions

- **Legacy benchmark test had no database:** the old unit test mocked the pipeline but not SQL execution. The new evaluator correctly attempted both database queries and exposed a connection failure, but the test waited for the pool timeout. The test now injects a deterministic evaluator result, keeping it offline while production still executes both statements.
- **Numeric normalization gap:** the first comparator version treated integer and decimal/float values as different. Integers were added to the numeric normalization path and validated with decimal, float, NULL, unordered-row, and duplicate-row cases.
- **AST versus EX:** an executable-but-wrong query is explicitly tested to produce `generated_executed=True` and `execution_match=False`; AST remains independently reported.
- **Model defaults:** mutable list/dict defaults in `SQLEvalSummary` were changed to Pydantic `default_factory` fields.
- **Failure metadata:** benchmark records now retain generated error text and evaluator error categories where available; `--dataset` now controls the summary dataset name.

## Validation

The final local checks completed successfully:

- `17 passed`
- Ruff: all checks passed
- Black: all files unchanged after formatting

The evaluator tests cover unordered rows, duplicate preservation, NULL values, numeric representations, executable-but-wrong SQL, execution failures, error-category precedence, empty results, AST diagnostics, and benchmark integration.

## Remaining Limitations

- Gold and generated wall-clock SQL execution times are collected. Plan cost, rows scanned, index usage, and other query-plan metrics are not yet collected; the evaluation models remain extensible for those fields.
- Qdrant and FAISS provider implementations are not wired yet; only their configuration boundary exists.
- The comparator's unordered/multiset/column-order policy is explicit and tested, but should still be checked against the exact BIRD or Spider reference evaluator before reporting those benchmark numbers as official.
- A first-pass error categorizer now labels common timeout, missing-object, missing-table, missing-column, invalid-function, type, connection, and other execution errors. Driver-specific SQLSTATE mapping remains a future refinement.
- The benchmark's system metrics such as LLM tokens, latency, retries, and retrieval latency remain separate from SQL correctness and are not folded into EX.

## Docker Build Failure And Fix

The first Docker CI attempts failed while pip streamed the large CPU Torch wheel from the PyTorch index. Increasing pip retries and timeouts did not solve the underlying broken connection; the failure moved between Torch and the later requirements install.

The fix was to make dependency installation offline and deterministic:

1. Populate the local `wheels/` cache with `pip download`, including all transitive requirements and the CPU Torch wheel.
2. Stop excluding `wheels/` from the Docker build context.
3. Copy `wheels/` into the image and install with `pip --no-index --find-links=/app/wheels`.
4. Add the same wheel-download step to CI before `docker compose build`, because the cache is intentionally ignored by Git and is generated in fresh CI workspaces.

The Dockerfile already copied `app/`, `ingest/`, `eval/`, and `scripts/`; this preserved the earlier `ModuleNotFoundError: No module named 'ingest'` fix. Compose still publishes host port 8000. A separate Quarry port conflict was not present during final verification; when another service owns that port, stop that service or change only the host side of the mapping.

Final container verification passed locally:

- offline wheel resolution passed;
- Docker image build passed;
- `python -m eval.benchmark` passed with the pgvector extension, HNSW index, and Chinook tables detected;
- `import ingest.schema_ingest` passed inside the image;
- the API container reached healthy state and `/health` returned `{"status":"ok"}`.

The five-case packaged Chinook sanity run also returned `execution_accuracy=1.0`, `ast_match_rate=1.0`, and successful execution for both gold and generated SQL when the gold SQL was used as the controlled generated input. This validates the evaluator and database fixture independently of Gemini generation.

## Dataset And CI Split

The dataset-oriented entry point is `python -m evaluation.runner`. Chinook cases live in `evaluation/chinook/cases.json` and remain the fast deterministic regression set. BIRD Mini-Dev is documented under `evaluation/bird/` as an opt-in dataset; its data is not committed. The older `eval/` package remains the implementation and compatibility layer; `evaluation/` is the dataset-facing namespace.

Normal CI runs unit/evaluator tests, Docker/pgvector infrastructure checks, and `evaluation.runner --gold-as-generated` for the seeded Chinook cases. That smoke test validates evaluator correctness without requiring Gemini credentials. A live generation benchmark should be run manually or nightly once credentials, model availability, and a regression threshold are configured; BIRD is intentionally not a required push check yet.
