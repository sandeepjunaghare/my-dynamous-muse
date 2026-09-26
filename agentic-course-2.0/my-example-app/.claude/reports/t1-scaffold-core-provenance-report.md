# Implementation Report — T1: Project scaffold, core infrastructure, provenance primitive

**Plan**: `.claude/plans/t1-scaffold-core-provenance.md`
**Branch**: `docs/local-prospect-engine-tickets`
**Status**: COMPLETE

## Summary

Stood up the Local Prospect Engine service skeleton: a `uv` project whose full validation suite runs green
with zero suppressions, the `core/` infrastructure that predates any feature (config, logging, database,
cost, exceptions, middleware, dependencies), and `app/shared/provenance.py` — the `ProvenancedValue[T]` type
that makes "a field without provenance cannot be written to HubSpot" a property of the type system rather
than a convention someone has to remember. The app boots, `GET /health` answers without touching the
database, and the Alembic baseline applies and reverses cleanly over the async engine. No feature slice,
no HubSpot client, no Agent SDK dependency.

## Tasks completed

| Task | File | |
|---|---|---|
| uv project, four tools configured | `pyproject.toml`, `.python-version` | CREATE |
| Provenance primitive | `app/shared/provenance.py` | CREATE |
| Configuration | `app/core/config.py` | CREATE |
| Structured logging | `app/core/logging.py` | CREATE |
| Exception hierarchy | `app/core/exceptions.py` | CREATE |
| Async database access | `app/core/database.py` | CREATE |
| Per-run cost accounting | `app/core/cost.py` | CREATE |
| Correlation-id middleware | `app/core/middleware.py` | CREATE |
| Global FastAPI dependencies | `app/core/dependencies.py` | CREATE |
| App assembly, handlers, `/health` | `app/main.py` | CREATE |
| Async migration environment | `alembic/env.py`, `alembic.ini` | CREATE |
| Baseline migration (no domain tables) | `alembic/versions/0001_baseline.py` | CREATE |
| Compose (app alone) + image | `docker-compose.yml`, `Dockerfile` | CREATE |
| Every variable documented, no secrets | `.env.example` | CREATE |
| Package docs | `README.md`, `.gitignore` | CREATE |
| Session log | `_tasks/todo.md` | UPDATE |

## Tests added

68 tests, all passing.

| File | Covers |
|---|---|
| `tests/shared/test_provenance.py` (23) | Each of the four fields required; frozen; blank source rejected; naive `retrieved_at` rejected and non-UTC normalised; generic `T` actually validated, including a nested model; `manual_hubspot_entry` exists; unknown method rejected; `is_promotable` and its field-writes-not-tasks docstring |
| `tests/core/test_config.py` (7) | Missing required variable raises; typo'd line in `.env` rejected; invalid `environment` rejected; defaults; no setting carries a local path; `get_settings` cached |
| `tests/core/test_cost.py` (13) | Calls accumulate per kind; negative count rejected; money stays exact `Decimal`; cap fires exactly at the boundary (500th allowed, 501st refused); cap of `0` refuses immediately; cap is per-kind; the error carries kind/cap/recorded for T6's degrade path |
| `tests/core/test_logging.py` (8) | Correlation id generated and reused; entry is JSON with level, timestamp, `request_id`; `exc_info=True` renders a traceback string; every log event in `app/` matches `domain.component.action_state`; `get_logger` is statically `FilteringBoundLogger`, asserted with `assert_type` |
| `tests/core/test_health.py` (8) | `/health` 200 with the expected shape; answers even when engine creation is made to explode (proving it never asks for a connection); `X-Request-ID` echoed and generated; each request gets its own; 404; structured error JSON; an unexpected error leaks nothing |
| `tests/test_structure.py` (9) | Redis/Langfuse/Celery absent from dependencies; no unused Agent SDK; compose runs the app alone with no `postgres`/`db`/`redis`; no frontend manifest; no `# type: ignore`; no `Any` annotation (AST-checked); no auth fields on `Settings`; no RLS in migrations; only `core/` and `shared/` under `app/`; provenance imports nothing from `core/` |

## Validation results

| Level | Command | Result |
|---|---|---|
| Style | `uv run ruff check .` | **pass** — all checks passed |
| Style | `uv run ruff format --check .` | **pass** — 25 files formatted |
| Types | `uv run mypy .` | **pass** — 24 source files, 0 issues |
| Types | `uv run pyright` | **pass** — 0 errors, 0 warnings |
| Tests | `uv run pytest` | **pass** — 68 passed |
| Manual | `uvicorn` + `curl /health` | **pass** — `{"status":"ok","version":"0.1.0","environment":"dev"}` |
| Manual | `X-Request-ID` round-trip | **pass** — `abc123` echoed; a UUID4 generated when absent |
| Manual | `alembic upgrade head` → `downgrade base` → `upgrade head` | **pass** — round-trips; only `alembic_version` created |
| Manual | `docker compose config -q` | **pass** |

Zero `# type: ignore`, zero `Any` annotations — asserted by test, not just by review.

## Deviations from the plan

All intentional; each is a decision the reviewer should not flag.

1. **`extra="forbid"` guards `.env`, not the whole process environment.** AC3 asks config to "fail loudly on
   an unknown one". Verified empirically: pydantic-settings only reads variables matching declared fields, so
   an unknown *process* env var is never seen — `extra="forbid"` cannot police PATH and HOME. It **does**
   reject an unknown key in `.env`, which is the surface a person edits and where a typo actually happens.
   The test and `.env.example` now state that scope precisely rather than implying more. AC3 is met as
   written for the case it can be met for.

2. **`Any` and provenance-independence are checked via AST, not grep.** The plan specified matching `: Any`
   and `-> Any`. That fired on this codebase's own docstrings, which discuss the rule (`app/core/logging.py`
   explains why it does *not* return `Any`). Parsing annotations from the AST is exact and has no false
   positives. Same for "provenance imports nothing from `core/`" — the textual check matched the module
   docstring saying exactly that.

3. **`get_logger`'s return type is asserted with `typing.assert_type`, not `isinstance`.** `FilteringBoundLogger`
   is a protocol that structlog's lazy proxy does not satisfy at runtime, so `isinstance` is false for a
   perfectly correct logger. The claim under test is static, and `assert_type` is what both type checkers verify.

4. **The database engine is built lazily, not at module import.** The reference example creates the engine at
   import time. Here that would make importing `app.core.database` require a reachable `DATABASE_URL`, which
   would defeat the `/health`-must-not-touch-the-database test. `get_engine()` / `get_sessionmaker()` build on
   first use; `dispose_engine()` is idempotent and resets them.

5. **`Dockerfile` added.** Not in the plan's file list, but `docker-compose.yml` specifies `build: .` and
   AC9 requires compose to run the app — there is nothing to run without it.

6. **`.gitignore` and `README.md` added.** `.gitignore` keeps `.venv/`, caches and `.env` out of the repo
   (`.env` is a secret per the architecture doc); `README.md` is required by `pyproject.toml`'s `readme` field.

7. **Compose `env_file` is marked `required: false`.** `docker compose config -q` is a plan validation command
   and fails in a fresh clone when `.env` does not exist. A missing `DATABASE_URL` still fails loudly — at
   application startup, where the error names the variable, which is the guarantee AC3 actually wants.

8. **Lint and type-check scope is the application.** `ruff`/`mypy` run from the repo root would otherwise lint
   `.claude/skills/**` reference scripts, `.agents/**` article markdown and the pre-existing
   `tooling/mcp/codebase_search.py` — none of which are T1's code, and reformatting them would bury the diff.
   Scope is `app/` + `tests/` + `alembic/`; the exclusions carry a comment saying why.

9. **`pyright` needed `venvPath`/`venv` in `pyproject.toml`.** Without them it resolves against the system
   interpreter and reports every third-party import as unresolved, cascading into ~50 spurious errors. Found
   at Phase 1 because the toolchain was proven on an empty project first.

10. **Did not add mypy's `disallow_any_explicit`.** Tried it; it rejects third-party TypedDicts we do not
    control (`SettingsConfigDict`). The plan's own mechanism — the structure test — enforces the rule on our
    code without collateral damage.

## Issues encountered

- **Assumption #1 of the plan is still unmet: there is no Supabase project.** The Alembic round-trip was
  therefore validated two ways that do not need one — offline SQL rendering (`upgrade head --sql`, which
  exercises `env.py` and the baseline), and a live `upgrade → downgrade → upgrade` against a **throwaway
  local Postgres container**, started and removed for the check and added to nothing in the repo. The
  migration tooling is proven over the async engine; what remains unproven is only that a specific Supabase
  URL is reachable. **The dev and prod Supabase projects need creating before T2**, which is the first ticket
  with a table.

- **Sandbox friction, not code problems.** `uv` crashes under the OS sandbox (macOS SystemConfiguration
  access), and binding a TCP port is refused — so dependency installation and the live `curl` checks were run
  outside it. Both were genuine sandbox denials, visible in the errors; no application behaviour changed.

- **Branch.** Work is on `docs/local-prospect-engine-tickets`, per the skill's rule to use an existing
  feature branch rather than cut a new one, so the plan commits ride into the same PR. The consequence is
  that the PR will contain both the docs realignment and the T1 scaffold. Worth a moment's thought before
  `piv-create-pr` if a T1-only PR is wanted — that would mean re-basing onto `main` without the plan commits.

## Ready for the next step

All plan tasks complete, every validation command run and passing. Next: `piv-commit`, then
`piv-create-pr`, then `piv-review-pr`.
