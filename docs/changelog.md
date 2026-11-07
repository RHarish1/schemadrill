# Shipped Features and Bugs Solved

Derived from `git log` (2026-09-26 to 2026-11-07). Hashes are abbreviated.

## Timeline of shipped features

| Date | Milestone | Key commits |
| --- | --- | --- |
| 09-26 | **Bootstrap**: FastAPI, pgvector retrieval, Gemini, Docker Compose, CI, Chinook fixture. Evaluation pipeline and SQL judge | `af43584`, `d7c3e8b` |
| 09-28 | **Pipeline refactor**: retry state, retrieval boundary, validated stages. **Semantic evaluation** (result-set comparison). Offline Docker builds. Module docs. **SQL guard** | `54c1105`, `7b5a7f7`, `23349c7`, `e2650ba`, `b6bdae8`, `c116c1e` |
| 09-29 | Expanded eval questions, configurable self-correction, FK retrieval expansion, benchmark matrix runner, lint fixes | `3d5807a`, `6fdd915`, `a50da57`, `2f4a872`, `d3bc8ed` |
| 10-11 | **React frontend**, frontend CI checks, Cloud Run prep, Neon production branch and skills | `4454f40`, `4ecc0b8`, `f397f44`, `62eb9e2` |
| 10-20 | Frontend API status sync, `.gcloudignore` | `10cc984` |
| 10-25 | **Stage-level instrumentation**, multi-dataset fixtures (ecommerce, organization, complex_synthetic), Neon fixture migration, **dataset catalog API and selector**, per-dataset example questions, **Redis reliability** (rate limit, concurrency, backoff) | `1c9def2`, `c88289a`, `a5027bb`, `efaeda7`, `2928106`, `29ff10b` |
| 11-01 | BIRD loader, embedding warm-up, full-schema baseline, prompt tweaks, **Neon Auth tenant foundation**, production auth config | `09a2425`, `f361027`, `2bcea0a`, `22f91ef`, `ff4fa42` |
| 11-03 | Frontend audit blockers resolved | `4555052` |
| 11-06 | **Authenticated default dataset provisioning** | `231e1cb` |
| 11-07 | Readonly auth and tenant provisioning fix, wheel handling, issuer normalization, **Cloud Build CI/CD with live-eval deploy gate** | `7a13432`, `ffe92c9`, `1d34e0a`, `fb62b1d`, `6f4b804`, `b2a71bf`, `73d9a4e` |

## Feature inventory

- **Query API** (`POST /query`), datasets API (`/api/datasets`), metrics, public `/health`.
- **Retrieval**: pgvector HNSW over per-table DDL, optional FK expansion, full-schema baseline, pluggable provider interface.
- **Generation**: Gemini via Instructor, structured `SqlResponse`, stateless bounded self-correction.
- **Safety**: AST SQL guard (24 adversarial tests), `EXPLAIN` gate, read-only transaction and role, row cap.
- **Reliability**: Redis rate limit per user, concurrency admission, provider backoff and timeouts.
- **Auth and tenancy**: Neon Auth (Google provider) JWT verification, per-user dataset registry, idempotent provisioning.
- **Frontend**: React/Vite SPA, dataset dropdown, example questions, API status, auth session.
- **Evaluation**: result-set accuracy, execution success, AST similarity, retries, tokens, stage latency (mean/median/p95), ablation matrix, repeated runs, BIRD loader, Chinook cases.
- **CI/CD**: GitHub Actions (lint, tests, frontend, Docker smoke) and Cloud Build (pytest and live Gemini 100% gate before deploy).

## Bugs solved

| Area | Problem | Fix | Commit |
| --- | --- | --- | --- |
| Retry loop | Messages accumulated across attempts, so token use grew | Stateless `RetryState` rebuilds each prompt | `54c1105` |
| Retry loop | Pydantic forward-reference and mis-nested patches caused `UnboundLocalError` | Reordered models, recreated module cleanly | `7b5a7f7` |
| Evaluation | "Executed OK" was reported as accuracy | Separate result-set match, execution success, AST diagnostic | `23349c7` |
| Evaluation | Comparator dropped duplicates (`set(rows)`), mishandled ints vs decimals | Multiset comparison and numeric normalization | `23349c7`, `a23e914` |
| Evaluation | Unaliased aggregates produced false mismatches | Compare column count, order, and values (Spider/BIRD style) | `b2a71bf` |
| Evaluation | Smoke tests exited 0 on failed queries | Fail fast when accuracy or success < 1.0 | `7a13432`, `1d34e0a` |
| Docker | Large torch wheel downloads broke the build | Offline `wheels/` cache and `--no-index` install | `e2650ba` |
| Docker | `ModuleNotFoundError: ingest` | Copy `ingest/` into image | earlier fix |
| Cloud Build | 331 MB wheel upload | `.gcloudignore` for `*.whl`, PyPI fallback in Dockerfile | `ffe92c9` |
| Cloud Build | Custom service account needed a logs bucket | `CLOUD_LOGGING_ONLY` | `6f4b804` |
| Cloud Build | Eval step needed `DATABASE_URL` and `GEMINI_API_KEY` from Secret Manager (`valueFrom.secretKeyRef`) | Inject secrets via `availableSecrets.secretManager` (`secretEnv`) | `73d9a4e`, latest |
| Ingestion | Remote schema ingestion connection dropped | Keep-alive fix | `7ba112b`, `c227fe0` |
| Frontend | API status out of sync | State sync fix | `10cc984` |
| Frontend | Hardcoded auth URL in image | Export `VITE_NEON_AUTH_URL` only when set | `1d34e0a`, `22f91ef` |
| CI | Frontend `npm audit` blockers | Dependency overrides | `4555052` |
| CI | Secrets printed by compose config | Avoid printing | `07e423d` |
| CI | Ruff/Black failures | Formatting fixes | `d3bc8ed` |
| Database | Read-only role lost its login/password, so 5 min `PoolTimeout` | Restore `LOGIN PASSWORD`/`ALTER ROLE` in `init.sql` | `7a13432` |
| Database | Synthetic datasets unreadable by serving role | `GRANT USAGE/SELECT` in dataset init | `7a13432` |
| Tenancy | 500 `InsufficientPrivilege` on `/api/datasets` and `/query` | Use `INGEST_DATABASE_URL` for provisioning when serving as read-only | `7a13432` |
| Auth | JWT issuer mismatch when `/neondb/auth` suffix was configured | Accept configured issuer and normalized origin | `fb62b1d` |
| Tenancy | Dataset id was globally unique | Composite PK `(dataset_id, user_id)` | `2bcea0a`, `231e1cb` |
