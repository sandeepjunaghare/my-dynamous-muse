# Feature: T4 — Sourcing data model (`sourcing_run` + `candidate`, field-level provenance)

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files.

> **Read this first.** T2 (`app/manifests/`) is the house pattern and the thing to copy: typed
> `Mapped[...]`/`mapped_column(...)` models on `app.core.database.Base`, JSONB for content nobody queries
> inside, a hand-written migration, *the repository flushes and the service commits*, slice exceptions on
> `LocalProspectEngineError` with `default_code`/`status_code` ClassVars, `requires_db` on every
> database-backed test. `.claude/references/vertical-slice-architecture.md` is right about *structure* and
> wrong about *code* (sync SQLAlchemy 1.x, `Column(...)`, `-> Any`) — do not copy its examples.

## Feature Description

The sourcing workbench gets its two tables. A **`sourcing_run`** is one execution of a brief — *vertical ·
ICP band · geography* — with its timings, its stage counts, what it cost, and where it ended up
(`running → completed | degraded | failed`). A **`candidate`** is one business that run sourced,
pre-qualification, and **every field on it carries its own provenance**: the source URL, when it was
retrieved, and how.

This is the ticket that makes "provenance is a write-gate" true *at rest*. T1 made an uncited value
unrepresentable as a `ProvenancedValue`; T3 enforces `is_promotable()` at the HubSpot boundary; T4 is what
sits between them — the place a candidate lives for the length of a run, where a field may be absent (not
yet found, or not citable) without the candidate being refused, and where a citation survives the trip
through Postgres intact.

No pipeline stage, no source adapter, no HubSpot write, no route. T5 (pipeline + FMCSA), T7
(qualification), T8 (routing) and T6 (enrichment) all build on these two tables.

## User Story

As **the sourcing pipeline (T5–T8), and the founder reading its output**
I want **each run and each sourced business persisted with its brief, its cost and a citation on every field**
So that **a run is explainable after the fact, a re-run is idempotent, and nothing uncited can ever be
promoted to HubSpot — while the workbench can still hold a candidate whose fields are not all cited yet**.

## Problem Statement

There is nowhere for sourcing state to live. Without a run table, per-run cost (D9: *measure first*) has no
home — `app/core/cost.py`'s docstring already says "T4 persists the totals onto `sourcing_run`". Without a
candidate table carrying field-level provenance, the first adapter (T5) would have to choose between
dropping citations at the database (E18 again, one layer down) or refusing to store a candidate the moment
one field is uncited — which would make the workbench useless, since T6 fills owner and headcount *after*
qualification (D8/D10), so almost every candidate is partially cited for most of its life.

## Solution Statement

Two tables, following T2's split: **identity, lifecycle and keys in real columns; cited content in JSONB**
validated by Pydantic on the way in and out.

- **`sourcing_run`** — columns `id`, `manifest_id` (FK → `vertical_manifest.id`), `vertical`, `geography`,
  `icp_band` (JSONB, validated by the manifests slice's existing `IcpBand`), `status` (CHECK-constrained),
  `status_detail`, `started_at`, `finished_at`, `counts` (JSONB `{stage_name: n}`), `cost` (JSONB, the
  per-kind calls/USD a `RunCost` accumulated). A CHECK ties `finished_at` to status: a running run has no
  finish time, a finished run must.
- **`candidate`** — columns `id`, `run_id` (FK → `sourcing_run.id`), `registry_id`, `fields` (JSONB,
  validated by `CandidateFields`), `created_at`, `updated_at`. **Unique `(run_id, registry_id)`** is the
  upsert key. `CandidateFields` wraps every field in `ProvenancedValue[T]`; only `registry_id` is
  required — the rest are `ProvenancedValue[T] | None`, and `None` is how "unprovenanced" is stored. A
  CHECK makes the `registry_id` column equal the cited `fields.registry_id.value`, so the key and its
  citation can never drift apart.
- **Repository** — `create_run`, `get_run`, `mark_finished`, `upsert_candidate` (Postgres
  `INSERT … ON CONFLICT (run_id, registry_id) DO UPDATE`), `get_candidate`, `list_candidates`
  (deterministic order). Flushes, never commits.
- **Service** — `start_run(brief)` resolves the vertical's **ACTIVE** manifest (refusing when there is none)
  and opens a run; `record_candidates(run_id, …)` upserts into a *running* run only; `finish_run(run_id,
  outcome, counts=…, cost=…)` closes it and persists counts and cost; `get_run`, `list_candidates`. Owns
  the commit.

"Storable but not promotable" falls out of the shape: a candidate whose `phone` is `None` persists and
reloads fine, and `is_promotable(candidate.fields.phone)` is `False`. `CandidateFields.unprovenanced_fields()`
names the absent fields so T9 can say *which* field refused, without T4 deciding which fields are required
for promotion (that is T9's call).

## Out of Scope / Non-Goals

- **No pipeline, no stages, no `app/tools/`** — T5 establishes the runner and the registry.
- **No FMCSA (or any) source adapter** — T5. No census-file parsing, no QCMobile.
- **No HubSpot writes, no promotion ledger** — T9. `is_promotable` is consumed, not changed.
- **No routes, no CLI.** Nothing reads runs over HTTP until T10's report; nothing in the ticket asks for a
  surface. (Recorded as a decision.)
- **No `disqualification` table, no re-source suppression** — T7. No index on `candidate.registry_id`
  alone (the cross-run lookup T7 needs) — T7 adds it in its own revision with the query that uses it.
- **No enrichment fields** (owner, headcount band, years in business, TMS). `fields` is JSONB precisely so
  T6 adds them to `CandidateFields` with **no migration**. T4 carries *business identity* only: registry
  id, legal name, DBA name, address, phone, website.
- **No vertical-specific registry attributes** (MC number, authority status, BOC-3). Whether those are
  candidate fields or qualification inputs is T5/T7's call against real data.
- **No run-lock / "one running run per vertical" constraint.** A run-lock is the stated trigger for
  reopening the Redis decision; it is not T4's to introduce.
- **Not changing** `app/shared/provenance.py`, `app/core/cost.py`, `app/manifests/*`, or any applied
  migration (`0001`–`0003` — the amend window is closed).
- **Not editing `CLAUDE.md`'s architecture map** in this branch — T11 and T12 are in flight in sibling
  worktrees and all three would conflict on the same lines; the map update follows the merge, as T2's and
  T3's did (`9bbf26c`, `bbed27a`).

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium — two tables and a thin lifecycle, but it fixes the at-rest shape of
provenance that T5–T9 all read, and its migration is the one T11's `0005_cadence` rebases onto
**Primary Systems Affected**: `app/sourcing/` (new), `alembic/versions/0004_sourcing.py` (new),
`alembic/env.py` (one import), `tests/sourcing/` (new)
**Dependencies**: none new. `sqlalchemy.dialects.postgresql.insert` / `JSONB` are already installed.

## Related Work

**Implements**: T4 in [`docs/tickets/local-prospect-engine.md`](../../docs/tickets/local-prospect-engine.md)
**Epic**: [`docs/local-prospect-engine.architecture.md`](../../docs/local-prospect-engine.architecture.md) →
*Data model — the shape* · PRD §6 step 1 (*brief in*)

**Back-references** (decisions inherited — do **not** reopen):

- `.claude/plans/t1-scaffold-core-provenance.md` — `ProvenancedValue[T]`, `is_promotable`, `RunCost`
  ("T4 persists the totals onto `sourcing_run`")
- `.claude/plans/t2-vertical-manifest.md` — the slice shape, JSONB-body pattern, hand-written migration,
  `model_dump(mode="json")` gotcha, partial-index-in-both-places lesson, `requires_db` fixtures
- `_tasks/todo.md` → D1 (deterministic pipeline), D9 (measure cost, 500-call breaker), D10 (qualify before
  verify), the closed amend window, the per-worktree database rule

**Forward-references**:

- T5 (pipeline + FMCSA) calls `start_run` / `record_candidates` / `finish_run`
- T6 adds enrichment fields to `CandidateFields` and catches `CostLimitExceededError` → `finish_run(degraded)`
- T7 FKs `disqualification` to `candidate`, reads `candidate.registry_id` across runs
- T8 reads `fields.address`; T9 FKs the promotion ledger to `candidate` and gates on `is_promotable`
- T11's `0005_cadence` sets `down_revision = "0004_sourcing"`

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `CLAUDE.md` — ground rules: provenance write-gate, vertical is data, zero suppressions, no `Any`,
  `domain.component.action_state` logs, "never silently answer an open question"
- `app/shared/provenance.py` — `ProvenancedValue[T]` (frozen; `_assert_immutable` rejects list/dict/set and
  non-frozen models inside `value`; `retrieved_at` normalised to UTC; blank `source_url` rejected),
  `RetrievalMethod`, `is_promotable`
- `app/core/cost.py` — `RunCost.total_calls(kind)`, `.total_usd(kind)`, `BillableKind`; money is `Decimal`
- `app/core/database.py` — `Base(DeclarativeBase)`
- `app/core/exceptions.py` — `LocalProspectEngineError`, ClassVar codes, `CostLimitExceededError` example
- `app/manifests/models.py` — typed model, JSONB `Mapped[dict[str, object]]`, CHECK on a status string,
  indexes declared in the model **and** the migration so `alembic check` stays clean
- `app/manifests/schemas.py` — `SLUG_PATTERN`, `IcpBand` (reused for the brief), frozen leaves, tuples
- `app/manifests/repository.py` — flush-don't-commit, `model_dump(mode="json")` into JSONB, log events
- `app/manifests/service.py` — `get_active` raising `ActiveManifestNotFoundError`; service commits
- `app/manifests/exceptions.py` — exception shape with structured attributes
- `alembic/versions/0002_vertical_manifest.py` — hand-written migration style
- `alembic/env.py` — `_REGISTERED_MODELS` tuple; each slice appends one import and one entry
- `tests/conftest.py` — `requires_db`, `migrated_database`, `db_session` (savepoint; service `commit()` is real)
- `tests/manifests/builders.py` — `a_vertical()`, `cited()`, `a_body()`
- `tests/test_structure.py` — no empty packages, single migration head, no `Any`, no `if vertical ==`

### New Files to Create

- `app/sourcing/__init__.py` — package docstring
- `app/sourcing/schemas.py` — `RunStatus`, `RunOutcome`, `SourcingBrief`, `PostalAddress`,
  `CandidateFields`, `RunCostSummary`, `SourcingRunResponse`, `CandidateResponse`
- `app/sourcing/models.py` — `SourcingRun`, `Candidate`
- `app/sourcing/exceptions.py` — `SourcingError`, `SourcingRunNotFoundError`, `SourcingRunNotRunningError`
- `app/sourcing/repository.py` — `SourcingRepository`
- `app/sourcing/service.py` — `SourcingService`
- `app/sourcing/README.md` — the slice's shape, lifecycle, and what is deliberately absent
- `alembic/versions/0004_sourcing.py` — revision `0004_sourcing`, `down_revision = "0003_seed_freight_and_fire"`
- `tests/sourcing/{__init__,builders,test_schemas,test_repository,test_service}.py`

### Relevant Documentation

- SQLAlchemy 2.0 — PostgreSQL `INSERT … ON CONFLICT`:
  https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#insert-on-conflict-upsert
  - Why: `insert(...).on_conflict_do_update(index_elements=..., set_=...)`, `stmt.excluded`
- SQLAlchemy 2.0 — ORM bulk/upsert with RETURNING and `populate_existing`:
  https://docs.sqlalchemy.org/en/20/orm/queryguide/dml.html#orm-upsert-statements
  - Why: returning ORM objects from an upsert must refresh the identity map, or a second upsert of the
    same key hands back the stale first object
- PostgreSQL JSONB `||` concatenation: https://www.postgresql.org/docs/16/functions-json.html
  - Why: the merge semantics of the upsert

### Patterns to Follow

**Model (T2):**
```python
id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
body: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
__table_args__ = (CheckConstraint("status in (...)", name="ck_..."), Index("ix_...", "vertical"))
```
**JSONB writes:** always `model.model_dump(mode="json")` — plain `model_dump()` leaves `datetime`/`Decimal`
objects that asyncpg cannot encode into JSONB.
**Logging:** `logger = get_logger(__name__)`; events `sourcing.repository.run_created`,
`sourcing.repository.candidate_upserted`, `sourcing.service.run_started`,
`sourcing.service.candidates_recorded`, `sourcing.service.run_finished`, `sourcing.service.write_refused`.
**Exceptions:** subclass with `default_code`/`status_code` ClassVars and keyword-only structured attributes.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — schemas and exceptions (no database)
### Phase 2: Persistence — models, migration, `env.py` registration
### Phase 3: Behaviour — repository, service
### Phase 4: Tests and docs

---

## STEP-BY-STEP TASKS

### CREATE `app/sourcing/__init__.py`
- **IMPLEMENT**: one-line docstring.
- **VALIDATE**: `uv run ruff check app/sourcing`

### CREATE `app/sourcing/exceptions.py`
- **IMPLEMENT**: `SourcingError(LocalProspectEngineError)` code `sourcing_error`;
  `SourcingRunNotFoundError` (404, `run_id: str`); `SourcingRunNotRunningError` (409, `run_id: str`,
  `status: str`) — writing candidates into, or finishing, a run that has already finished.
- **PATTERN**: `app/manifests/exceptions.py`
- **SATISFIES**: AC3 (run lifecycle service)

### CREATE `app/sourcing/schemas.py`
- **IMPLEMENT**:
  - `RunStatus(StrEnum)`: `running`, `completed`, `degraded`, `failed`. `RunOutcome(StrEnum)`: the three
    terminal members only, so `finish_run(..., RunOutcome.running)` is a type error rather than a runtime
    check. `degraded` exists because T6 marks a run degraded when the Places breaker trips.
  - `SourcingBrief` (frozen): `vertical` (`SLUG_PATTERN`, ≤64), `geography` (`SLUG_PATTERN`, ≤64),
    `icp_band: IcpBand` (imported from `app.manifests.schemas` — a read of another slice's vocabulary,
    which VSA allows; reusing it keeps one definition of "an ICP band" and its ordering validator).
  - `PostalAddress` (frozen): `street`, `city`, `state`, `postal_code` — all `str`, `min_length=1`.
    Frozen because `ProvenancedValue` rejects a non-frozen model inside `value`.
  - `CandidateFields` (frozen, `extra="forbid"`): `registry_id: ProvenancedValue[str]` (required; a
    validator rejects a blank value), `legal_name`, `dba_name`, `phone`, `website:
    ProvenancedValue[str] | None = None`, `address: ProvenancedValue[PostalAddress] | None = None`.
    `unprovenanced_fields() -> tuple[str, ...]` — names of fields that are `None`, in declaration order.
  - `RunCostSummary`: `calls: dict[BillableKind, int]`, `usd: dict[BillableKind, Decimal]`;
    `from_run_cost(cost: RunCost)` iterates every `BillableKind` (stable shape); `total_usd()`.
  - `RunCounts` type: `dict[str, int]` keys `SLUG_PATTERN`, values `ge=0` — validated via a small model
    `RunTotals(counts, cost)` built in the service.
  - `SourcingRunResponse` (`from_attributes`): every column, `icp_band: IcpBand`, `status: RunStatus`,
    `counts: dict[str, int]`, `cost: RunCostSummary`.
  - `CandidateResponse` (`from_attributes`): `id`, `run_id`, `registry_id`, `fields: CandidateFields`,
    `created_at`, `updated_at`.
- **GOTCHA**: `ProvenancedValue` is generic; `ProvenancedValue[PostalAddress]` validates nested dicts on
  load (T2 does the same with `ProvenancedValue[ManifestSource]`). Tuples not lists anywhere inside a
  citation.
- **VALIDATE**: `uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC1 (brief, counts, cost, status; field-level provenance), AC4 (storable-not-promotable)

### CREATE `app/sourcing/models.py`
- **IMPLEMENT**: `SourcingRun` and `Candidate` as described in *Solution Statement*. Constraints:
  - `ck_sourcing_run_status`: `status in ('running','completed','degraded','failed')`
  - `ck_sourcing_run_finished_at_matches_status`: `(status = 'running') = (finished_at is null)`
  - `ix_sourcing_run_vertical` on `vertical`
  - `uq_candidate_run_registry_id` unique on `(run_id, registry_id)`
  - `ck_candidate_registry_id_is_cited`: `registry_id = (fields -> 'registry_id' ->> 'value')`
  - FKs: `manifest_id → vertical_manifest.id`, `run_id → sourcing_run.id`, no cascade (nothing deletes
    runs; T9's ledger will FK candidates, and a silent cascade would orphan it)
  - server defaults: `started_at`/`created_at`/`updated_at` `now()`, `counts`/`cost` `'{}'::jsonb`
- **GOTCHA**: `ForeignKey(VerticalManifest.id)` (import the model) rather than a string, so the referenced
  table is always on `Base.metadata` when the unit of work sorts dependencies.
- **VALIDATE**: `uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC1, AC2 (upsert key)

### CREATE `alembic/versions/0004_sourcing.py`
- **IMPLEMENT**: hand-written, mirroring `0002`; `revision = "0004_sourcing"`,
  `down_revision = "0003_seed_freight_and_fire"`. Create `sourcing_run` then `candidate`; downgrade drops
  in reverse.
- **GOTCHA**: the revision id is **fixed** — T11 is writing `0005_cadence` against it.
- **VALIDATE**: `TEST_DATABASE_URL=… uv run alembic upgrade head && alembic downgrade -1 && alembic upgrade head && alembic check`
  (with `DATABASE_URL` pointed at the throwaway DB)
- **SATISFIES**: AC1

### UPDATE `alembic/env.py`
- **ADD**: `from app.sourcing import models as sourcing_models`; append `SourcingRun`, `Candidate` to
  `_REGISTERED_MODELS`.
- **GOTCHA**: T11 appends to the same tuple → a trivial merge conflict, expected; resolve by keeping both.

### CREATE `app/sourcing/repository.py`
- **IMPLEMENT**:
  - `create_run(brief, manifest_id) -> SourcingRun` — status `running`; flush; log.
  - `get_run(run_id) -> SourcingRun | None`.
  - `mark_finished(run, outcome, counts, cost, detail)` — sets status, `finished_at = now(UTC)`,
    `counts`, `cost`, `status_detail`; flush; log.
  - `upsert_candidate(run_id, fields) -> Candidate` —
    `insert(Candidate).values(run_id=…, registry_id=fields.registry_id.value,
    fields=fields.model_dump(mode="json", exclude_none=True))
    .on_conflict_do_update(constraint="uq_candidate_run_registry_id",
    set_={"fields": Candidate.fields.op("||")(excluded["fields"]), "updated_at": func.now()})
    .returning(Candidate)`, executed with `execution_options(populate_existing=True)`.
    **Merge, not replace**: a key present in the incoming dump overwrites; a field the incoming write does
    not carry keeps its existing citation. Same input twice → same row, same content.
  - `get_candidate(candidate_id)`, `list_candidates(run_id)` ordered by `registry_id`, then `id`
    (deterministic — T5's determinism test reads this).
- **GOTCHA**: `exclude_none=True` is what makes the merge safe — without it every absent field is an
  explicit JSON `null` that would erase an existing citation.
- **VALIDATE**: `uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC2

### CREATE `app/sourcing/service.py`
- **IMPLEMENT**: `SourcingService(session)`:
  - `start_run(brief)` → `ManifestService(session).get_active(brief.vertical)` (raises
    `ActiveManifestNotFoundError` when only drafts exist — a run must not source against an unaccepted
    proposal) → `create_run(brief, manifest.id)` → commit → `SourcingRunResponse`.
  - `record_candidates(run_id, candidates: Sequence[CandidateFields])` → run must exist (404) and be
    `running` (409) → upsert each → commit → `list[CandidateResponse]`.
  - `finish_run(run_id, outcome, *, counts, cost: RunCost, detail=None)` → exists, running → validate
    totals → `mark_finished` → commit.
  - `get_run(run_id)`, `list_candidates(run_id)` (404 when the run does not exist).
- **VALIDATE**: `uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC3

### CREATE `tests/sourcing/builders.py`, `test_schemas.py`, `test_repository.py`, `test_service.py`
- See *Testing Strategy*. DB modules carry `pytestmark = requires_db`.
- **VALIDATE**: `TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/lpe_t4 uv run pytest tests/sourcing -q`

### CREATE `app/sourcing/README.md`
- Shape, lifecycle diagram, upsert semantics, storable-not-promotable, deliberately absent.

---

## TESTING STRATEGY

### Unit (no DB) — `test_schemas.py`
- Brief: valid; non-slug vertical/geography rejected; inverted ICP band rejected (inherited validator).
- `CandidateFields`: `registry_id` required; blank registry id rejected; unknown key rejected;
  `unprovenanced_fields()` names exactly the `None` fields; `is_promotable` is `False` for an absent field
  and `True` for a cited one; a mutable/non-frozen address cannot be cited.
- JSON round-trip (`model_dump(mode="json")` → `model_validate`) is equal, `retrieved_at` still tz-aware
  UTC, method still the enum.
- `RunCostSummary.from_run_cost`: per-kind calls and `Decimal` USD, total; every kind present; JSON
  round-trip keeps `Decimal` exact.

### Integration (DB) — `test_repository.py`
- Run defaults: `running`, `started_at` set, `finished_at` None, `counts == {}`.
- **Round-trip: provenance survives persist → load** — upsert, `expunge_all()`, reload; every field equal,
  including `retrieved_at` and method; the absent field is still absent.
- Upsert twice with identical input → one row, same id, same `fields`.
- Upsert merge: second write without `phone` keeps the first write's cited phone; a second write *with* a
  different cited `legal_name` replaces it.
- Same registry id in two runs → two rows.
- `list_candidates` ordering by `registry_id`.
- DB refuses: duplicate `(run_id, registry_id)` via plain insert; `registry_id` column disagreeing with
  the cited value (CHECK); `status='running'` with a `finished_at` (CHECK); unknown status (CHECK).

### Integration (DB) — `test_service.py`
- `start_run` refuses a vertical with only a DRAFT manifest (`ActiveManifestNotFoundError`), and records
  the ACTIVE manifest's id when there is one.
- `record_candidates` → 404 on unknown run, 409 on a finished run; idempotent across two calls.
- **Storable but not promotable**, end to end: a candidate with an uncited phone is recorded and listed;
  `is_promotable(phone)` is `False`, `is_promotable(legal_name)` is `True`.
- `finish_run` persists outcome, `finished_at`, counts, and cost (a `RunCost` with Places calls + USD);
  finishing twice → 409; negative count rejected.

### Edge cases
- Merge never erases a citation; identical re-upsert changes nothing but `updated_at`.
- A run with zero candidates finishes cleanly.

---

## VALIDATION COMMANDS

```bash
cd <worktree>/agentic-course-2.0/my-example-app
uv run ruff check . && uv run ruff format --check .
uv run mypy . && uv run pyright
uv run pytest                                    # no DB: DB tests skip
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/lpe_t4
uv run pytest                                    # DB tests run, none skipped
DATABASE_URL=$TEST_DATABASE_URL uv run alembic downgrade 0003_seed_freight_and_fire
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
DATABASE_URL=$TEST_DATABASE_URL uv run alembic check
uv run alembic heads                             # exactly one: 0004_sourcing
```

---

## ACCEPTANCE CRITERIA

- [ ] AC1 `sourcing_run` (brief: vertical · ICP band · geography; timings; counts; cost; status) and
      `candidate` (business identity, each field carrying its own provenance) + migration `0004_sourcing`
- [ ] AC2 Repository with idempotent upsert keyed on `(run, registry id)`
- [ ] AC3 Run lifecycle service (`running → completed | degraded | failed`)
- [ ] AC4 Round-trip test: provenance survives persist → load
- [ ] AC5 A candidate with an unprovenanced field is storable, and `is_promotable` says no for that field
- [ ] ruff, mypy, pyright, pytest green with DB tests running; zero suppressions; no `Any`; no
      `if vertical ==`; one migration head

## COMPLETION CHECKLIST

- [ ] All tasks completed in order, each validated
- [ ] Full suite green with `TEST_DATABASE_URL` set
- [ ] Migration round-trips and `alembic check` is clean
- [ ] Report written to `.claude/reports/t4-sourcing-model-report.md`

---

## OPEN QUESTIONS / ASSUMPTIONS

None of these sit on the architecture doc's *Open questions* or PRD §9. Each is the conservative reading of
an ambiguity in the ticket, recorded so review can overturn it cheaply.

1. **"Unprovenanced" is stored as absent (`None`), not as a raw uncited value.** T1 made an uncited
   `ProvenancedValue` unrepresentable on purpose, and T6's AC says a field that cannot be cited is *left
   absent, not inferred*. A raw-value slot would reintroduce exactly the thing the primitive forbids.
2. **The upsert merges, it does not replace.** Incoming cited fields overwrite; fields the incoming write
   does not carry keep their citation. Rationale: within one run the source is one snapshot, and
   replace-semantics would let a stage-1 retry erase stage-3 enrichment. No "clear a field" path exists.
3. **The key is exactly `(run_id, registry_id)`**, as the ticket says. If a manifest ever declares two
   *registries* in one run, ids could collide across them; T5 would then namespace the id or a new
   revision adds the source to the key. Not solved speculatively.
4. **`sourcing_run.manifest_id` (FK, NOT NULL) is added beyond the ticket's column list.** A run that
   cannot say which manifest *version* it ran under cannot explain its candidates — T7's disqualification
   rows name rule ids that only mean something per version. Consequence: `start_run` refuses a vertical
   with no ACTIVE manifest, which is the behaviour T2's `get_active` docstring already asks for.
5. **The brief's ICP band reuses `IcpBand` from `app.manifests.schemas`** (cross-slice read of a type, not
   a table write). The brief records what was *asked*; whether it may differ from the manifest's band, and
   which wins, is T5/T7's call. T4 stores it and does not reconcile.
6. **`geography` is a slug string** (`dfw`), not an enum or a polygon. Multi-metro is a non-goal, but
   hard-coding DFW would be a literal branch on data.
7. **Counts are JSONB `{stage_name: n}`**, not one column per stage, so T5–T8 add their counts without
   each editing this table (and conflicting in parallel waves).
8. **Cost is JSONB `{calls: {kind: n}, usd: {kind: "decimal"}}`**, every `BillableKind` present. A total
   is computed, not stored — one fact, one home. Cost is persisted at `finish_run`; a process that dies
   mid-run leaves a `running` row with no cost (see NOTES).
9. **No routes and no CLI.** The ticket names no surface; T10 is the first reader.

## NOTES (open canvas)

**Why JSONB for candidate fields rather than a `candidate_field` child table or per-field columns.**
Per-field columns would be four columns per field (value, url, timestamp, method) and a migration for every
field T6 adds. A child table makes provenance queryable — but nothing in the MVP queries *inside*
provenance; every consumer reads a candidate whole (T7 evaluates rules against it, T8 reads its address, T9
gates each field). T2 made the same call for the manifest body and it has held. The two things that *are*
queried — the run and the registry id — are real columns.

**Why the registry id is stored twice, with a CHECK.** The key has to be a column (unique constraint, T7's
cross-run lookup). But it is also a field like any other, and "every field carries its own provenance"
includes it — a registry id nobody can cite is E10 (74 BOC-3 process agents instead of brokers). Storing the
citation in `fields` and the bare value in the column, with `registry_id = fields->'registry_id'->>'value'`
enforced by Postgres, makes the duplication harmless: they cannot disagree.

**A run that dies mid-flight.** No heartbeat, no reaper. A crashed process leaves a `running` row. T5's
runner is expected to wrap the pipeline and call `finish_run(failed, …)` in its error path; a stale-run
sweep is not needed at one weekly run and would be speculation.

**Upsert and the identity map.** `INSERT … ON CONFLICT … RETURNING` through the ORM returns `Candidate`
objects, but if the session already holds that primary key the stale in-memory object wins unless
`populate_existing=True` is set. The idempotency test catches this if it is forgotten.

**Throughput.** One statement per candidate. At ~600 census rows per run against hosted Supabase that is
seconds, once a week. A batched `INSERT … ON CONFLICT` would fail on a batch containing the same registry id
twice ("cannot affect row a second time") — the per-row loop sidesteps that for free.

## AMENDMENTS

- 2026-10-08: the *Upsert and the identity map* note was wrong that "the idempotency test catches
  this". The identity map is weak-referencing, so with `populate_existing` removed the whole suite
  still passed. Added `test_an_object_already_in_the_session_is_refreshed`, which holds the first
  object; it fails without the option. The frozen-brief test asserts `model_config` rather than
  assigning, because an assignment would need a type suppression.
