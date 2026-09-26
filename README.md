# SchemaDrill

SchemaDrill is a retrieval-augmented PostgreSQL text-to-SQL service. It embeds one DDL block per table into pgvector, retrieves the top three tables for a question, sends the retrieved schema and question to Gemini, validates the generated SQL, and executes it with a bounded retry loop.

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

Run every prompt sequentially through retrieval, Gemini, SQL validation, and Postgres execution:

```sh
docker compose exec app python -m eval.run_benchmark \
	--questions eval/questions.json \
	--output /tmp/schemadrill-results.jsonl
```

The command prints each prompt result immediately and ends with execution accuracy, mean latency, and total runtime. Run a smaller check with:

```sh
docker compose exec app python -m eval.run_benchmark --db chinook --limit 1
```

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
docker compose run --rm app python -m eval.benchmark
docker compose down -v
```

## CI

GitHub Actions runs Ruff, Black, pytest, Docker Compose validation, Postgres/pgvector checks, and the vector benchmark. Docker and CI install CPU-only PyTorch; the local `wheels/` cache is intentionally ignored because it is not needed for the build and is too large for a normal GitHub commit.
