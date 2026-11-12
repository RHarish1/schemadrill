# Shipped Features and Bugs Solved

Derived from `git log` (2026-09-26 to 2026-11-10). Hashes are abbreviated.

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
| 11-08 | **v0.1.1**: gitignore private self-notes (`notes/`, `AGENTS.md`, `GEMINI.md`) | `cc1c910` |
| 11-08 | **v0.2.0**: **Google auth completion** — JWT email/display name capture and backfill, `GET /api/me`, signed-in identity chip, cross-tab sign-out, session-expiry refresh and draft preservation | `acf122d` |
| 11-08 | **v0.2.1**: patch bump to retrigger GitHub dynamic CodeQL scan after upstream 504 timeout | `92d3353` |
| 11-08 | **v0.2.2**: add committed `.github/workflows/codeql.yml` (`v4.39.1`, Python + JS/TS + Actions) to use runner-cached CodeQL `2.27.3` | `88dacf0` |
| 11-08 | **v0.2.3**: fix production `500` (`psycopg.errors.IndeterminateDatatype`) on user profile backfill for existing users | `faf0bfc` |
| 11-10 | **Reliability**: unwrap provider retries, request-scoped logging, sanitized `429`/`503`/`500` error responses | `e9bb1c0` |
| 11-10 | **v0.3.0**: **Execution Trace observability** — separate internal telemetry and sanitized public `execution_trace` API (including failed queries), plus collapsible React Execution Trace drawer | `54227fc` |
| 11-10 | **v0.3.1**: **Read-only boundary hardening & trace detail breakdown** — full-AST rejection of mutation/DDL/locking/command nodes (including data-modifying CTEs, `SELECT ... INTO`, `FOR UPDATE`, `set_config`), `SELECT`-only `schemadrill_readonly` grants on tenant tables (`scripts/tenant.sql`), and `Show details` exposure of `top_k`, `fk_expansion`, independent validation check statuses (`read_only_policy`, `sql_syntax`, `dry_run`), and `execution_mode` | `5ba478c` |
| 11-10 | **v0.3.2**: add repo-local `schemadrill-workflow` agent skill (`.agents/skills/`) covering ADRs, SemVer sync, verification, and commit standards | this commit |
| 11-12 | **v0.4.0**: **Per-request query tuning (`top_k`, `fk_expansion`, `max_retries`)** — expose bounded optional `top_k` (`1..10`), `fk_expansion`, and `max_retries` (`0..3`) on authenticated `POST /query` (mapped to request-scoped `Settings` and `1 + max_retries` attempt budget), plus authenticated React query options (`top_k` 1–5 dropdown with tradeoff tooltip, `max_retries` 0–3 dropdown, `fk_expansion` switch, and draft/timeout alignment) | `7f25038` |
| 11-12 | **v0.4.1**: **Upstash Redis Cloud Run secret wiring & CI/CD docs** — add `REDIS_URL=schemadrill-redis-url:latest` to `cloudbuild.yaml` `--update-secrets`, annotate `.github/workflows/ci.yml` to keep external Redis secrets out of GitHub Actions, and document the Upstash / local Compose / CI Redis strategy in `docs/solution-design.md` and `docs/architecture.md` | this commit |
| 11-12 | **v0.5.0**: **Phase 1 BYOK (Bring Your Own Key)** — browser-held Gemini keys (`sessionStorage` only, no `localStorage`), `app/providers/` abstraction with per-request clients, `POST /api/llm/validate` with HMAC-fingerprinted model cache, free-tier vs BYOK Redis/in-memory rate limiting, `403 invalid_llm_key` & `429 llm_quota_exceeded` / `free_tier_exhausted` error semantics, sticky fallback model with monotonic deadline & call cap, and Settings modal + tier badge UI | this commit |

## Feature inventory

- **Query API** (`POST /query`), datasets API (`/api/datasets`), user profile API (`GET /api/me`), metrics, public `/health`.
- **Retrieval**: pgvector HNSW over per-table DDL, optional FK expansion, full-schema baseline, pluggable provider interface.
- **Generation**: Gemini via Instructor, structured `SqlResponse`, stateless bounded self-correction.
- **Safety**: Full-AST SQL guard (35 adversarial tests, rejecting nested mutations/CTEs, `SELECT ... INTO`, `FOR UPDATE`, and `set_config`), `EXPLAIN` gate, read-only transaction and `SELECT`-only serving role, row cap.
- **Reliability**: Redis rate limit per user, concurrency admission, provider backoff and timeouts.
- **Auth and tenancy**: Neon Auth (Google provider) JWT verification, user profile capture/backfill (`email`, `display_name`), per-user dataset registry, idempotent provisioning.
- **Frontend**: React/Vite SPA, dataset dropdown, example questions, API status, signed-in user identity chip, cross-tab sign-out, 401 token refresh, session-expiry prompt with OAuth draft preservation.
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
| CI | GitHub Default Setup CodeQL (`@v4`) deleted runner-cached `2.27.3` and failed with `504` fetching `2.27.4` | Switch to `.github/workflows/codeql.yml` pinned to `v4.39.1` (`2.27.3` toolcache) | `88dacf0` |
| Database | Read-only role lost its login/password, so 5 min `PoolTimeout` | Restore `LOGIN PASSWORD`/`ALTER ROLE` in `init.sql` | `7a13432` |
| Database | Synthetic datasets unreadable by serving role | `GRANT USAGE/SELECT` in dataset init | `7a13432` |
| Tenancy | 500 `InsufficientPrivilege` on `/api/datasets` and `/query` | Use `INGEST_DATABASE_URL` for provisioning when serving as read-only | `7a13432` |
| Tenancy | 500 `psycopg.errors.IndeterminateDatatype` (`$4 IS NOT NULL`) on `/api/datasets` and `/api/me` for existing users | Replace untyped `%s IS NOT NULL` with explicit `%s::text` casts inside `COALESCE` / `IS DISTINCT FROM` | `faf0bfc` |
| Auth | JWT issuer mismatch when `/neondb/auth` suffix was configured | Accept configured issuer and normalized origin | `fb62b1d` |
| Tenancy | Dataset id was globally unique | Composite PK `(dataset_id, user_id)` | `2bcea0a`, `231e1cb` |
