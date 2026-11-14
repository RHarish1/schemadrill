# Solution Design: How We Solve the Planned Problems

Initial technical approach for each hard problem in the [roadmap](roadmap.md).

> **Architectural Decision (Phase 1 & Phase 2a):** SchemaDrill holds zero server-side secret storage (no KMS, no database secret columns). LLM API keys and Neon Postgres credentials are session-held in the client browser (`sessionStorage`) and transported per request over encrypted HTTPS headers (`X-LLM-Api-Key` and `X-DB-Url`). Credentials live only in the worker thread locals for the duration of the request and are rigorously sanitized from logs, traces, exception representations, and responses.

## 1. Zero-Storage Credential Transport (Phase 1 BYOK & Phase 2a Connections)

**Problem:** we must store user secrets and be able to decrypt them later, but a database leak
must not expose them.

**Approach: envelope encryption.**
1. Create a Cloud KMS key (the key-encryption key, KEK). It never leaves KMS.
2. For each secret, generate a random data key (DEK) and encrypt the secret with AES-256-GCM.
3. Ask KMS to encrypt the DEK. Store `ciphertext`, `nonce`, and `encrypted_dek` in the table.
4. To use a secret: KMS-decrypt the DEK, decrypt the secret in memory, use it, and discard it.

```text
llm_keys(id, user_id, provider, label, last4, ciphertext, nonce, encrypted_dek,
         status, created_at, last_checked_at)
connections(id, user_id, label, host, database, role_name, access_level,
            ciphertext, nonce, encrypted_dek, status, created_at, last_checked_at)
```

- Bind `user_id` and the row id as AES-GCM associated data, so a ciphertext copied to another
  row or user fails to decrypt.
- Put it behind one `SecretStore` interface (`put`, `get`, `delete`). Local dev uses a
  fake key from an env var, so tests don't need KMS.
- **Logs:** add a logging filter that redacts `postgres://user:pass@`, `sk-...`, `AIza...`
  patterns, and never put secrets in exceptions. Add a test that fails if a secret appears in
  captured logs.
- Lower-effort alternative if KMS feels heavy: a single master key in Secret Manager used with
  AES-GCM. Weaker, but a reasonable first step behind the same interface.

## 2. Real tenant isolation

**Problem today:** all users share the same schemas, and `schema_embeddings` has no owner.

**Approach:**
- Add `connection_id` to `schema_embeddings` (primary key becomes
  `(connection_id, schema_name, table_name)`) and filter every retrieval query on it.
- Resolve the connection in one place: `get_connection(user_id, connection_id)` checks
  ownership and raises 404 if it isn't the user's (404, not 403, so ids can't be probed).
- Never accept a raw connection string or a `schema_name` from the request. The client sends
  only `connection_id` and the question.
- Keep curated demos as a special shared, read-only `connection_id` that every user may query.
- **Test:** create two users and two connections, then assert user A cannot retrieve embeddings,
  run queries on, or see the status of user B's connection. Make this an explicit CI test.

## 3. Connecting to a user's database safely

**SSRF (user supplies the host):**
- Parse the URL with a real parser (not regex). Require `sslmode=require` or stricter.
- Allow only hosts ending in `.neon.tech` (verify the current Neon domain list).
- Resolve the hostname and reject private, loopback, and link-local IPs (including after
  DNS). Connect to the resolved IP only if you pin it, otherwise re-check after connecting.
- Short `connect_timeout` (about 10 s) and `statement_timeout` (about 15 s).
- Strip or reject unusual parameters (`options`, `host=` overrides, multiple hosts).

**Detecting access level on connect:** run read-only checks, not writes.
```sql
SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user;
SELECT has_schema_privilege(current_user, 'public', 'CREATE');
SELECT count(*) FROM information_schema.table_privileges
 WHERE grantee = current_user AND privilege_type IN ('INSERT','UPDATE','DELETE');
```
Classify as `read_only`, `writer`, or `owner`. Also check `pg_has_role(current_user,
table_owner, 'USAGE')` for owned tables if the role owns objects.

**Owner strings:** warn and recommend the read-only role. Default to not storing owner-level
strings for query use. Offer an optional one-time "create a safe read-only role for me" step
that uses the owner string in memory only, then asks the user to paste the new read-only string.

**Query-time protection (keep and extend what exists):**
open the transaction as read-only, set `statement_timeout`, `idle_in_transaction_session_timeout`,
and `search_path`, run the existing AST guard, then keep the existing row cap.

**Connection pooling:** do not keep one global pool per tenant forever. Use a small bounded
cache (for example LRU of 20 pools, 1 to 2 connections each, idle eviction after a few
minutes) because Cloud Run has limited memory and Neon limits connections.

**Scale-to-zero wake-up:** catch connection timeout or "starting up" errors on first
connect, retry up to 3 times with backoff (1 s, 2 s, 4 s), and have the API return a
distinct `database_waking` status so the UI shows "Waking up your database...".

## 4. Dead or expired credentials

**Connections.** On any failure, classify using the Postgres error (SQLSTATE) rather than text:

| Signal | Status | UI message |
| --- | --- | --- |
| `28P01` / `28000` (auth failed) | `needs_reconnect` | "Password changed or role removed. Paste a new string." |
| `3D000` (database does not exist) | `needs_reconnect` | "That database no longer exists." |
| DNS failure / host not found | `needs_reconnect` | "Project or branch not found." |
| Timeout / `57P03` (starting up) | `waking` (transient) | "Waking your database..." then retry |
| `53300` (too many connections) | `degraded` (transient) | "Database is busy. Try again." |

A status change affects only that connection. Add a lightweight "check connection" button and
a check at app load, not a background job (keeps cost at zero).

**LLM keys.** Map provider errors to states the same way. The exact exception types and HTTP
codes differ by SDK, so put this in one adapter per provider:

| Signal | Status | Behavior |
| --- | --- | --- |
| 401 / 403 | `invalid` | Stop using it, ask the user to replace it |
| 429 (rate limit) | `rate_limited` | Backoff and retry, then fall back |
| 402 / quota or billing error | `out_of_quota` | Pause the key, tell the user which one |
| 5xx / timeout | `degraded` | Retry, then fall back |

**Fallback:** try the user's keys in their saved order. Stop at the first success, and record
which key was used. If all fail, return the per-key reasons.

**Retry without retyping:** store the failed question and its context in `query_history` with
status `failed_key_error`. The UI shows a Retry button that re-submits the saved question
(not new input) after the user fixes the key.

## 5. Multi-provider LLM support

**Problem:** the pipeline is tied to Gemini through Instructor in `app/llm_client.py`.

**Approach:** introduce one interface and keep the pipeline unaware of providers.
```python
class LLMProvider(Protocol):
    def generate_sql(self, messages, model: str, api_key: str) -> SqlResponse: ...
    def count_tokens(self, messages, model: str, api_key: str) -> int: ...
    def validate_key(self, api_key: str) -> KeyStatus: ...
```
- Implement `GeminiProvider` first (move existing code), then Anthropic and OpenAI.
  Instructor supports all three, so structured `SqlResponse` output can stay the same.
- Add an `OpenAICompatibleProvider(base_url)` that also covers OpenRouter, Groq, Mistral, and
  similar services with one implementation.
- Pass `provider`, `model`, `api_key` into `run_pipeline` as a request-scoped object
  (`LLMCredentials`), not through global `Settings`. The existing retry state does not change.
- Treat prompts as the one place where models differ: add a regression run per provider using
  the existing Chinook evaluation harness, so you can compare accuracy per model.
- Rate limiting key changes from `user:{id}` to `(user_id, key_id)` so one exhausted key does
  not block the others.

## 6. Schema introspection and embeddings for a user's database

**Approach:** reuse `ingest/schema_ingest.py`, parameterized by connection.
1. Read `information_schema` and `pg_catalog` for tables, columns, types, primary and
   foreign keys, and (optionally) column comments. Skip system schemas.
2. Render one DDL block per table, as today, then embed with the existing model.
3. Upsert into `schema_embeddings` with `connection_id`.
4. **Only structure is embedded and sent to the LLM, not row data.** Say so in the UI, since
   the user's schema (table and column names) still goes to the LLM provider they chose.
5. **Large schemas:** cap tables per connection (for example 500), let the user choose which
   schemas to include, and embed in batches. Retrieval already uses top-k, so prompt size
   stays bounded.
6. **Drift:** store a hash of the introspected schema. On "Re-sync" or on a failed query
   (missing table/column errors), compare hashes and re-embed only changed tables.
7. Run ingestion as a background task or Cloud Run job for big schemas, with a
   `status: indexing` state shown to the user.

## 7. CSV and PDF export

**CSV:** already exists client-side. Fix two things:
- **Formula injection:** prefix cells starting with `=`, `+`, `-`, `@`, tab, or CR with `'`.
- **Size:** currently exports whatever is in the browser. The server already caps rows
  (`max_result_rows`), so state that cap in the UI.

**PDF:** two options.

| Option | Pros | Cons |
| --- | --- | --- |
| Client-side (jsPDF + autotable) | No server load, no new backend dependency | Large tables are slow, fonts and layout are basic |
| Server-side (ReportLab or WeasyPrint) | Consistent output, can include question and SQL | Heavier image, uses Cloud Run CPU |

Start client-side (cheapest, fits a free project). Include the question, generated SQL,
timestamp, and a table capped to a sensible number of rows with a "truncated" note.

## 8. CSV / Excel upload (write path)

**Safety principles:** the LLM never writes. Writes use a separate credential and are explicit,
previewed, and confirmed.

**Flow:**
1. Check the saved role can write (from the access detection above). If not, explain how to add
   a writer role (SQL shown in the wiki).
2. Upload with a size limit (for example 10 MB) and row limit. Parse with `pandas` or the
   `csv` module for CSV and `openpyxl` for XLSX (`read_only=True`, `data_only=True`; ignore
   macros and external links).
3. Infer column types from a sample (integer, numeric, boolean, date/timestamp, text), fall
   back to text, and let the user override.
4. Sanitize identifiers: lowercase, replace non-alphanumerics, prefix if it starts with a
   digit, de-duplicate, and **always quote with `psycopg.sql.Identifier`**. Never build table
   or column names by string formatting.
5. Preview and confirm. Show the target table name, columns, types, and first rows.
6. Load in a single transaction with `COPY ... FROM STDIN` (psycopg `copy`), which is fast and
   parameter-safe. Roll back on any error and report the row number.
7. Create new tables only at first. Appending to existing tables requires matching types and an
   explicit confirmation, and comes later.
8. After success, re-introspect and re-embed the new table, so questions work immediately.

**Excel "merge" idea:** treat each sheet as its own table at first. Merging or joining sheets
is a later step, and can be done by letting the user ask questions across the uploaded tables.

## 9. Moving existing secrets out of plain env vars

`cloudbuild.yaml` currently reads `DATABASE_URL` and `GEMINI_API_KEY` from the Cloud Run
service's plain env values.
- Create the secrets in Secret Manager, then mount them in Cloud Run with `--set-secrets`.
- Grant the Cloud Build and Cloud Run service accounts `secretmanager.secretAccessor`.
- Use Cloud Build `availableSecrets` / `secretEnv` for the evaluation step instead of
  describing the service and parsing JSON.
- After BYOK ships, the server Gemini key is only needed for the CI evaluation gate.

## 10. Fixing the control-plane write grants

`schemadrill_readonly` can currently write to `users` and `user_datasets`.
- Create a `schemadrill_app` role for control-plane tables (`users`, `connections`,
  `llm_keys`, `query_history`) and use it only in `tenant.py` and the new connection code.
- Leave `schemadrill_readonly` with `SELECT` on the demo schemas and `schema_embeddings` only.
- Move the schema creation out of the request path into a migration step (for example a
  small `scripts/migrate.py` run at deploy time) so serving roles don't need DDL rights.

## 11. Handling the Google and Neon identity split and session lifecycle (shipped in v0.2.0)

Google sign-in (Neon Auth on **your** project) and the user's own Neon account are unrelated
identities. Do not try to match them. The link is just rows in `connections` owned by your
`user_id` (the JWT `sub`). A user with several Google accounts has separate workspaces, which
is acceptable.

**Shipped design decisions for Google authentication:**
1. **Profile capture and conditional backfill from verified JWT claims.**
   - *Decision:* `current_user()` extracts `sub`, `email`, and `name`/`display_name` from the
     verified Neon Auth JWT (top-level or nested `user` claims). `provision_user()` inserts
     `(user_id, email, display_name)` with `ON CONFLICT (user_id) DO NOTHING RETURNING user_id`;
     only when no row is returned does it run an `UPDATE ... SET email = COALESCE(%s::text, email),
     display_name = COALESCE(%s::text, display_name) WHERE user_id = %s::text AND (email IS DISTINCT
     FROM COALESCE(%s::text, email) OR display_name IS DISTINCT FROM COALESCE(%s::text,
     display_name))`, and `GET /api/me` returns the stored `AuthenticatedUser`. Explicit `::text`
     casts are required because `psycopg` v3 binds `$N` parameters via PostgreSQL's server-side
     extended query protocol rather than client-side literal interpolation.
   - *Why:* Preserves exact `is_new_user` detection for one-time default dataset seeding,
     backfills existing `public.users` rows that predated profile capture, and avoids dead-tuple
     writes on unchanged subsequent requests.
   - *Alternatives considered:* Unconditional `INSERT ... ON CONFLICT DO UPDATE RETURNING`
     (rejected: `RETURNING` fires on every call, breaking `is_new_user` detection and writing a
     new heap tuple per request); separate Neon Auth server lookup (rejected: Better Auth's JWT
     plugin already embeds `session.user` claims in the signed token).
2. **Client 401 refresh-before-expire, cross-tab sign-out, and OAuth draft preservation.**
   - *Decision:* The SPA uses `@neondatabase/auth` with `BetterAuthReactAdapter()`. On an
     authenticated `401`, `api.ts` forces one fresh session/JWT fetch (`X-Force-Fetch: true`)
     and retries once before notifying session expiry. Active sessions also revalidate on window
     focus/`visibilitychange` and at `session.expiresAt`. Explicit sign-out broadcasts across
     tabs via `BroadcastChannel` (clearing UI state without raising an expiry warning) and clears
     draft state, whereas re-signing in after expiry stashes any draft `{question, dataset}` in
     `sessionStorage` across the full-page Google OAuth redirect.
   - *Why:* Neon Auth JWTs expire every 15 minutes while the Better Auth cookie session lasts
     longer; retrying once with a forced refresh absorbs clock skew and stale cached tokens
     transparently, while preserving typed SQL questions when a session genuinely expires.
   - *Alternatives considered:* Treating every `401` as immediate logout (rejected: interrupts
     users whose cookie session is still valid); storing drafts in `localStorage` (rejected:
     `sessionStorage` scopes drafts to the tab and clears cleanly on sign-out or tab close).

## 12. Cost and abuse (free project)

- Keep Cloud Run `min=0` and a max-instances cap.
- Per-user rate limits already exist. Add per-user caps on stored connections (for example 5),
  stored keys (for example 5), upload size, and daily query count.
- BYOK removes LLM cost. The remaining costs are Cloud Run, your control-plane DB, and the
  embedding model CPU time. Set a billing alert.

## Recommended implementation order for the first slice

1. `SecretStore` interface plus encryption, with tests (no feature depends on cutting corners here).
2. `LLMProvider` interface, move Gemini behind it, pass credentials per request.
3. `llm_keys` table and API, with validation and error-state mapping.
4. `connections` table, safe connect and access detection, tenant-scoped embeddings.
5. Run the pipeline against a connection, then the dropdown, then exports, then uploads.

Each step is independently testable and shippable, and steps 1 to 3 already remove your Gemini
bill without needing user databases.

## 13. CI static analysis: CodeQL Advanced Setup vs Default Setup (shipped in v0.2.2)

- **Decision:** Replace GitHub's dynamic CodeQL Default Setup with a committed
  `.github/workflows/codeql.yml` workflow pinned to `github/codeql-action@v4.39.1`, scanning
  `actions`, `javascript-typescript`, and `python` with `workflow_dispatch` enabled.
- **Why:** Floating `github/codeql-action@v4` (`v4.39.2`) deleted the runner's pre-cached
  `/opt/hostedtoolcache/CodeQL/2.27.3` bundle and repeatedly failed with `HTTP 504` downloading
  `codeql-bundle-v2.27.4` from GitHub Releases, while `on: dynamic` hid **Re-run jobs** in the
  UI. Pinning `v4.39.1` uses the pre-cached `2.27.3` bundle off local disk with zero download,
  restores manual reruns, and extends coverage to the React/TypeScript frontend and Actions YAML.
- **Alternatives considered:** Keeping Default Setup and pushing empty commits until GitHub's
  `2.27.4` CDN recovered (rejected: flaky, unrepeatable, and Python-only); scanning Python only
  in `codeql.yml` (rejected: misses `frontend/src` and `.github/workflows`).

## 14. Per-request retrieval & retry tuning parameters (shipped in v0.4.0)

- **Decision:** Expose optional bounded tuning parameters on `QueryRequest` (`top_k: int = 3`
  with `1 <= top_k <= 10`, `fk_expansion: bool = False`, and `max_retries: int = 1` with
  `0 <= max_retries <= 3`) and surface them in the authenticated frontend UI (`top_k` dropdown
  `1..5` with tradeoff tooltip, `max_retries` dropdown `0..3`, and `fk_expansion` switch).
  In `POST /query`, these map to a request-scoped `Settings` copy (`model_copy(update=...)`)
  where `effective_attempt_budget = 1 + payload.max_retries` and `disable_self_correction =
  (payload.max_retries == 0)`, and the request timeout scales with
  `provider_timeout_seconds * effective_attempt_budget`.
- **Why:** Users querying simple tables can trade down to `top_k=1..2`, `fk_expansion=false`, and
  `max_retries=0` for lower latency and token usage, while complex multi-table analytical queries
  can opt into higher `top_k`, one-hop FK expansion, and up to 3 self-correction retries.
  `Settings.max_retries` historically represents the total attempt budget in `run_pipeline()`
  and `evaluation/`, so translating `QueryRequest.max_retries` into `1 + payload.max_retries`
  at the HTTP boundary gives API consumers standard retry-count semantics (`0` = no retries,
  matching `execution_trace.retry_count`) without breaking existing pipeline or evaluation
  contracts.
- **Alternatives considered:** Renaming `Settings.max_retries` across `app/` and `evaluation/`
  (rejected: unnecessary churn across tests and benchmark configs); allowing unbounded `top_k` or
  `max_retries` in `QueryRequest` (rejected: risks token/latency abuse).

## 15. Redis hosting & CI/CD strategy: Upstash on Cloud Run, no remote Redis in GitHub Actions (shipped in v0.4.1)

- **Decision:**
  - **Production (Cloud Run, `asia-south1`):** Host Redis on **Upstash Redis** (serverless,
    free tier, TLS `rediss://...`), store the connection string in GCP Secret Manager as
    `schemadrill-redis-url`, and mount it declaratively via `--update-secrets` in
    [cloudbuild.yaml](../cloudbuild.yaml).
  - **GitHub Actions CI:** Do **not** configure `REDIS_URL` or connect to Upstash/external
    Redis in `.github/workflows/ci.yml`. Unit tests in `quality` exercise `RedisRateLimiter`
    with `FakeRedis`, and `docker-postgres` brings up the isolated `redis:7-alpine` container
    from `docker-compose.yml` during `docker compose up -d app`.
  - **Local development:** `docker compose up` runs `redis:7-alpine` with
    `REDIS_URL=redis://redis:6379/0` and zero external secrets; bare local runs without
    `REDIS_URL` fall back cleanly to `AllowAllRateLimiter`.
- **Why:**
  - Upstash scales to zero on a free tier and connects over public TLS (`rediss://`) without
    requiring a Serverless VPC Access connector, whereas GCP Memorystore starts around
    \$36/month and requires VPC egress configuration.
  - Because SchemaDrill's backend is Python/FastAPI (`redis.asyncio.from_url(settings.redis_url)`),
    it uses the standard `rediss://` TCP/TLS URL directly rather than `@upstash/redis` or
    `UPSTASH_REDIS_REST_*` JS/REST clients (the Vite React frontend is a static browser SPA and
    must never hold Redis credentials).
  - Keeping Upstash out of GitHub Actions avoids external network flakiness, unnecessary
    latency, and cross-PR rate-limit key collisions.
- **Alternatives considered:** GCP Memorystore (rejected: fixed monthly cost + VPC connector);
  connecting GitHub Actions to Upstash via a repository secret (rejected: shared mutable state
  across concurrent CI runs); `@upstash/redis` JS client (rejected: backend is Python/FastAPI).

## 16. Phase 1 BYOK: browser-held keys, tiering, and model fallback (shipped in v0.5.0)

- **Decision:** Implement Phase 1 BYOK with browser-held keys (ephemeral JavaScript module memory only for secrets, with non-sensitive model preferences in `sessionStorage` to mitigate CWE-312 cleartext storage alerts), per-request `SecretStr` transport via `X-LLM-Api-Key` / `X-LLM-Provider` headers, an `LLMProvider` abstraction (`app/providers/`), strict free-tier vs BYOK rate-limiting, `POST /api/llm/validate` with HMAC-SHA256 fingerprinted model caching, `invalid_key` (`403 invalid_llm_key`) vs `401` session-expiry separation, and sticky model fallback under a monotonic `QUERY_DEADLINE_SECONDS` and `MAX_TOTAL_PROVIDER_CALLS` cap.
- **Why:** Users can bring their own Gemini quota and pick models immediately without requiring KMS, encrypted DB columns, or key custody on the server, while the server's free-tier key stays protected by fail-closed hourly/daily/global caps and an in-memory fallback limiter when Redis is unset. Ephemeral closure memory prevents client-side secrets from being exposed to web storage extraction (CWE-312 / CWE-315 / CWE-359).
- **Security statement:** Keys are never stored on the server, never logged, never persisted to browser web storage (`sessionStorage` / `localStorage`), and never placed in `RetryState`, `InternalTrace`, or public traces; they transit server memory per request only.
