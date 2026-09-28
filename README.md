# SchemaDrill

SchemaDrill is a retrieval-augmented PostgreSQL text-to-SQL service. It embeds one DDL block per table into pgvector, retrieves the top three tables for a question, sends the retrieved schema and question to Gemini, validates the generated SQL, and executes it with a bounded retry loop.

See [docs/modules.md](docs/modules.md) for the module-by-module ownership map and [docs/implementation-notes.md](docs/implementation-notes.md) for the implementation decisions and troubleshooting history.

## Current v0 Status

The repository currently implements the v0 runtime end to end: Docker Compose, PostgreSQL/pgvector, local `bge-small-en-v1.5` retrieval, structured Gemini SQL generation, PostgreSQL parsing and `EXPLAIN` retries, read-only execution, row-cap handling, and Markdown result generation.

The evaluation harness distinguishes generated-query execution success, result-based Execution Accuracy (EX), AST similarity, result metadata, and SQL execution timing. Chinook is the fast deterministic regression dataset. BIRD Mini-Dev is an opt-in evaluation dataset and is not run on every push.

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

The command prints each prompt result immediately and reports result-based Execution Accuracy, execution success, AST match rate, result metadata, and timing. AST match remains a diagnostic and is not used as EX. Run a smaller check with:

```sh
docker compose exec app python -m eval.run_benchmark --db chinook --limit 1
```

Each JSONL record includes `gold_sql`, `generated_sql`, `execution_match`, `ast_match`, execution errors/categories, row counts, columns, and gold/generated SQL execution times. The result comparator preserves duplicate rows, handles NULL and numeric representations, ignores row order, and keeps column order significant.

The legacy entry point delegates to the same runner:

```sh
docker compose exec app python -m eval.run_eval --questions eval/questions.json
```

## Dataset Evaluation

Use `evaluation/` for dataset-facing commands and cases. The older `eval/` package remains the implementation and backward-compatible CLI location.

Run the five deterministic Chinook cases locally after starting the database and ingesting the schema:

```sh
docker compose exec app python -m evaluation.runner \
	--questions evaluation/chinook/cases.json \
	--dataset chinook \
	--output /tmp/schemadrill-chinook.jsonl
```

Run the evaluator/database smoke check without Gemini:

```sh
docker compose run --rm app python -m evaluation.runner \
	--gold-as-generated --dataset chinook
```

This controlled check should report 100% EX and AST match for the seeded gold cases. It validates the evaluator and database fixture, not model quality. BIRD Mini-Dev belongs in a manual/nightly run once its local dataset and reference semantics are configured:

```sh
python -m evaluation.runner \
	--questions evaluation/bird/mini_dev.json \
	--dataset bird-mini-dev
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
docker compose run --rm app python -m evaluation.runner --gold-as-generated --dataset chinook
docker compose down -v
```

## CI

GitHub Actions runs Ruff, Black, pytest, Docker Compose validation, Postgres/pgvector checks, the vector benchmark, and the credential-free Chinook evaluator smoke test. The smoke test validates EX, AST, and result execution against the seeded gold SQL without calling Gemini. BIRD Mini-Dev is intentionally excluded from push/PR CI and should run manually or nightly.
