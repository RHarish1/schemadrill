# Module Reference

This is the module-level map for SchemaDrill. The rendered architecture diagram is in the [README](../README.md#architecture). The request path is:

```text
app.main
  -> app.pipeline
      -> app.retrieval -> app.embedding -> app.db
      -> app.prompting -> app.llm_client
      -> app.sql_guard -> app.executor -> app.db
```

Evaluation is deliberately separate from serving:

```text
evaluation.runner
  -> eval.run_benchmark
      -> app.pipeline
      -> eval.evaluator
          -> eval.result_compare
          -> app.sql_judge
```

## Runtime Modules

| Module | Responsibility | Main contract | Side effects |
| --- | --- | --- | --- |
| `app/main.py` | FastAPI application and HTTP boundary | `POST /query` accepts `QueryRequest`, returns `QueryResult`; `GET /api/me` returns `AuthenticatedUser` | Converts uncaught request errors to HTTP 500; closes the DB pool on shutdown |
| `app/config.py` | Environment-backed configuration | `Settings`, `get_settings()` | Loads `.env`; configures logging once through an LRU cache |
| `app/models.py` | Pydantic request, response, DDL, retry, and exception contracts | `AuthenticatedUser`, `QueryRequest`, `QueryResult`, `SqlResponse`, `RetryState`, `DDLBlock` | Validation only; no I/O |
| `app/pipeline.py` | Ordered query workflow and retry orchestration | `PipelineStage: RetryState -> RetryState`; `run_pipeline()` | Calls retrieval, Gemini, PostgreSQL `EXPLAIN`, and execution; retains only latest retry SQL/error |
| `app/prompting.py` | Stateless Gemini prompt construction | `build_messages()`, `messages_for_retry()` | Logs the exact generated prompt; does not accumulate prior messages |
| `app/llm_client.py` | Structured Gemini generation and token counting | `generate()`, `count_prompt_tokens()` | Network calls to Gemini; validates output as `SqlResponse` through Instructor |
| `app/retrieval.py` | Retrieval provider abstraction, pgvector lookup, and optional one-hop FK expansion | `RetrievalProvider`, `get_retrieval_provider()` | Embeds the question and queries `schema_embeddings`; optional catalog neighbors are appended in deterministic order; Qdrant/FAISS are explicit v0-unavailable providers |
| `app/embedding.py` | Config-selected sentence-transformer loading and embedding | `embed_passage()`, `embed_query()` | Loads and caches the configured embedding model; CPU model loading can be expensive |
| `app/db.py` | Shared PostgreSQL connection-pool boundary | `readonly_connection()`, `close_pool()` | Opens pooled connections and sets transactions read-only |
| `app/sql_guard.py` | Deterministic SQL security boundary | `guard_sql()`, `SQLGuardResult`, `default_guard_config()` | Parses PostgreSQL ASTs; enforces single SELECT, RBAC table scope, blocked functions, and AST-based LIMIT policy |
| `app/auth.py` | Neon Auth JWT verification | `current_user(request)`, `current_user_id(request)` | Fetches and caches JWKS; accepts EdDSA/RS256; checks issuer (configured value and normalized origin) and optional audience; extracts `sub`, `email`, and `display_name`, or raises 401/503 |
| `app/tenant.py` | Per-user provisioning and dataset ACL | `provision_user()`, `get_user()`, `user_has_dataset()` | Ensures `users`/`user_datasets` tables; captures/backfills user email and display name; on first sight of a user seeds default datasets. Uses `INGEST_DATABASE_URL` when serving as the read-only role |
| `app/reliability.py` | Rate limiting and provider metrics | `RedisRateLimiter`, `AllowAllRateLimiter`, `ReliabilityMetrics` | Redis calls; counters for rejections and timeouts |
| `app/executor.py` | PostgreSQL dry-run and bounded result execution | `dry_run()`, `execute()` | Runs `EXPLAIN`, fetches at most `max_result_rows + 1`, raises `RowCapExceeded`, returns a DataFrame |

## Pipeline Stages

`build_pipeline_stages()` returns four stages in this order:

1. **Retrieval:** fills `retrieved_ddl` once. Empty retrieval is still considered complete and is not repeated on retries.
2. **Generation:** rebuilds the prompt from `RetryState`, calls the injected generator, and validates `SqlResponse`.
3. **Gate:** parses SQL and runs PostgreSQL `EXPLAIN`. Parse and dry-run errors update `last_error` and retry.
4. **Execution:** runs the query with a row cap. Row-cap errors retry; other database errors are terminal.

Every stage receives and returns a Pydantic-validated `RetryState`. Retrieval remains fixed; `last_sql` and `last_error` are replaced rather than appended.

The gate invokes `guard_sql()` before PostgreSQL `EXPLAIN`. Guard diagnostics may contain internal table/function details for logs and tracing, but pipeline failures return generic security or authorization messages so restricted table names are not disclosed.

## Ingestion

| Module | Responsibility |
| --- | --- |
| `ingest/schema_ingest.py` | Reads table metadata and foreign keys, renders one DDL block per table, embeds it, and upserts it into `schema_embeddings` |
| `scripts/init.sql` | Creates the database roles, schema-embedding table, vector extension, and indexes |
| `scripts/load_demo_dbs.sql` | Seeds the deterministic Chinook artist, album, and track data |

Ingestion uses the admin database URL and writes vectors. Serving uses the read-only database URL.

## Evaluation Modules

### `eval/` implementation namespace

`eval/evaluator.py` executes gold and generated SQL independently and returns `SQLEvalResult`. It records execution flags, errors/categories, canonical `result_match`, separate AST match, columns, row counts, and wall-clock execution time.

`eval/result_compare.py` exposes one comparator, `compare_result_sets()`, with `compare_results()` as a compatibility alias. It compares result sets, not SQL text. Column order is significant; row order is ignored; duplicate rows are preserved; NULL is explicit; numeric representations are normalized.

`eval/models.py` defines `SQLEvalResult` and `SQLEvalSummary` as Pydantic reporting contracts.

`eval/run_benchmark.py` is the legacy-compatible live benchmark runner. It calls the pipeline for generated SQL, then evaluates gold versus generated results.

`eval/benchmark.py` is the infrastructure benchmark. It checks pgvector, the HNSW index, seeded Chinook tables, and vector lookup latency. It is not SQL correctness evaluation.

### `evaluation/` dataset namespace

`evaluation/chinook/cases.json` is the five-case deterministic regression dataset.

`evaluation/runner.py` is the dataset-facing CLI. Normal mode runs live generation. `--gold-as-generated` runs the credential-free evaluator/database smoke test used by Docker CI; it validates the evaluator and fixture, not model quality.

`evaluation/bird/README.md` documents the opt-in BIRD Mini-Dev location and command. BIRD data is intentionally not committed and is not part of push/PR CI.

## CI Ownership

`.github/workflows/ci.yml` has three responsibilities:

- **Frontend job:** `npm ci`, `npm audit --audit-level=high`, and `vite build`.
- **Quality job:** Ruff, Black, and Python tests.
- **Docker/Postgres job:** populate the ignored wheel cache, build the offline image, validate Compose, run the pgvector infrastructure benchmark, run the Chinook evaluator smoke test, and check API health.

`.github/workflows/codeql.yml` runs CodeQL static security analysis across `actions`, `javascript-typescript`, and `python` on push, pull requests, weekly schedule, and manual `workflow_dispatch` (pinned to `github/codeql-action@v4.39.1` so runners reuse the pre-cached CodeQL `2.27.3` bundle instead of failing on upstream release-download `504` timeouts).

The normal CI smoke test uses gold SQL as controlled generated SQL so it does not require a Gemini credential. Live generation and BIRD evaluation belong in manual or scheduled workflows once credentials, dataset licensing, and regression thresholds are configured.
