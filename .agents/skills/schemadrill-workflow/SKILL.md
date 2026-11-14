---
name: schemadrill-workflow
description: >-
  Engineering workflow, architectural decision logging, failure/bug root-cause
  documentation, release SemVer synchronization, pre-commit checks, and
  Conventional Commit standards for SchemaDrill. Activate before making
  non-trivial design changes, debugging or fixing bugs, bumping versions,
  updating documentation, or preparing git commits.
---

# SchemaDrill Engineering & Release Workflow

Follow this workflow for non-trivial changes and bug fixes in SchemaDrill so
architecture docs, failure postmortems, version metadata, tests, and commit
history stay synchronized across devices.

## 1. Architecture & Design Decisions

- **Clarify ambiguity first:** Confirm non-trivial trade-offs (schema changes,
  API boundaries, auth/tenancy isolation, LLM pipeline stages, dependencies)
  before implementing.
- **Document every decision:** Record *what* was decided, *why*, alternatives
  considered, and trade-offs in [docs/solution-design.md](../../../docs/solution-design.md).
  Add a concise row to the ADR table in [docs/architecture.md](../../../docs/architecture.md)
  for system-level architectural or CI/infrastructure decisions.
- **Respect core boundaries:**
  - Keep typed Pydantic contracts at stage/API boundaries in `app/models.py`.
  - Keep deterministic security checks in `app/sql_guard.py` (never delegate
    SQL safety to LLM output).
  - Use `settings.control_plane_database_url` for control-plane tables (`users`,
    `user_datasets`, `tenant_connections`, `tenant_schema_embeddings`) while
    keeping `app/db.py` on the read-only `DATABASE_URL` pool.
  - Use explicit PostgreSQL type casts (e.g., `%s::text`) when parameters could
    be `NULL` or appear inside polymorphic predicates, because `psycopg` v3
    uses server-side extended query binding.

## 2. Failure & Bug Root-Cause Documentation

Whenever you diagnose and fix a production, runtime, security, evaluation, or
CI/CD bug:

1. **Write a regression test** that reproduces the failure condition (including
   environment mismatches that unit mocks previously hid).
2. **Document how it failed and how it was fixed:**
   - **Symptom & exact error:** HTTP status, exception class, and failing module
     or SQL statement.
   - **Root cause & test gap:** Why it failed in production/CI and why existing
     mocks or local Docker Compose did not catch it.
   - **Fix & invariant:** The code/config change that permanently prevents recurrence.
3. **Update all tracking logs in the same commit:**
   - Add a row to the `## Bugs solved` table in [docs/changelog.md](../../../docs/changelog.md).
   - If the failure reveals a platform or runtime gotcha (`psycopg` v3, Cloud
     Run networking, Neon pooling/roles, `libpq` TLS, SSRF, CodeQL, evaluation),
     record the pattern in [references/debugging-runbook.md](./references/debugging-runbook.md).
   - If the local `notes/` directory exists, append the full breakdown to
     `notes/failures-and-war-stories.md` and `notes/decisions/` (keep `notes/`
     untracked).

## 3. Documentation Sync

Update affected docs in the same commit as the code change:

- [docs/roadmap.md](../../../docs/roadmap.md) — mark shipped items, adjust scope,
  and add newly discovered phase work.
- [docs/solution-design.md](../../../docs/solution-design.md) — keep design
  details and SQL/client flows aligned with the implementation.
- [docs/modules.md](../../../docs/modules.md) — update runtime, pipeline, or
  CI/workflow inventory when modules or contracts change.

## 4. Pre-Commit Verification

Run the checks relevant to your change before committing:

```bash
# Backend lint, formatting, and unit tests
.venv/bin/ruff check .
.venv/bin/black --check .
.venv/bin/pytest -q

# Frontend production bundle (when frontend/ changes)
npm --prefix frontend run build
```

When modifying TypeScript files under `frontend/src/`, also run strict type
checking:

```bash
./frontend/node_modules/.bin/tsc --noEmit --strict --skipLibCheck \
  --target ES2022 --module ESNext --moduleResolution bundler \
  frontend/node_modules/vite/client.d.ts \
  frontend/src/auth.ts frontend/src/services/api.ts frontend/src/types/api.ts
```

## 5. Synchronized Release Versioning

Bump the Semantic Version (`MAJOR.MINOR.PATCH`: patch for fixes/chore/CI, minor
for backward-compatible features, major for breaking changes) before every
commit across all three version sources:

1. `app/main.py` — `FastAPI(title="SchemaDrill", version="X.Y.Z", ...)`
2. `frontend/package.json` — `"version": "X.Y.Z"`
3. `frontend/package-lock.json` — root `"version"` and `packages[""].version`

Add a matching timeline row (and a **Bugs solved** entry when fixing a bug) in
[docs/changelog.md](../../../docs/changelog.md).

## 6. Commit Discipline

- Use Conventional Commits:
  `<type>(<optional scope>): <imperative summary>`
  where `<type>` is one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`.
- Keep the subject around 72 characters and specific to what changed and where;
  explain *why* in the commit body when non-trivial.
- Stage explicit tracked paths rather than `git add -A` so local/private files
  (`.env`, `wheels/`, `AGENTS.md`, `GEMINI.md`, `notes/`) are never staged.
