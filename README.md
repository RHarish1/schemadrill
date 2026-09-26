# SchemaDrill

SchemaDrill is a retrieval-augmented PostgreSQL text-to-SQL service. It embeds one DDL block per table into pgvector, retrieves the top three tables for a question, sends the retrieved schema and question to Gemini, validates the generated SQL, and executes it with a bounded retry loop.

## Current v0 Status

The repository currently implements the v0 runtime end to end: Docker Compose, PostgreSQL/pgvector, local `bge-small-en-v1.5` retrieval, structured Gemini SQL generation, PostgreSQL parsing and `EXPLAIN` retries, read-only execution, row-cap handling, and Markdown result generation.

The evaluation harness is also present. It runs the checked-in Chinook cases, compares the parsed generated SQL AST with each gold SQL statement immediately before execution, and reports SQL accuracy separately from execution success. The result table is generated and stored in the run output, but its contents are intentionally not compared yet.

This is still a deliberately small v0 benchmark: the checked-in dataset is deterministic Chinook smoke data, not the planned multi-database Spider/BIRD subset. RBAC enforcement, stronger SQL safety rules, result/golden-table comparison, judge-model calls, caching, tracing, and frontend work remain outside v0.

## Run Locally

Prerequisites: Docker with Compose and a Gemini API key.

```sh
cp .env.example .env
# Put a Gemini Developer API key in GEMINI_API_KEY inside .env.
docker compose up -d --build
```

Wait until both services are healthy:

```sh
docker compose ps
curl http://localhost:8000/health
```

The first ingestion loads `BAAI/bge-small-en-v1.5` on CPU. Ingest the demo Chinook schema once after the database is ready:

```sh
docker compose exec app python -m ingest.schema_ingest chinook
```

The Compose database seed creates five deterministic Chinook cases with artists, albums, and tracks. To start from a clean database and reload the schema and rows:

```sh
docker compose down -v
docker compose up -d db
docker compose exec app python -m ingest.schema_ingest chinook
```

Open Swagger at <http://localhost:8000/docs> or call the API:

```sh
curl -X POST http://localhost:8000/query \
	-H 'Content-Type: application/json' \
	-d '{"db":"chinook","question":"List the names of all artists."}'
```

View retrieval similarity scores, the exact Gemini prompt, and generated SQL:

```sh
docker compose logs -f app
```

The API uses a read-only Postgres role. v0 intentionally keeps `sql_guard` to parse validity; stronger allowlists and LIMIT injection are deferred to v1.

## Prompt Benchmark

Run every prompt sequentially through retrieval, Gemini, SQL validation, the offline parsed-SQL judge, and Postgres execution:

```sh
docker compose exec app python -m eval.run_benchmark \
	--questions eval/questions.json \
	--output /tmp/schemadrill-results.jsonl
```

The command prints each prompt result immediately and ends with execution accuracy, SQL accuracy, mean latency, and total runtime. Execution accuracy currently means that the pipeline returned a successful execution; SQL accuracy means that the parsed generated SQL matched the checked-in gold SQL. No returned table values are compared yet. Run a smaller check with:

```sh
docker compose exec app python -m eval.run_benchmark --db chinook --limit 1
```

Each JSONL record includes `gold_sql`, `generated_sql`, `sql_match`, `judge_reason`, and the generated `table_markdown`. The judge is deterministic and offline: it parses both PostgreSQL statements with `sqlglot` and compares their ASTs. It does not block execution when SQL differs.

The legacy entry point delegates to the same runner:

```sh
docker compose exec app python -m eval.run_eval --questions eval/questions.json
```

## PostgreSQL

You do not need a remote PostgreSQL instance for local development. Compose starts `pgvector/pg16`, creates the `schemadrill` database, enables the vector extension, creates the HNSW-backed `schema_embeddings` table, and loads the demo `chinook` schema.

A remote instance such as Neon is optional for shared or deployed environments. Set both `DATABASE_URL` and `INGEST_DATABASE_URL` to that instance, enable the `vector` extension, run `scripts/init.sql`, create/load the target schemas, and run ingestion before calling `/query`.

## Local Checks

```sh
source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
black --check .
pytest -q
docker compose config
docker compose up -d db
docker compose exec app python -m ingest.schema_ingest chinook
docker compose run --rm app python -m eval.benchmark
docker compose down -v
```

## CI

GitHub Actions runs Ruff, Black, pytest, Docker Compose validation, Postgres/pgvector checks, and the vector benchmark. Docker and CI install CPU-only PyTorch; the local `wheels/` cache is intentionally ignored because it is not needed for the build and is too large for a normal GitHub commit.
