# SchemaDrill Debugging Runbook & Production Gotchas

Reference of real production, security, evaluation, and CI/CD failures solved in
SchemaDrill, organized by **Symptom → Root Cause → Fix & Invariant**.

---

## 1. Split Database Roles: `DATABASE_URL` vs `INGEST_DATABASE_URL`

- **Symptoms:**
  1. `psycopg.OperationalError: connection to server at "127.0.0.1", port 5432 failed: Connection refused` on `/api/connections` in Cloud Run (`3f168f6`).
  2. `psycopg.errors.UndefinedTable: relation "public.tenant_connections" does not exist` on `/api/connections` even while `/api/connections/inspect` returned `200 OK` (`d34eac4`).
- **Root Cause:**
  - `DATABASE_URL` in Cloud Run (`schemadrill-database-url`) connects as `schemadrill_readonly` so LLM-generated SQL against demo schemas (`chinook`, `ecommerce`, etc.) runs under a `SELECT`-only Postgres role.
  - When `INGEST_DATABASE_URL` was unset on Cloud Run, `Settings.ingest_database_url` defaulted to `localhost:5432`.
  - When `INGEST_DATABASE_URL` was pointed to `schemadrill-database-url` (`schemadrill_readonly`), `_ensure_tenant_schema()` skipped running `scripts/tenant_connections.sql` because `_is_readonly_role()` was `True`, and `scripts/tenant_connections.sql` enforces `REVOKE ALL ON public.tenant_connections, public.tenant_schema_embeddings FROM schemadrill_readonly`.
- **Fix & Invariant:**
  - Always resolve control-plane connections via `settings.control_plane_database_url` (`app/config.py`), which normalizes `-pooler.` hostnames.
  - In Cloud Run / `cloudbuild.yaml`, bind `DATABASE_URL=schemadrill-database-url:latest` (`schemadrill_readonly`) and `INGEST_DATABASE_URL=schemadrill-ingest-database-url:latest` (`neondb_owner`).

---

## 2. Cloud Run Outbound IPv6 & SSRF `hostaddr` Pinning

- **Symptom:** `POST /api/connections/inspect` failed on Cloud Run with `503 Database unreachable` (`ENETUNREACH`) while succeeding locally (`ff0bace`).
- **Root Cause:**
  - To prevent SSRF and TOCTOU DNS rebinding, `app/connections/validate.py` resolves DNS once (`socket.getaddrinfo`), validates every IP against private/loopback/link-local/CGNAT ranges, and pins the resolved IP in `libpq`'s `hostaddr` parameter while passing the hostname in `host` (for TLS SNI).
  - Neon endpoints publish both `A` (IPv4) and `AAAA` (IPv6) records; Linux containers returned `AF_INET6` first, which failed on Cloud Run's IPv4-only default egress.
- **Fix & Invariant:**
  - Validate **all** resolved IPv4 and IPv6 records against SSRF blocklists first (so a private IPv6 record cannot hide behind a public IPv4 record), then prefer `AF_INET` (IPv4) when selecting the pinned `hostaddr`.

---

## 3. `psycopg-binary` Bundled OpenSSL & `sslmode=verify-full`

- **Symptom:** User database connections inside `python:3.12-slim` failed TLS verification with `root certificate file "system" does not exist` (`ce0a28a`).
- **Root Cause:**
  - `psycopg-binary` bundles its own OpenSSL compiled with `OPENSSLDIR=/usr/lib/psycopg-openssl`, so `libpq`'s `sslrootcert="system"` misses Debian's CA store at `/etc/ssl/certs/ca-certificates.crt`.
- **Fix & Invariant:**
  - `_resolve_system_ca_bundle()` in `app/connections/validate.py` resolves the first existing system CA bundle (`ssl.get_default_verify_paths().cafile`, `/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/cert.pem`) and passes it to `sslrootcert` under `sslmode="verify-full"`.

---

## 4. `psycopg` v3 Extended Query Protocol (`IndeterminateDatatype`)

- **Symptom:** `500 Internal Server Error` on `/api/me` and `/api/datasets` for returning users: `psycopg.errors.IndeterminateDatatype: could not determine data type of parameter $4` (`faf0bfc`).
- **Root Cause:**
  - Unlike `psycopg2` (client-side string interpolation), `psycopg` v3 uses PostgreSQL's server-side extended query protocol (`$1..$N`).
  - Polymorphic predicates like `%s IS NOT NULL` leave the parameter type as `UNKNOWNOID` during PostgreSQL's `Parse` phase. Mocked unit tests do not execute PostgreSQL's parser and miss this.
- **Fix & Invariant:**
  - Never write untyped `%s IS NOT NULL` in `psycopg` v3 queries. Cast nullable parameters explicitly (`%s::text`) and fold change detection into `col IS DISTINCT FROM COALESCE(%s::text, col)`.

---

## 5. Browser Secret Hygiene (`CWE-312`)

- **Symptom:** CodeQL alert `js/clear-text-storage-of-sensitive-data` on `frontend/src/llmKey.ts` (`fb28a0e`).
- **Root Cause:** Serializing state containing `apiKey` into `window.sessionStorage.setItem()` exposes raw credentials to any same-origin script / XSS sink.
- **Fix & Invariant:**
  - Hold raw secrets (`apiKey` in `llmKey.ts`, `dbUrl` in `dbCreds.js`) exclusively in module-scoped JavaScript memory closures. Persist only non-sensitive metadata/preferences to `sessionStorage`.

---

## 6. Text-to-SQL Evaluation & FK Expansion Trade-offs

- **Key Findings & Fixes (`23349c7`, `a23e914`, `b2a71bf`, `3f168f6`):**
  - **Execution success != accuracy:** Always track `sql_execution_success_rate` and `result_set_accuracy` separately.
  - **Multiset row comparison:** Compare result sets using multisets (`Counter`) with numeric normalization and positional column values so duplicate rows are preserved and unaliased aggregates (`COUNT(*)`) do not cause false negatives.
  - **1-hop FK expansion:** On small schemas where `top_k=3` covers the target tables, eager FK expansion adds prompt noise (-8.7 pp on Chinook). On wider multi-table schemas (`ecommerce`, 6 tables), 1-hop FK expansion recovers join targets outside `top_k=3` (`83.3% -> 100.0%`). Keep `fk_expansion` off by default and configurable per request.

---

## 7. CI/CD & Container Build Gotchas

- **Cloud Build context size (`ffe92c9`):** Keep `*.whl` in `.gcloudignore` so local offline wheel caches (`wheels/`) are never uploaded to Cloud Build; `Dockerfile` installs from `wheels/` when present and falls back to the PyTorch CPU index in Cloud Build.
- **CodeQL Action version pinning (`88dacf0`):** Pin `.github/workflows/codeql.yml` (`v4.39.1`) instead of relying on dynamic Default Setup when upstream bundle releases (`2.27.4`) return `HTTP 504`.
