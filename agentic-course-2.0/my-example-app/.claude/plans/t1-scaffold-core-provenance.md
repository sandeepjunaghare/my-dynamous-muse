# Feature: T1 — Project scaffold, core infrastructure, provenance primitive

The following plan should be complete, but its important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils types and models. Import from the right files etc.

> **This is a greenfield ticket.** There is no `app/` yet — no `pyproject.toml`, no code of any kind. Every
> "pattern to follow" below comes from the project's *reference* docs rather than from existing source, and
> **three of those references contain code that must NOT be copied verbatim** — they are sync where we are
> async, and untyped where we forbid `Any`. Those divergences are called out explicitly in *Patterns to
> Follow*; read that section before writing a line.

## Feature Description

Stand up the Local Prospect Engine service skeleton and the one primitive every later slice depends on. T1
delivers three things: a `uv` project whose full validation suite runs green with zero type suppressions, a
FastAPI application with the `core/` infrastructure that predates any feature (config, logging, database,
cost accounting, exceptions, dependencies), and `shared/provenance.py` — the `ProvenancedValue[T]` type that
makes "a field without provenance cannot be written to HubSpot" a property of the type system rather than a
convention someone remembers.

Nothing in T1 sources, qualifies, promotes or schedules anything. Its success condition is that the service
boots, validates clean, and the provenance type refuses to be constructed incorrectly.

## User Story

As a **developer building the Local Prospect Engine**
I want **a validated project skeleton with core infrastructure and a provenance primitive that cannot be bypassed**
So that **every later slice inherits type safety, structured logging, cost accounting and the write-gate for free, instead of each one re-deciding them**

## Problem Statement

Nine tickets depend on T1 and none can start without it. Worse, three cross-cutting guarantees are cheap to
build now and extremely expensive to retrofit:

1. **Provenance.** E18 measured the cost of its absence — of 34 fire-vertical contacts, 14 carry a company
   name in the first-name field and 0 of 23 companies have a headcount. A field nobody can cite is exactly a
   field that should never have been written. If provenance is a convention rather than a type, some slice
   will forget.
2. **Cost accounting.** The project has deliberately set no budget ceiling and chosen to measure instead
   (D7). That only works if the first run that spends money already counts what it spent — which means cost
   accounting belongs in the scaffold, not in the enrichment ticket that happens to spend first.
3. **Zero-suppression strict typing.** MyPy *and* Pyright strict with no `# type: ignore` and no `Any`
   escape hatch is a property of the whole codebase or of none of it. Several libraries in the stack need
   deliberate handling to satisfy it (see *Gotchas*).

## Solution Statement

Build `core/` and `shared/provenance.py` following the vertical-slice reference's *structure*, corrected for
this project's actual decisions: async SQLAlchemy rather than the reference's sync example, explicitly typed
structlog rather than the reference's `-> Any`, hosted Supabase rather than a Compose Postgres, and no auth
fields in config because the MVP has no auth.

`ProvenancedValue[T]` is a Pydantic v2 generic model whose three provenance fields are required — omitting
any one raises `ValidationError` at construction, so an unprovenanced value cannot exist as a
`ProvenancedValue` at all. Alongside it, `is_promotable()` states the gate predicate, with a docstring that
records the one distinction the whole system turns on: **the gate governs prospect field writes, not task
creation.**

## Out of Scope / Non-Goals

- **Not included:** any feature slice — `manifests/`, `sourcing/`, `qualification/`, `routing/`,
  `promotion/`, `cadence/`, `tools/` (defer to T2–T13)
- **Not included:** the HubSpot client, FMCSA adapter, Google Places client, or any outbound integration
  (defer to T3, T5, T6)
- **Not included:** the Claude Agent SDK dependency. Nothing in T1 calls a model; add it at first use (T6/T12)
  rather than carrying an unused dependency
- **Not included:** the `lpe` CLI. T2 introduces it — but structure `pyproject.toml` so adding a
  `[project.scripts]` entry is a two-line change
- **Not included:** any `vertical_manifest`, `sourcing_run`, `candidate`, `cadence_state` table. T1's
  migration is a **baseline only** — Alembic wired and proven, with no domain tables
- **Not changing:** the decisions in `CLAUDE.md` and the architecture doc. If something here seems to
  contradict them, stop and flag it rather than diverging
- **Explicitly absent and staying absent:** Redis, Langfuse, Celery/queues, the Vite/React frontend, auth,
  Supabase RLS

## Feature Metadata

**Feature Type**: New Capability (greenfield scaffold)
**Estimated Complexity**: Medium — low conceptual difficulty, high precision requirement. The type-strictness
bar and the async/sync divergence from the reference examples are where this goes wrong.
**Primary Systems Affected**: entire `app/` tree (created here), Alembic, Docker Compose, CI-less local validation
**Dependencies**: fastapi · uvicorn · pydantic v2 · pydantic-settings · sqlalchemy[asyncio] · asyncpg ·
alembic · structlog · pytest · pytest-asyncio · ruff · mypy · pyright (all via `uv`)

## Related Work

**Implements**: T1 in [`docs/tickets/local-prospect-engine.md`](../../docs/tickets/local-prospect-engine.md)
**Epic**: [`docs/local-prospect-engine.architecture.md`](../../docs/local-prospect-engine.architecture.md) (the how) + [`docs/local-prospect-engine.prd.md`](../../docs/local-prospect-engine.prd.md) (intent)

**Back-references** (decisions inherited, do not reopen):

- `docs/local-prospect-engine.architecture.md` → *Stack & libraries*, *Runtime*, *Data model*, *Cost* —
  hosted Supabase, no Postgres container, no Redis, 12-factor config, external scheduling
- `_tasks/todo.md` → D1–D11, the decision record from the session that produced this slicing
- `.claude/references/vertical-slice-architecture.md` → Rules 1–3 (what belongs in `core/` vs `shared/` vs a slice)

**Forward-references** (plans that extend this — append as follow-ups get created):

- (none yet — T2 and T3 both depend on this and can be planned once it is implemented)

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

There is no application source yet. These are the specs and references that define what to build:

- `CLAUDE.md` (whole file, ~90 lines) — Why: the ground rules. Provenance-as-write-gate and its
  field-writes-not-task-creation scope, zero suppressions, structlog event naming, the Runtime section
  (hosted Supabase, no Postgres in Compose, 12-factor), and the "deliberately left out" list
- `docs/tickets/local-prospect-engine.md` (T1 section, plus *Epic summary*) — Why: the acceptance criteria
  this plan implements, verbatim
- `docs/local-prospect-engine.architecture.md` (*Stack & libraries*, *Runtime*, *Data model — the shape*,
  *Cost: a circuit breaker, not a ceiling*) — Why: why each dependency is present or absent
- `.claude/references/vertical-slice-architecture.md` (lines 46–140) — Why: Rule 1 (`core/`) and the
  `core/config.py` + `core/database.py` examples. **The database example is sync — see Gotchas**
- `.claude/references/vertical-slice-architecture.md` (lines 266–362) — Why: the `core/logging.py` pattern
  with correlation ids and the `{domain}.{action}.{status}` event convention. **Its `-> Any` return type
  violates our ground rules — see Gotchas**
- `.claude/references/vertical-slice-architecture.md` (lines 141–232) — Why: Rule 2, the three-feature rule.
  Justifies `shared/provenance.py` existing on day one (three known consumers) rather than being speculative
- `.claude/references/conventions.md` — Why: commit format for the final commit; no AI attribution
- `.claude/references/backend-api-best-practices.md` (lines 1–35) — Why: route/validation/error-handling
  conventions the `/health` endpoint and the exception handler should already follow

### New Files to Create

```
pyproject.toml                    # uv project, deps, ruff/mypy/pyright/pytest config
.python-version                   # 3.12
.env.example                      # every variable, no secrets — committed
docker-compose.yml                # the app alone; NO postgres service
alembic.ini
alembic/env.py                    # async migration environment
alembic/versions/0001_baseline.py # baseline only — no domain tables
app/__init__.py
app/main.py                       # FastAPI + lifespan + exception handlers + GET /health
app/core/__init__.py
app/core/config.py                # pydantic-settings; strict 12-factor
app/core/logging.py               # structlog, correlation id, typed
app/core/database.py              # async engine/session over hosted Supabase
app/core/cost.py                  # per-run billable-call accounting + cap
app/core/exceptions.py            # base exception hierarchy
app/core/dependencies.py          # FastAPI dependencies (db session, settings, run context)
app/core/middleware.py            # correlation-id middleware
app/shared/__init__.py
app/shared/provenance.py          # ProvenancedValue[T] + is_promotable()
tests/__init__.py
tests/conftest.py
tests/core/test_config.py
tests/core/test_logging.py
tests/core/test_cost.py
tests/core/test_health.py
tests/shared/test_provenance.py
tests/test_structure.py           # asserts the absent-by-design things stay absent
```

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [SQLAlchemy 2.0 asyncio ORM](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
  - Specific section: `async_sessionmaker` and `AsyncSession` lifecycle
  - Why: the project reference's database example is SQLAlchemy 1.x-style **sync** code and must not be copied
- [SQLAlchemy 2.0 DeclarativeBase](https://docs.sqlalchemy.org/en/20/orm/declarative_styles.html#using-a-declarative-base-class)
  - Specific section: `class Base(DeclarativeBase)` over the legacy `declarative_base()` factory
  - Why: the legacy factory returns an untyped base and will not survive Pyright strict with zero suppressions
- [asyncpg + PgBouncer prepared statements](https://github.com/sqlalchemy/sqlalchemy/issues/6467)
  - Why: the single most likely way this ticket loses a day — see Gotchas
- [Supabase: asyncpg PreparedStatementError](https://github.com/supabase/supabase/issues/35684)
  - Why: confirms the failure mode is specific to the pooled port, and that the direct port avoids it
- [Alembic async migration environment](https://alembic.sqlalchemy.org/en/latest/cookbook.html#programmatic-api-use-connection-sharing-with-asyncio)
  - Specific section: `run_async_migrations` / connection sharing
  - Why: a default `alembic init` writes a sync `env.py` that will not work against an async engine
- [pydantic-settings v2](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
  - Specific section: `SettingsConfigDict`, required vs defaulted fields
  - Why: config must fail loudly at startup when a required variable is missing, not default silently
- [structlog typing](https://www.structlog.org/en/stable/typing.html)
  - Why: gives the concrete types (`FilteringBoundLogger`, `EventDict`, `Processor`) that replace the
    reference's `Any`
- [Pydantic v2 generic models](https://docs.pydantic.dev/latest/concepts/models/#generic-models)
  - Why: `ProvenancedValue[T]` is a generic `BaseModel`; validation of the parameterized field is the point

### Patterns to Follow

**Naming:** snake_case modules and functions, PascalCase classes, per the user's global rules. Slice-internal
names stay unprefixed (`config.get_settings`, not `core_get_settings`).

**Logging — event names are `domain.component.action_state`:**

```python
logger.info("core.database.connect_started", url_host=host)
logger.info("core.database.connect_succeeded")
logger.error("core.database.connect_failed", error=str(e), exc_info=True)
```

Note this project's convention is **four segments of meaning in three dots** — `domain.component.action_state`
(CLAUDE.md), which is narrower than the reference doc's `{domain}.{action}.{status}`. Follow CLAUDE.md.

**Config — mirror `vertical-slice-architecture.md:67-107`, with these changes:**

```python
@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore  <-- FORBIDDEN. See Gotchas for the correct approach.
```

- **Drop** `secret_key` and `algorithm` from the reference example — there is no auth in this MVP
- **Add** `database_url`, `log_level`, `hubspot_private_app_token` (optional at T1), `max_places_calls_per_run`
  (default 500), `environment` (`dev` | `prod`)
- Required fields have **no default**, so a missing variable fails at startup

**Provenance — the shape:**

```python
class ProvenancedValue[T](BaseModel):
    value: T
    source_url: str
    retrieved_at: datetime
    retrieval_method: RetrievalMethod  # StrEnum, NOT a bare str
```

`RetrievalMethod` is a `StrEnum` because T13 depends on one specific member existing —
`manual_hubspot_entry` — and a bare string would let a typo through silently.

**Error handling:** a base `LocalProspectEngineError` in `core/exceptions.py`, with feature slices deriving
their own. Centralized handlers in `main.py` per `backend-api-best-practices.md:20-26` — structured JSON,
never a stack trace to the client.

---

## IMPLEMENTATION PLAN

### Phase 1: Project skeleton and validation gate

Build the toolchain *first* and prove it green on an empty project. Every later phase then validates
incrementally rather than discovering a hundred type errors at the end.

**Tasks:**
- `uv` project, Python 3.12, all dependencies
- ruff, mypy strict, pyright strict, pytest configured in `pyproject.toml`
- One trivial passing test, to prove the suite runs

### Phase 2: Core infrastructure

**Depends on:** Phase 1 (the validation gate must be green before code lands against it)

**Tasks:**
- `core/config.py`, `core/logging.py`, `core/exceptions.py`
- `core/database.py` (async), `core/cost.py`
- `core/middleware.py`, `core/dependencies.py`

### Phase 3: The provenance primitive

**Depends on:** Phase 1 only. **Independent of:** Phase 2 — `shared/provenance.py` imports nothing from
`core/`. It can be built in parallel with Phase 2, and is the highest-value piece to get right.

**Tasks:**
- `ProvenancedValue[T]`, `RetrievalMethod`, `is_promotable()`

### Phase 4: Application assembly and migrations

**Depends on:** Phases 2 and 3

**Tasks:**
- `app/main.py` with lifespan, middleware, exception handlers, `GET /health`
- Alembic async `env.py` + baseline migration
- `docker-compose.yml` (app only), `.env.example`

### Phase 5: Testing and the absence guard

**Depends on:** Phase 4

**Tasks:**
- Unit tests for config, logging, cost, provenance
- `GET /health` integration test
- `tests/test_structure.py` — asserts the deliberately-absent things are still absent

---

## STEP-BY-STEP TASKS

IMPORTANT: Execute every task in order, top to bottom. Each task is atomic and independently testable.

### CREATE `pyproject.toml` + `.python-version`

- **IMPLEMENT**: `uv init`-style project named `local-prospect-engine`, requires-python `>=3.12,<3.13`.
  Dependencies: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings>=2`,
  `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `structlog`. Dev: `pytest`, `pytest-asyncio`, `httpx`,
  `mypy`, `pyright`, `ruff`. Configure all four tools in this one file.
- **PATTERN**: CLAUDE.md → *Commands* for the exact invocations the tooling must support
- **IMPORTS**: n/a
- **GOTCHA**: set `[tool.mypy] strict = true` **and** `[tool.pyright] typeCheckingMode = "strict"` — they
  disagree often enough that satisfying one does not satisfy the other. Set
  `[tool.pytest.ini_options] asyncio_mode = "auto"` or every async test needs a decorator. Do **not** add
  `claude-agent-sdk` — nothing in T1 calls a model.
- **VALIDATE**: `uv sync && uv run ruff check . && uv run mypy . && uv run pyright && uv run pytest`
- **SATISFIES**: AC1

### CREATE `app/core/config.py`

- **IMPLEMENT**: `Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="forbid")`. Fields: `app_name: str = "local-prospect-engine"`, `environment: Literal["dev", "prod"] = "dev"`, `debug: bool = False`, `database_url: str` (required), `log_level: str = "INFO"`, `max_places_calls_per_run: int = 500`, `hubspot_private_app_token: str | None = None`. Cached `get_settings() -> Settings`.
- **PATTERN**: MIRROR `.claude/references/vertical-slice-architecture.md:67-107` for structure
- **IMPORTS**: `from functools import lru_cache`, `from typing import Literal`, `from pydantic_settings import BaseSettings, SettingsConfigDict`
- **GOTCHA**: `Settings()` with required fields makes both type checkers complain that arguments are
  missing — they cannot see that pydantic-settings fills them from the environment. **Do not silence it with
  `# type: ignore`** (forbidden). Use `Settings.model_validate({})`, which is correctly typed and performs
  identical env loading. `extra="forbid"` turns a typo'd env var into a startup failure instead of a silent
  default.
- **VALIDATE**: `uv run pytest tests/core/test_config.py -q && uv run pyright app/core/config.py`
- **SATISFIES**: AC3

### CREATE `app/core/logging.py`

- **IMPLEMENT**: `request_id_var: ContextVar[str]`, `get_request_id()`, `set_request_id()`, an
  `add_request_id` processor, `setup_logging()` configuring JSON output with
  `structlog.processors.format_exc_info`, and `get_logger(name: str) -> FilteringBoundLogger`.
- **PATTERN**: MIRROR `.claude/references/vertical-slice-architecture.md:268-340` for structure and processor order
- **IMPORTS**: `import structlog`, `from structlog.types import EventDict, FilteringBoundLogger, WrappedLogger`, `from contextvars import ContextVar`
- **GOTCHA**: the reference returns `-> Any` and types its processor parameters as bare positional args.
  **Both violate the zero-`Any` ground rule.** Type the processor as
  `(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict` and `get_logger` as
  `-> FilteringBoundLogger`. Also: the reference uses `logging.getLevelName(...)` without importing
  `logging` — import it.
- **VALIDATE**: `uv run pytest tests/core/test_logging.py -q && uv run pyright app/core/logging.py`
- **SATISFIES**: AC4

### CREATE `app/core/exceptions.py`

- **IMPLEMENT**: `LocalProspectEngineError(Exception)` base, carrying `message: str` and `code: str`. Derive
  `ConfigurationError`, `DatabaseError`, `CostLimitExceededError`.
- **PATTERN**: `.claude/references/backend-api-best-practices.md:20-26`
- **GOTCHA**: `CostLimitExceededError` is **not** a crash path — T6 catches it to mark a run *degraded*.
  Document that in its docstring so a later slice does not turn it into a 500.
- **VALIDATE**: `uv run pyright app/core/exceptions.py`
- **SATISFIES**: AC1

### CREATE `app/core/database.py`

- **IMPLEMENT**: `class Base(DeclarativeBase)`, `create_async_engine(settings.database_url, pool_pre_ping=True)`, `async_sessionmaker(engine, expire_on_commit=False)`, and an async `get_db()` dependency yielding `AsyncSession`.
- **PATTERN**: MIRROR the *placement and role* of `.claude/references/vertical-slice-architecture.md:107-140`
- **IMPORTS**: `from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine`, `from sqlalchemy.orm import DeclarativeBase`
- **GOTCHA**: **The reference example is sync SQLAlchemy 1.x and must not be copied.** Three specific traps:
  (1) `declarative_base()` from `sqlalchemy.ext.declarative` is legacy and untyped — use
  `class Base(DeclarativeBase): pass`, or Pyright strict fails on every model that inherits it.
  (2) The URL needs the async driver: `postgresql+asyncpg://`, not `postgresql://`.
  (3) **Use Supabase's direct connection (port 5432), not the pooled port 6543.** The pooler is PgBouncer in
  transaction mode, which breaks asyncpg's prepared statements — the symptom is an intermittent
  `PreparedStatementError` / `DuplicatePreparedStatementError` under any concurrency, and it is maddening to
  diagnose. One user and one weekly run needs no pooler. If the pooled port is ever required, pass
  `connect_args={"statement_cache_size": 0}` **and** `prepared_statement_cache_size=0` on the engine. Record
  whichever choice is made in `.env.example` with a comment.
- **VALIDATE**: `uv run pyright app/core/database.py && uv run python -c "import app.core.database"`
- **SATISFIES**: AC5

### CREATE `app/core/cost.py`

- **IMPLEMENT**: a `RunCost` accumulator — `record(kind: BillableKind, count: int = 1, usd: Decimal | None = None)`, `total_calls(kind)`, `total_usd()`, and `check_cap(kind, cap)` raising `CostLimitExceededError`. `BillableKind` is a `StrEnum` (`places_lookup`, `anthropic_tokens`, ...). Every `record` emits `core.cost.call_recorded`.
- **PATTERN**: event naming from `core/logging.py`; cap default from `settings.max_places_calls_per_run`
- **IMPORTS**: `from decimal import Decimal`, `from enum import StrEnum`
- **GOTCHA**: use `Decimal`, never `float`, for money. This module is deliberately here rather than in T6
  (D7) — the first run that spends anything must already count it. Keep it storage-agnostic: it accumulates
  in memory for one run and T4 persists the total onto `sourcing_run`.
- **VALIDATE**: `uv run pytest tests/core/test_cost.py -q`
- **SATISFIES**: AC6

### CREATE `app/shared/provenance.py`

- **IMPLEMENT**: `RetrievalMethod(StrEnum)` with at minimum `registry_api`, `bulk_file`, `web_lookup`, `llm_inference`, `manual_hubspot_entry`. `ProvenancedValue[T](BaseModel)` with required `value`, `source_url`, `retrieved_at`, `retrieval_method`. `is_promotable(value: ProvenancedValue[T] | None) -> bool`.
- **PATTERN**: `.claude/references/vertical-slice-architecture.md:141-175` (Rule 2 — why this is `shared/` on day one: sourcing, qualification and promotion are three known consumers)
- **IMPORTS**: `from pydantic import BaseModel, HttpUrl, field_validator`, `from enum import StrEnum`, `from datetime import datetime`
- **GOTCHA**: this is the ticket's load-bearing piece. (1) Make the model **frozen** (`model_config = ConfigDict(frozen=True)`) — provenance that can be mutated after construction is not provenance. (2) `retrieved_at` must be timezone-aware; reject naive datetimes in a validator, or comparisons across sources silently mis-order. (3) `is_promotable()`'s docstring **must** state that the gate governs prospect *field* writes and never task creation — T13 adopts records with zero citable fields and would otherwise be refused outright. (4) `manual_hubspot_entry` exists precisely so adopted records can be honest rather than exempt; do not omit it as unused — T13 depends on it.
- **VALIDATE**: `uv run pytest tests/shared/test_provenance.py -q && uv run pyright app/shared/provenance.py`
- **SATISFIES**: AC7, AC8

### CREATE `app/core/middleware.py` + `app/core/dependencies.py`

- **IMPLEMENT**: correlation-id middleware setting `request_id_var` per request and echoing `X-Request-ID`; dependencies exposing settings, the db session, and a per-run `RunCost`.
- **PATTERN**: `.claude/references/vertical-slice-architecture.md:362-424`
- **GOTCHA**: set the context var **before** anything logs, or the first log lines of a request lose their id.
- **VALIDATE**: `uv run pyright app/core/middleware.py app/core/dependencies.py`
- **SATISFIES**: AC4

### CREATE `app/main.py`

- **IMPLEMENT**: `lifespan` calling `setup_logging()` on startup and disposing the engine on shutdown; middleware registered; centralized exception handlers; `GET /health` returning `{"status": "ok", "version": ...}`.
- **PATTERN**: `.claude/references/backend-api-best-practices.md:1-35`
- **GOTCHA**: `/health` must **not** hit the database — a health check that fails when Supabase blips is a
  liveness check masquerading as one. If a DB check is wanted later, it belongs at `/health/ready`.
- **VALIDATE**: `uv run pytest tests/core/test_health.py -q && uv run uvicorn app.main:app --port 8000 &  sleep 2; curl -fsS localhost:8000/health; kill %1`
- **SATISFIES**: AC2

### CREATE `alembic/` + baseline migration

- **IMPLEMENT**: `alembic init`, then convert `env.py` to the async recipe; `0001_baseline` with empty `upgrade()`/`downgrade()`; target metadata = `Base.metadata`.
- **PATTERN**: the Alembic async cookbook entry linked above
- **GOTCHA**: default `alembic init` writes a **sync** `env.py` that will fail against an async engine — use
  `alembic init -t async`, or port it by hand. Alembic should use the **direct** port for the same prepared-
  statement reason as the app. The baseline defines **no domain tables** — T2 and T4 add theirs.
- **VALIDATE**: `uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head`
- **SATISFIES**: AC5

### CREATE `docker-compose.yml` + `.env.example`

- **IMPLEMENT**: one service (the app). `.env.example` lists every variable with a comment, no secrets.
- **GOTCHA**: **no `postgres` service** — Supabase is hosted (D9). `.env.example` must show the
  `postgresql+asyncpg://` scheme and carry a comment naming the direct-vs-pooled port decision and why.
- **VALIDATE**: `docker compose config -q && grep -q 'postgresql+asyncpg' .env.example && ! grep -qE '^\s+postgres:' docker-compose.yml`
- **SATISFIES**: AC9

### CREATE `tests/test_structure.py`

- **IMPLEMENT**: assert the absent-by-design things are absent — no `redis`, `langfuse`, `celery` in
  dependencies; no `postgres:` service in compose; no `# type: ignore` or bare `Any` annotation anywhere in `app/`.
- **GOTCHA**: scope the `Any` check to annotations, not the substring — `Any` appears inside words like
  `Anything` and in imports the code does not use. Match `: Any` and `-> Any`.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q`
- **SATISFIES**: AC1, AC9, AC10

### RUN full validation and commit

- **VALIDATE**: `uv run ruff check . && uv run mypy . && uv run pyright && uv run pytest`
- **GOTCHA**: commit per `.claude/references/conventions.md` — conventional tag, imperative, ≤72 chars,
  **no AI attribution**.
- **SATISFIES**: AC1

---

## TESTING STRATEGY

pytest with `asyncio_mode = "auto"`. Tests mirror the source tree (`tests/core/`, `tests/shared/`).

### Unit Tests

- **config** — required variable missing → `ValidationError` at startup; `extra="forbid"` rejects an unknown
  variable; defaults land where expected
- **logging** — an emitted event is valid JSON carrying `request_id`, `level`, `timestamp`; `exc_info=True`
  produces a formatted traceback string
- **cost** — `record` accumulates; `check_cap` raises `CostLimitExceededError` at the boundary, not one
  call early or late; money arithmetic stays `Decimal`
- **provenance** — the load-bearing suite:
  - constructing with any one of the three fields missing raises `ValidationError` (three separate cases)
  - the model is frozen: mutation raises
  - a naive `retrieved_at` is rejected
  - `is_promotable(None)` is `False`; a fully provenanced value is `True`
  - `RetrievalMethod.manual_hubspot_entry` exists (T13's dependency, asserted here so it cannot be deleted
    as unused)
  - generic parameterization actually validates: `ProvenancedValue[int](value="abc", ...)` raises

### Integration Tests

- `GET /health` returns 200 with the expected shape, via `httpx.AsyncClient` against the app
- `X-Request-ID` supplied by the client is echoed; when absent, one is generated
- Alembic `upgrade head` → `downgrade base` → `upgrade head` round-trips cleanly

### Edge Cases

- Missing `.env` entirely — the app must fail loudly at startup, not boot with silent defaults
- Unknown/typo'd env variable — rejected by `extra="forbid"`
- `retrieved_at` in a non-UTC timezone — accepted and normalized, not rejected
- `ProvenancedValue[T]` where `T` is itself a model — nested validation still applies
- Cost cap of `0` — every billable call raises immediately (degenerate but must not divide-by-zero or pass)

---

## VALIDATION COMMANDS

Execute every command to ensure zero regressions and 100% feature correctness.

### Level 1: Syntax & Style
```bash
uv run ruff check .
uv run ruff format --check .
```

### Level 2: Types
```bash
uv run mypy .
uv run pyright
```
Both must pass. Zero `# type: ignore`, zero `Any` escape hatches.

### Level 3: Unit Tests
```bash
uv run pytest -q
uv run pytest tests/shared/test_provenance.py -v   # the load-bearing suite
```

### Level 4: Manual Validation
```bash
uv run uvicorn app.main:app --reload
curl -fsS localhost:8000/health
curl -fsS -H 'X-Request-ID: abc123' localhost:8000/health -D - | grep -i x-request-id
uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
docker compose config -q
```

### Level 5: Everything at once
```bash
/piv-validate
```

---

## ACCEPTANCE CRITERIA

- [ ] **AC1** — `ruff`, `mypy --strict`, `pyright --strict` and `pytest` all pass with **zero suppressions**
- [ ] **AC2** — FastAPI app boots; `GET /health` returns 200 without touching the database
- [ ] **AC3** — `core/config` loads from env, is strict 12-factor (no local paths), fails loudly on a missing
      required variable and on an unknown one
- [ ] **AC4** — `core/logging` emits structlog JSON with a correlation id; event names follow
      `domain.component.action_state`
- [ ] **AC5** — `core/database` provides an async session over hosted Supabase; Alembic baseline applies and
      reverses cleanly
- [ ] **AC6** — `core/cost` records billable calls per run and enforces a configurable cap by raising
      `CostLimitExceededError`
- [ ] **AC7** — `ProvenancedValue[T]` cannot be constructed without all three provenance fields; it is frozen;
      `retrieved_at` must be timezone-aware
- [ ] **AC8** — `is_promotable()` exists and its docstring records that the gate governs prospect field
      writes, not task creation
- [ ] **AC9** — Compose runs the app alone; **no Postgres service**; `.env.example` documents the
      direct-vs-pooled port decision
- [ ] **AC10** — Redis, Langfuse, Celery, frontend, auth and RLS are absent, and a test asserts it
- [ ] No regressions (nothing to regress — this is the first slice)

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully
- [ ] Full test suite passes
- [ ] No linting or type checking errors, zero suppressions
- [ ] Manual testing confirms `/health` and the migration round-trip
- [ ] Acceptance criteria all met
- [ ] `_tasks/todo.md` updated; committed per `.claude/references/conventions.md`

---

## OPEN QUESTIONS / ASSUMPTIONS

**Assumptions this plan makes** — flag rather than silently diverge if any is wrong:

1. **A Supabase project exists, or will be created before implementation.** T1 cannot run its migration
   without a reachable `database_url`. If it does not exist yet, everything except the Alembic task can still
   be completed.
2. **Direct connection (5432), not the pooler.** Justified by one user and one weekly run. Revisit only if
   connection limits ever bite.
3. **`app_name` / package name is `local-prospect-engine`** with the import root `app`. The CLI in T2 is
   assumed to be `lpe`; T1 only needs `pyproject.toml` shaped so adding `[project.scripts]` is trivial.
4. **No Claude Agent SDK dependency in T1.** Added at first use (T6/T12).
5. **`RetrievalMethod` members** are a first guess beyond `manual_hubspot_entry` (which is required). T5/T6
   may add members; that is a one-line change and not a schema break.

**Settled:**

- **`core/cost.py` stays in `core/`. Confirmed 2026-09-26.** It is a knowing deviation from "core is what
  exists before any feature" — nothing in T1 spends money. It is here because D7 chose to measure rather
  than cap, and measurement that arrives with the first spender arrives too late to set a ceiling from. Treat
  it as infrastructure that predates the features which will use it, not as a speculative abstraction: T6
  (Places) and T12 (Anthropic) are both known consumers, which also satisfies the three-feature rule's
  intent. AC6 stands.

- **`ProvenancedValue` is frozen. Confirmed 2026-09-26.** `model_config = ConfigDict(frozen=True)`. A
  citation is a fact about how a value was obtained, so re-citing it means obtaining it again — a slice
  needing a new citation constructs a new value rather than mutating one. Decided before nine dependent
  tickets consume it, because loosening this later is cheap and tightening it later is not. AC7 stands.

**Questions that would change this plan if answered differently:**

- None outstanding. Both prior questions are settled above. The five assumptions remain, of which #1 — a
  reachable Supabase project — is the only one that can block a task.

---

## NOTES (open canvas)

**Why the reference examples are dangerous here.** This project's own
`vertical-slice-architecture.md` is the right guide to *structure* and the wrong guide to *code*. It predates
the decisions in CLAUDE.md, and three of its examples actively conflict with the ground rules:

| Reference shows | This project requires | Consequence if copied |
|---|---|---|
| `create_engine` / `Session` (sync) | `create_async_engine` / `AsyncSession` | Blocking calls inside async handlers — the exact anti-pattern `backend-api-best-practices.md` names |
| `declarative_base()` | `class Base(DeclarativeBase)` | Pyright strict fails on every inheriting model; no suppression allowed |
| `get_logger(name) -> Any` | `-> FilteringBoundLogger` | Violates the zero-`Any` ground rule directly |
| `secret_key` / `algorithm` in Settings | no auth at all | Dead config that implies an auth story the MVP explicitly excluded |

An implementing agent that mirrors the reference faithfully will produce code that fails validation. Hence
the warning at the top of this plan.

**The prepared-statement trap, stated plainly.** Supabase exposes a pooled port (6543, PgBouncer transaction
mode) and a direct port (5432). asyncpg uses prepared statements; PgBouncer in transaction mode does not keep
a session long enough for them to survive. The failure is *intermittent* and load-dependent — it passes local
testing and fails when two things happen at once, which for us would be the weekly run. Choosing the direct
port now avoids the whole class of problem, and the trade (no pooling) costs nothing at one user.

**Why provenance is a type rather than a check.** The alternative — validate provenance at the HubSpot
boundary — was considered and rejected. A check can be forgotten by a new slice; a type cannot be constructed
wrongly in the first place. E18 is what forgetting looks like: 14 of 34 contacts with a company name in the
first-name field, written by a process that had no opinion about whether anyone could cite it. Making
`ProvenancedValue` frozen and its fields required means the failure mode moves from "wrong data in HubSpot"
to "code that does not compile."

**Sequencing note for the wave.** Phase 3 (provenance) is independent of Phase 2 (core infra). If this ticket
is split across two sessions or two people, that is the seam — and provenance is the half worth doing with
full attention.

---

## AMENDMENTS

*(append-only; newest at the bottom; leave empty at creation)*

- 2026-09-26 — `core/cost.py` confirmed to stay in `core/` rather than moving to T6. It was raised as an open
  question because nothing in T1 spends money; resolved in favour of keeping it, since D7 chose to measure
  cost rather than cap it and the first spender would otherwise arrive uninstrumented. AC6 unchanged.
- 2026-09-26 — `ProvenancedValue` confirmed frozen. The plan already specified `frozen=True` in the
  provenance task's gotchas, AC7 and the test list; this closes the open question behind it so the constraint
  is a decision rather than a proposal. No task, criterion or test changed.
