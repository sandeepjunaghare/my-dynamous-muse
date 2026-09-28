# Feature: T2 — Vertical manifest slice (the M9 lever)

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files.

> **Read this first.** `.claude/references/vertical-slice-architecture.md` is the right guide to *structure*
> and the **wrong guide to code** — it predates CLAUDE.md and its examples are sync SQLAlchemy 1.x with
> `Column(...)`, `declarative_base()` and `-> Any`. T1's report lists the traps; this ticket adds one more:
> **`Column(...)` produces untyped attributes and will not survive Pyright strict.** Use
> `Mapped[...]` / `mapped_column(...)`. See *Patterns to Follow*.

## Feature Description

The `vertical_manifest` is the abstraction the whole system turns on: **everything that differs between one
vertical and the next is a row in a table, never a branch in a slice.** A manifest declares, for one vertical,
the authoritative source(s) to search, the rules that disqualify a candidate, the signals that qualify one,
the ICP headcount band, and the vocabulary to speak in. Five generic pipeline stages read it; none of them
knows what "freight" means.

This ticket builds the schema, the table, the repository, the service, read-only routes, and the CLI that is
the manifest review surface — plus two seeded manifests (freight and fire) so that M9 ("vertical #2 in under a
founder-day") can be *proved* rather than asserted.

It also builds the lifecycle: a manifest is born `DRAFT`, and a **human** moves it to `ACTIVE` on the CLI while
recording the terms-of-use decision for every source it names. A source with no recorded decision cannot go
active. That is the gate; there is no second review surface and no frontend.

## User Story

As **a Compumatrice founder working a new vertical**
I want to **declare that vertical's sources, rules, signals, ICP band and vocabulary as data I review and
activate myself**
So that **adding vertical #2 is reviewing a row rather than writing a feature — and no source is ever hit
before someone decided its terms of use were acceptable**.

## Problem Statement

Nothing that distinguishes one vertical from another has a home yet. Today it lives in a founder's head, a
hand-written skip list, and four folders of playbook material (E7, E20). If the sourcing pipeline is built
before that home exists, the vertical leaks into the slices as `if vertical == "freight"` branches, M9 quietly
becomes impossible, and E10 repeats — a credible list of the wrong companies, produced because nobody had
established which registry was authoritative.

There is also a legal edge: FMCSA, Google Places and each state registry carry their own terms of use. Right
now nothing records whether anyone read them, and nothing can stop a run from hitting a source whose terms
were never considered.

## Solution Statement

One table, `vertical_manifest`, with **identity and lifecycle in real columns** (`vertical`, `version`,
`status`) and **the cited content in one JSONB `body`**, validated on the way in and out by a Pydantic v2
schema. Every researched field in the body is wrapped in the existing `ProvenancedValue[T]`, so a manifest
field nobody can cite cannot be represented.

The lifecycle is enforced in two places at once, by design:

- **In the database** — a partial unique index (`unique (vertical) where status = 'active'`) makes "at most one
  ACTIVE manifest per vertical" an invariant the application cannot forget.
- **In the service** — `activate()` refuses unless a terms-of-use decision has been recorded for **every**
  source the manifest declares, and refuses to activate anything that is not a DRAFT.

Terms-of-use decisions are stored **outside** the `ProvenancedValue` wrappers. A citation records what the
authoring agent *researched*; a terms decision records what a human *decided*. They have different provenance
and different lifetimes, and keeping the decision out of the cited value is what lets `activate` record it
without rewriting — and thereby invalidating — a citation. See *NOTES*.

The CLI (`lpe manifest list | show | activate`) is the review surface. Two manifests ship seeded as DRAFT.

## Out of Scope / Non-Goals

- **Not the authoring agent.** `lpe manifest propose` is **T12**. Do not add a stub, a placeholder subcommand,
  or an `app/manifests/agent.py`. Do not add the Claude Agent SDK dependency — `tests/test_structure.py`
  asserts it is absent until first use.
- **Not the rule *evaluator*.** T2 defines `DisqualifierRule` as a typed, declarative shape. **T7** builds the
  engine that runs it. Write no evaluation logic, no `matches()`, no scoring.
- **Not write routes.** Routes are read-only (AC8). Creation happens via T12; activation happens on the CLI.
  No `POST /manifests`.
- **Not a frontend.** Review is the CLI plus HubSpot (architecture → *Review surface*).
- **Not changing** `app/core/*`, `app/main.py`'s error handlers, or `app/shared/provenance.py` — with exactly
  one deliberate exception: adding the `manual_research` member to `RetrievalMethod` (see Task 2 and
  *Open Questions*).
- **Not activating the fire manifest.** Google Places' terms of use are an open question that explicitly
  blocks marking that source active. Fire ships DRAFT and stays DRAFT.
- **Not multi-metro, not a second geography.** Geography is a brief input (T4/T5), not a manifest field.

## Feature Metadata

**Feature Type**: New Capability (the first feature slice in the service)
**Estimated Complexity**: Medium-High — the code is not hard, but it sets three house precedents at once
(repository, slice layout, CLI) that T4, T5, T7, T11 and T12 will all copy
**Primary Systems Affected**: `app/manifests/` (new), `app/cli.py` (new), `app/main.py` (router mount),
`alembic/` (first domain tables), `tests/` (first database-backed tests)
**Dependencies**: **No new runtime or dev dependencies.** argparse is stdlib; JSONB comes from
`sqlalchemy.dialects.postgresql`, already installed

## Related Work

**Implements**: T2 in [`docs/tickets/local-prospect-engine.md`](../../docs/tickets/local-prospect-engine.md)
**Epic**: [`docs/local-prospect-engine.architecture.md`](../../docs/local-prospect-engine.architecture.md)
(the how) + [`docs/local-prospect-engine.prd.md`](../../docs/local-prospect-engine.prd.md) (intent)

**Back-references** (decisions inherited — do **not** reopen):

- `.claude/plans/t1-scaffold-core-provenance.md` — the `ProvenancedValue[T]` contract this slice consumes,
  the lazy-engine database pattern, and the "reference examples are dangerous here" table
- `docs/.../architecture.md` → *Vertical as a declarative manifest, not code*, *Data model — the shape*,
  *Boundaries & contracts* (FMCSA is a lookup API, not a search API), *Review surface: HubSpot, no frontend*
- `.claude/references/adding-a-vertical.md` — the whole file; it is this ticket's specification in prose
- `_tasks/todo.md` → D3 (authoring is agentic, human activates), D12 (CLI review surface, terms recorded at
  `activate`), D11 (hosted Supabase), and the Wave-2 prep notes (B1/B3 merge guards)

**Forward-references** (plans that extend this — append as follow-ups get created):

- (none yet — T4, T5, T7 and T12 all consume this slice and should be planned once it is implemented)

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `CLAUDE.md` (whole file) — Why: the ground rules. "Vertical is data, not code", zero suppressions, the
  `domain.component.action_state` logging convention, "never silently answer an open question"
- `.claude/references/adding-a-vertical.md` (whole file, ~40 lines) — Why: the prose spec for this ticket,
  including the exact three CLI invocations and the "a source without a terms decision cannot be active" rule
- `app/shared/provenance.py` (lines 1–120, and `_assert_immutable` at 26–68 especially) — Why: **every cited
  manifest field wraps in `ProvenancedValue[T]`, and `_assert_immutable` will reject any nested model that is
  not `frozen=True` and any `list`/`dict`/`set`.** This single constraint dictates the whole schema shape:
  tuples, not lists; frozen models, not plain ones
- `app/core/database.py` (lines 30–36 for `Base`, 66–72 for `get_db`) — Why: `class Base(DeclarativeBase)` is
  what models inherit; `get_db` is the FastAPI session dependency
- `app/core/exceptions.py` (whole file, 52 lines) — Why: slice exceptions derive from
  `LocalProspectEngineError` and override the `default_code` / `status_code` **ClassVars**; `CostLimitExceededError`
  (lines 36–52) is the worked example of an exception carrying structured detail
- `app/core/dependencies.py` (whole file, 28 lines) — Why: `DbSession` is the Annotated alias route handlers
  take; mirror the alias style rather than writing bare `Depends()` at each use site
- `app/main.py` (lines 1–5 and 56–61) — Why: the docstring says "Feature routers mount here as slices land.
  T1 mounts none" — this ticket is the first mount, so update that line too
- `app/core/config.py` (lines 40–47) — Why: `max_places_calls_per_run` already lives in config. Do **not** add
  a per-run cap to the manifest source schema; one fact, one home
- `alembic/env.py` (lines 30–33) — Why: `target_metadata = Base.metadata`, and **nothing imports any model
  yet**, so autogenerate currently sees an empty schema. See Task 4's GOTCHA
- `alembic/versions/0001_baseline.py` (whole file, 25 lines) — Why: the migration house style — module
  docstring explaining what the revision is for, typed `revision`/`down_revision`
- `tests/conftest.py` (whole file, 82 lines) — Why: `DATABASE_URL` is forced to a fake value at import, and the
  docstring explains why it must **not** be `setdefault`. Your new `TEST_DATABASE_URL` fixtures must respect
  that separation — the app's URL stays fake; the test database is a *different* variable
- `tests/test_structure.py` (lines 118–199) — Why: where the no-`if vertical ==` guard belongs, and the house
  precedent that structural guards are **AST-based, not grep-based** (grep fires on prose — see T1 report
  deviation #2)
- `tests/shared/test_provenance.py` (lines 1–45) — Why: the test house style — a docstring saying what the
  suite is load-bearing for, `model_validate` for deliberately invalid input (the typed constructor would be a
  type error), small `_fields()` / `_value()` builders
- `tests/core/test_config.py` (lines 26–37) — Why: the `env` fixture pattern for detaching `.env` so a test
  does not read whatever the developer happens to have
- `docs/tickets/local-prospect-engine.md` (the T2 section, and the *Decisions this breakdown rests on* table) —
  Why: the acceptance criteria this plan is derived from
- `.claude/references/backend-api-best-practices.md` (lines 7–13, 21–27, 49–55) — Why: routing shape, the
  centralized-error-handler rule, and "paginate all collection endpoints"

### New Files to Create

- `app/manifests/__init__.py` — package marker
- `app/manifests/schemas.py` — the Pydantic v2 manifest schema (the heart of this ticket)
- `app/manifests/models.py` — the `VerticalManifest` SQLAlchemy model
- `app/manifests/repository.py` — data access; no business rules, no commits
- `app/manifests/service.py` — the lifecycle rules, the terms-of-use gate, the transaction boundary
- `app/manifests/exceptions.py` — slice exceptions deriving from `LocalProspectEngineError`
- `app/manifests/routes.py` — read-only endpoints
- `app/manifests/cli.py` — the `manifest` subcommand group (argparse)
- `app/manifests/README.md` — what the slice is, the lifecycle diagram, how to add a vertical
- `app/cli.py` — the thin `lpe` entry point that delegates to slice subparsers
- `alembic/versions/0002_vertical_manifest.py` — table, constraints, indexes
- `alembic/versions/0003_seed_freight_and_fire_manifests.py` — two DRAFT rows
- `tests/manifests/__init__.py`, `tests/manifests/test_schemas.py`, `test_repository.py`, `test_service.py`,
  `test_routes.py`, `test_cli.py`, `test_seeds.py`

**Files to UPDATE:** `app/main.py` (mount the router, fix the docstring) · `app/shared/provenance.py` (one new
`RetrievalMethod` member) · `alembic/env.py` (import the models so autogenerate sees them) · `pyproject.toml`
(uncomment `[project.scripts]`) · `tests/conftest.py` (the database fixtures T4 and T11 will inherit) ·
`tests/test_structure.py` (the no-`if vertical ==` guard) · `CLAUDE.md` (mark the three `lpe` commands as
built, and `app/manifests/` as no longer "planned")

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [SQLAlchemy 2.0 — Declarative with `Mapped` / `mapped_column`](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html#declarative-table-with-mapped-column)
  - Why: the modern typed form. The project reference's `Column(...)` examples yield untyped attributes that
    Pyright strict rejects, with no suppression available
- [SQLAlchemy 2.0 — PostgreSQL JSONB](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#sqlalchemy.dialects.postgresql.JSONB)
  - Why: the `body` column. Note JSONB comparison/mutation semantics — we treat the column as opaque and let
    Pydantic own its shape
- [SQLAlchemy 2.0 — asyncio ORM](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
  - Specific section: `AsyncSession.execute` + `select()`; there is no `session.query()` in 2.0 style
  - Why: the reference's `db.query(Model).filter(...)` is 1.x sync and must not be copied
- [SQLAlchemy — joining an external transaction in tests](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites)
  - Why: the exact begin-connection / bind-session / rollback pattern the DB fixtures use so tests never leak
    rows into each other
- [Alembic — partial / conditional indexes](https://alembic.sqlalchemy.org/en/latest/ops.html#alembic.operations.Operations.create_index)
  - Specific section: `postgresql_where`
  - Why: `unique (vertical) where status='active'` is the invariant; autogenerate is unreliable for it, so the
    migration is hand-written
- [Pydantic v2 — generic models](https://docs.pydantic.dev/latest/concepts/models/#generic-models)
  - Why: `ProvenancedValue[ManifestSource]` must actually validate the parameter
- [Pydantic v2 — `model_dump(mode="json")`](https://docs.pydantic.dev/latest/concepts/serialization/#modelmodel_dump)
  - Why: **the single most likely way this ticket loses an hour.** Plain `model_dump()` leaves `datetime` and
    `UUID` as Python objects, which asyncpg cannot write into JSONB. See Gotchas
- [argparse — sub-commands](https://docs.python.org/3/library/argparse.html#sub-commands)
  - Why: `lpe manifest show|activate|list` is two levels of subparser; `required=True` on both
- [FMCSA — Company Census File / QCMobile](https://mobile.fmcsa.dot.gov/QCDevsite/docs/getStarted) and
  [SAFER company snapshot](https://safer.fmcsa.dot.gov/CompanySnapshot.aspx)
  - Why: the freight seed's `base_url` and citations must point at something real. QCMobile is a **lookup** API
    keyed on USDOT/MC, not a search-by-geography API — the census file is where geography comes from
- [Texas State Fire Marshal — licensed contractor search](https://www.tdi.texas.gov/fire/fmlicense.html)
  - Why: the fire seed's registry source

### Patterns to Follow

**Slice layout** — mirror `vertical-slice-architecture.md` Rule 3 (`routes` → `service` → `repository` →
models/schemas/exceptions + a README), but **tests live in `tests/manifests/`, not colocated**. The reference
colocates; T1 established `tests/core/` and `tests/shared/` mirroring `app/`, and the ticket names
`tests/manifests/`. Follow the repo, not the article.

**Logging — `domain.component.action_state`, four segments of meaning in three dots:**

```python
logger.info("manifests.service.activation_started", manifest_id=str(manifest_id), vertical=vertical)
logger.info("manifests.service.activation_succeeded", vertical=vertical, version=version)
logger.warning("manifests.service.activation_refused", vertical=vertical, missing_terms=sorted(missing))
```

`tests/core/test_logging.py` asserts **every** log event in `app/` matches this shape — a three-segment event
name fails the existing suite.

**Exceptions — derive, and override the ClassVars:**

```python
class ManifestError(LocalProspectEngineError):
    """Base for every deliberate failure in the manifests slice."""
    default_code: ClassVar[str] = "manifest_error"


class TermsOfUseNotRecordedError(ManifestError):
    default_code: ClassVar[str] = "terms_of_use_not_recorded"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, vertical: str, missing_sources: tuple[str, ...]) -> None:
        super().__init__(message)
        self.vertical = vertical
        self.missing_sources = missing_sources
```

Mirror `CostLimitExceededError` (`app/core/exceptions.py:36-52`) for carrying structured detail. `app/main.py`
already renders any `LocalProspectEngineError` as structured JSON — **do not add try/except to route handlers.**

**Models — typed declarative, never `Column(...)`:**

```python
class VerticalManifest(Base):
    __tablename__ = "vertical_manifest"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
```

`Mapped[dict[str, object]]` — **not** `dict[str, Any]`. `tests/test_structure.py` fails the build on any `Any`
annotation in `app/`, checked through the AST.

**Repository owns queries; the service owns the transaction.** The repository `flush()`es and never `commit()`s;
`ManifestService` commits once per operation. T2 sets this precedent for T4, T9 and T11 — state it in the
slice README so the next slice does not invent a second convention.

**Naming:** snake_case modules/functions, PascalCase classes. Source identifiers are stable lowercase slugs
(`fmcsa`, `places`, `tx_fire_marshal`) because `--accept-terms fmcsa,places` types them by hand.

---

## IMPLEMENTATION PLAN

### Phase 1: The schema

The shape everything else serialises to. Pure Pydantic, no database, no I/O — and the phase where the
`ProvenancedValue` immutability constraint has to be respected or nothing downstream works.

**Tasks:** define the enums, the frozen leaf models, `ManifestBody`, and the one new `RetrievalMethod` member.

### Phase 2: Persistence

**Depends on:** Phase 1 (the body model is what the repository serialises).

**Tasks:** the model, the hand-written migration with the partial unique index, the `env.py` import that makes
autogenerate see the table, and the repository.

### Phase 3: Lifecycle and exposure

**Depends on:** Phase 2.

**Tasks:** exceptions, service (the terms gate and the supersede rule), read-only routes, router mount.

### Phase 4: The review surface

**Depends on:** Phase 3 (the CLI calls the service).
**Independent of:** Phase 5's seed content — the CLI can be built and tested against a manifest made in a test.

**Tasks:** `app/cli.py`, `app/manifests/cli.py`, the `[project.scripts]` entry point.

### Phase 5: Seeds

**Depends on:** Phase 2 (the table must exist).

**Tasks:** the freight and fire DRAFT rows, both fully cited, in a data migration.

### Phase 6: Tests, guards and docs

**Depends on:** everything above.

**Tasks:** the database fixtures T4/T11 inherit, the six test modules, the no-`if vertical ==` structural
guard, the slice README, and the CLAUDE.md corrections.

---

## STEP-BY-STEP TASKS

Execute in order, top to bottom. Each task is atomic and independently testable.

### CREATE `app/manifests/schemas.py`

- **IMPLEMENT**: the full manifest schema.
  - `SourceKind(StrEnum)`: `registry_api` · `bulk_file` · `web_lookup`. Deliberately parallel to
    `RetrievalMethod`'s first three members — the stage that pulls a source records the matching method.
  - `RuleKind(StrEnum)`: `predicate` (mechanical, free, evaluated by T7's engine) · `judgment` (delegated to
    the `classify_rollup` Agent SDK node). **"National rollup with no local owner" is a `judgment` rule** —
    that is the whole reason the node exists.
  - `RuleOperator(StrEnum)`: `equals` · `not_equals` · `contains` · `in_set` · `greater_than` · `less_than` ·
    `is_true`. Keep it small; T7 may add members, which is a one-line change and not a schema break.
  - `ScoreAxis(StrEnum)`: `intensity` · `automatable` — E9's 1–5 × 1–5 = 1–25.
  - `TermsDecision(StrEnum)`: `accepted` · `rejected`. **Undecided is the absence of a record, not a third
    member** — a `None` cannot be mistaken for a decision.
  - Frozen leaf models, every one `model_config = ConfigDict(frozen=True)`:
    - `ManifestSource`: `name` (slug, `pattern=r"^[a-z][a-z0-9_]*$"`) · `kind` · `base_url` ·
      `rate_limit_per_minute: int | None`. **No terms field. No per-run cap** — that is
      `settings.max_places_calls_per_run`.
    - `DisqualifierRule`: `id` (slug) · `kind: RuleKind` · `description` · `field: str | None` ·
      `operator: RuleOperator | None` · `value: str | tuple[str, ...] | int | bool | None`. A model validator
      requires `field` and `operator` when `kind is predicate`, and requires both to be absent when it is
      `judgment`.
    - `QualifyingSignal`: `id` · `question` (the literal thing to ask, e.g. "which TMS, and does it have an
      API") · `feeds: ScoreAxis`.
    - `IcpBand`: `headcount_min: int` · `headcount_max: int` · `requires_office_function: bool`. Validator:
      `1 <= min <= max`.
    - `Vocabulary`: `terms: tuple[str, ...]`, non-empty.
    - `SourceTerms`: `source_name` · `decision: TermsDecision` · `license: str | None` · `decided_by: str` ·
      `decided_at: datetime` (tz-aware, normalised to UTC — mirror provenance's validator) · `note: str | None`.
  - `ManifestBody(BaseModel, frozen=True)`:
    ```python
    sources: tuple[ProvenancedValue[ManifestSource], ...]
    disqualifier_rules: tuple[ProvenancedValue[DisqualifierRule], ...]
    qualifying_signals: tuple[ProvenancedValue[QualifyingSignal], ...]
    icp_band: ProvenancedValue[IcpBand]
    vocabulary: ProvenancedValue[Vocabulary]
    terms: tuple[SourceTerms, ...] = ()
    ```
    Model validators: `sources` non-empty; source names unique; rule ids unique; **every `terms.source_name`
    matches a declared source** (no orphan decisions); `terms` has at most one entry per source.
  - `ManifestStatus(StrEnum)`: `draft` · `active` · `superseded`.
  - `ManifestResponse(BaseModel)`: the API shape — `id`, `vertical`, `version`, `status`, `body`,
    `created_at`, `activated_at`, `activated_by`. `model_config = ConfigDict(from_attributes=True)`.
  - Helper on the body (pure, no I/O): `undecided_sources(self) -> tuple[str, ...]` — declared source names
    with no `accepted` entry in `terms`. The service and the CLI both need exactly this.
- **PATTERN**: `app/shared/provenance.py` — the frozen-model + StrEnum-with-docstrings house style; every enum
  member carries a one-line docstring saying what it is for.
- **IMPORTS**: `from app.shared.provenance import ProvenancedValue`; `from enum import StrEnum`;
  `from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator`.
- **GOTCHA**: `ProvenancedValue._assert_immutable` walks the wrapped value and **raises on any `list`, `dict`,
  `set`, or non-frozen `BaseModel`**. Therefore: every collection inside a cited value is a `tuple`, every
  nested model sets `frozen=True`. A `list[str]` inside `Vocabulary` will fail at construction with a message
  naming the field — that is the primitive working, not a bug to route around.
- **GOTCHA**: do not put `terms` inside `ManifestSource`. Recording a terms decision would then mutate a value
  whose citation says when it was retrieved, and `ProvenancedValue` is frozen precisely to stop that.
- **VALIDATE**: `uv run ruff check app/manifests/schemas.py && uv run mypy . && uv run pyright`
- **SATISFIES**: AC1, AC2, AC10

### UPDATE `app/shared/provenance.py`

- **IMPLEMENT**: add one member to `RetrievalMethod`:
  ```python
  manual_research = "manual_research"
  """A person read a public source and recorded the finding by hand.

  What the seeded freight and fire manifests carry: a human read FMCSA's and the Texas Fire Marshal's
  documentation and wrote the row. T12's authoring agent cites `llm_inference` instead. Marking
  hand-authored data honestly is the same job as certifying retrieved data — see `manual_hubspot_entry`.
  """
  ```
- **PATTERN**: `manual_hubspot_entry` (same file) — the existing precedent for "a human did this, and the data
  says so".
- **GOTCHA**: this is the **only** permitted edit to a T1 file other than `main.py`. It is also the one line in
  this ticket that T3's branch could conceivably touch; if the merge conflicts, both sides are adding enum
  members and the resolution is to keep both.
- **GOTCHA**: do not reuse `web_lookup` for the seeds. It is defined as "a paid or public **per-request**
  web/API lookup", which a hand-written row is not.
- **VALIDATE**: `uv run pytest tests/shared/test_provenance.py`
- **SATISFIES**: AC10

### CREATE `app/manifests/models.py`

- **IMPLEMENT**: `VerticalManifest(Base)` on table `vertical_manifest`:
  `id` (UUID pk, `default=uuid4`) · `vertical` (String(64)) · `version` (Integer) · `status` (String(16)) ·
  `body` (JSONB) · `created_at` (timestamptz, `server_default=func.now()`) · `activated_at`
  (timestamptz, nullable) · `activated_by` (String(128), nullable).
  `__table_args__`: `UniqueConstraint("vertical", "version", name="uq_vertical_manifest_vertical_version")`,
  plus an index on `vertical`.
- **PATTERN**: `app/core/database.py:33` for `Base`; SQLAlchemy 2.0 typed declarative (see *Patterns*).
- **IMPORTS**: `from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID`;
  `from sqlalchemy.orm import Mapped, mapped_column`.
- **GOTCHA**: the partial unique index lives in the **migration**, not in `__table_args__` — expressing it in
  the model risks autogenerate trying to "fix" it on a later ticket. Add a comment in the model saying where it
  lives and why.
- **GOTCHA**: `Mapped[dict[str, object]]`, never `dict[str, Any]`.
- **VALIDATE**: `uv run mypy . && uv run pyright`
- **SATISFIES**: AC3

### UPDATE `alembic/env.py`

- **IMPLEMENT**: import the slice's models so their tables are registered on `Base.metadata`:
  ```python
  from app.manifests import models as manifest_models

  # Importing a slice's models is what puts its tables on Base.metadata. The tuple keeps the import
  # alive: ruff would otherwise remove it as unused, and autogenerate would silently see an empty
  # schema and propose dropping every table. Each new slice appends here.
  _REGISTERED_MODELS = (manifest_models.VerticalManifest,)
  ```
- **GOTCHA**: do **not** reach for `# noqa: F401`. This project's ethos is zero suppressions; the bound tuple
  is self-documenting and lints clean.
- **GOTCHA**: this file is edited by every slice that adds a table (T4, T11). Keep the edit to one import plus
  one tuple entry so a parallel branch conflicts trivially.
- **VALIDATE**: `uv run alembic check` (against the throwaway Postgres below) — should report no pending changes
  once the migration exists
- **SATISFIES**: AC3

### CREATE `alembic/versions/0002_vertical_manifest.py`

- **IMPLEMENT**: `down_revision = "0001_baseline"`. Create the table as above, then:
  ```python
  op.create_index(
      "uq_vertical_manifest_one_active_per_vertical",
      "vertical_manifest",
      ["vertical"],
      unique=True,
      postgresql_where=sa.text("status = 'active'"),
  )
  ```
  Also a plain index on `vertical`. `downgrade()` drops the indexes then the table.
- **PATTERN**: `alembic/versions/0001_baseline.py` — module docstring saying what the revision is for and which
  ticket needs it; typed revision identifiers.
- **GOTCHA**: **hand-write this migration; do not trust `--autogenerate` for the partial index.** Autogenerate
  routinely omits `postgresql_where`, and the invariant is the point of the ticket.
- **GOTCHA**: the single-head guard (`tests/test_structure.py::test_migrations_have_exactly_one_head`) exists
  because of exactly this wave. If T4's branch later forks from `0001`, the fix is to rebase its
  `down_revision`, per `.claude/references/conventions.md` → *merge*.
- **VALIDATE**: `TEST_DATABASE_URL` container up, then
  `DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head && DATABASE_URL=$TEST_DATABASE_URL uv run alembic downgrade base && DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head`
- **SATISFIES**: AC3, AC4

### CREATE `app/manifests/exceptions.py`

- **IMPLEMENT**: `ManifestError` base plus `ManifestNotFoundError` (404),
  `ActiveManifestNotFoundError` (404), `ManifestNotDraftError` (409),
  `TermsOfUseNotRecordedError` (409, carries `vertical` + `missing_sources`),
  `UnknownSourceError` (422, carries the unknown names).
- **PATTERN**: `app/core/exceptions.py` — ClassVar `default_code` / `status_code`, structured detail in
  `__init__` like `CostLimitExceededError`.
- **GOTCHA**: `ActiveManifestNotFoundError` is separate from `ManifestNotFoundError` on purpose. T5 asking for
  an active manifest and finding only drafts is a different, more interesting failure than a bad id, and the
  run must fail loudly rather than source against nothing.
- **VALIDATE**: `uv run pytest tests/manifests/ -k exception`
- **SATISFIES**: AC7

### CREATE `app/manifests/repository.py`

- **IMPLEMENT**: `ManifestRepository`, constructed with an `AsyncSession`:
  - `async get_by_id(manifest_id: UUID) -> VerticalManifest | None`
  - `async get(vertical: str, version: int) -> VerticalManifest | None`
  - `async get_active(vertical: str) -> VerticalManifest | None` — **filters `status == active` only; a DRAFT
    row is never returned**
  - `async list(vertical: str | None, status: ManifestStatus | None, limit: int, offset: int) -> Sequence[VerticalManifest]`
    — ordered by `vertical, version desc` for stable output
  - `async next_version(vertical: str) -> int` — `max(version) + 1`, or 1
  - `async create_draft(vertical: str, body: ManifestBody) -> VerticalManifest`
  - `async mark_superseded(manifest: VerticalManifest) -> None`
  - `async mark_active(manifest: VerticalManifest, body: ManifestBody, actor: str) -> None`
- **PATTERN**: SQLAlchemy 2.0 `select()` + `await session.execute(...)`, `.scalar_one_or_none()` /
  `.scalars().all()`. **Not** `session.query(...)`.
- **IMPORTS**: `from sqlalchemy import select`; `from sqlalchemy.ext.asyncio import AsyncSession`.
- **GOTCHA**: serialise with `body.model_dump(mode="json")`. Plain `model_dump()` leaves `datetime` objects,
  and asyncpg raises `DataError: invalid input for query argument` when they reach JSONB. Deserialise with
  `ManifestBody.model_validate(row.body)`.
- **GOTCHA**: the repository **flushes, never commits**. The service owns the transaction.
- **VALIDATE**: `TEST_DATABASE_URL=... uv run pytest tests/manifests/test_repository.py`
- **SATISFIES**: AC5, AC6

### CREATE `app/manifests/service.py`

- **IMPLEMENT**: `ManifestService(session)`, holding the rules:
  - `async get_active(vertical) -> ManifestResponse` — raises `ActiveManifestNotFoundError`
  - `async get(manifest_id) -> ManifestResponse` — raises `ManifestNotFoundError`
  - `async list(...) -> list[ManifestResponse]`
  - `async create_draft(vertical, body) -> ManifestResponse` — assigns `next_version`, commits
  - `async activate(manifest_id, accept_terms: frozenset[str], actor: str) -> ManifestResponse`, in this order:
    1. load, or `ManifestNotFoundError`
    2. status must be `draft`, or `ManifestNotDraftError` (names the current status)
    3. `unknown = accept_terms - declared_source_names` → `UnknownSourceError`
    4. `missing = declared_source_names - accept_terms` → `TermsOfUseNotRecordedError(missing_sources=...)`
    5. build the new body: the cited parts copied **verbatim**, `terms` replaced with one accepted
       `SourceTerms` per declared source (`decided_by=actor`, `decided_at=now(UTC)`,
       `license` carried from the source's known licence when the caller supplies one, else `None`)
    6. supersede the current active row for that vertical, if any
    7. mark active, commit
- **PATTERN**: `vertical-slice-architecture.md` Step 5 for the service shape — but async, and with the
  `logger.info(...started)` / `...succeeded` / `logger.warning(...refused)` event trio.
- **GOTCHA**: steps 6 and 7 must be in **one transaction**. Superseding the old row and activating the new one
  in two commits leaves a window with no active manifest for that vertical, and a crash between them leaves
  none at all. The partial unique index also means the supersede must land *before* the activate or Postgres
  rejects the second active row.
- **GOTCHA**: `activate` must not bump the version. A version bump is how a manifest *changes*; activation is
  how a version is *accepted*.
- **VALIDATE**: `TEST_DATABASE_URL=... uv run pytest tests/manifests/test_service.py`
- **SATISFIES**: AC5, AC6, AC7

### CREATE `app/manifests/routes.py` + UPDATE `app/main.py`

- **IMPLEMENT**: `router = APIRouter(prefix="/manifests", tags=["manifests"])` with three read-only endpoints:
  - `GET /manifests` — `vertical` and `status` query filters, `limit` (default 50, le 100) and `offset`
  - `GET /manifests/active/{vertical}` — the active manifest, 404 when there is none
  - `GET /manifests/{manifest_id}` — by UUID
  Then `app.include_router(manifests_router)` in `main.py`, and update the module docstring's "T1 mounts none"
  line to say what now mounts.
- **PATTERN**: `app/core/dependencies.py` — take `DbSession`, not a bare `Depends()`. A thin
  `get_manifest_service(db: DbSession) -> ManifestService` dependency, then an `Annotated` alias.
- **GOTCHA**: **no try/except in handlers.** `app/main.py`'s `LocalProspectEngineError` handler already renders
  slice exceptions as structured JSON with the right status code. Catching them locally would produce a second,
  inconsistent error shape — the anti-pattern `backend-api-best-practices.md:23` names.
- **GOTCHA**: order matters only if both paths are one segment deep; `active/{vertical}` is two, so there is no
  collision with `{manifest_id}`. Keep `manifest_id` typed as `UUID` so a non-UUID path 422s rather than 500s.
- **VALIDATE**: `uv run pytest tests/manifests/test_routes.py` and
  `curl -s localhost:8000/manifests | head`
- **SATISFIES**: AC8

### CREATE `app/cli.py` + `app/manifests/cli.py` + UPDATE `pyproject.toml`

- **IMPLEMENT**:
  - `app/manifests/cli.py`: `register(subparsers) -> None` adding the `manifest` group with `list`, `show`,
    `activate`; plus the async handlers and the rendering.
    - `lpe manifest list [--vertical V]` — a table: id, vertical, version, status, source names.
    - `lpe manifest show <id>` — **every field with its citation**: for each source/rule/signal/icp/vocabulary
      entry, print the value and then its `source_url`, `retrieved_at`, `retrieval_method`; then a
      `terms of use:` block per declared source reading `accepted by X on DATE` or **`UNDECIDED — blocks
      activation`**.
    - `lpe manifest activate <id> --accept-terms a,b` — split on comma, strip, drop empties, pass as a
      `frozenset`. On success print the vertical, version and what it superseded.
  - `app/cli.py`: `main(argv: Sequence[str] | None = None) -> int` — the root parser, `prog="lpe"`, delegating
    to `app.manifests.cli.register`. Wrap the dispatch so a `LocalProspectEngineError` prints
    `error: <message>` to stderr and returns **1**, never a traceback. argparse's own usage errors exit **2**.
  - `pyproject.toml`: uncomment `[project.scripts]` / `lpe = "app.cli:main"` and delete the "T2 adds..." comment.
- **PATTERN**: the three invocations in `.claude/references/adding-a-vertical.md` are the contract — match them
  exactly, including `--accept-terms fmcsa,places`.
- **IMPORTS**: `argparse`, `asyncio`, `from app.core.database import get_sessionmaker`.
- **GOTCHA**: the handlers are async; the CLI is not. Use one `asyncio.run(...)` at the dispatch boundary, and
  `async with get_sessionmaker()() as session:` inside. Call `await dispose_engine()` before returning so the
  process does not exit with a live pool.
- **GOTCHA**: `main` must **return** an exit code, not call `sys.exit`, so tests can assert on it. The
  `[project.scripts]` wrapper turns the return value into the process exit status for free.
- **GOTCHA**: do **not** add a `propose` subcommand, even raising `NotImplementedError`. T12 owns it, and an
  empty command in the help text reads as a broken feature.
- **VALIDATE**: `uv sync && uv run lpe manifest --help && uv run pytest tests/manifests/test_cli.py`
- **SATISFIES**: AC9, AC11

### CREATE `alembic/versions/0003_seed_freight_and_fire_manifests.py`

- **IMPLEMENT**: `down_revision = "0002_vertical_manifest"`. Insert two rows, **both `status="draft"`**, with
  fixed UUID and timestamp literals so the migration is deterministic and re-runnable in any environment.
  - **freight** (`vertical="freight"`, version 1): source `fmcsa` (`kind=bulk_file`,
    `base_url="https://safer.fmcsa.dot.gov/"`, with the census-file/QCMobile split recorded in the
    description); disqualifier rules — `asset_based_carrier` (predicate) and `double_brokering_risk`
    (judgment); qualifying signal — "which TMS, and does it have an API" (`feeds=automatable`); ICP band
    20–200, office function required; vocabulary — loads, lanes, carrier packets, COIs, check calls, detention.
  - **fire** (`vertical="fire"`, version 1): sources `tx_fire_marshal` (`registry_api`) **and** `places`
    (`web_lookup`); disqualifier rule — `national_rollup_no_local_owner` (**judgment** — E7's four known
    rollups are what it must catch); qualifying signal — owner-accessible; ICP band 20–200 office function;
    vocabulary — inspections, permits, AHJ, renewals.
  - Every cited field carries `retrieval_method="manual_research"`, a real `source_url`, and the fixed
    `retrieved_at`.
  - `downgrade()` deletes exactly these two rows by `(vertical, version)`.
- **PATTERN**: build the bodies with the Pydantic schema, then `model_dump(mode="json")` — **do not hand-write
  the JSON literal.** A hand-written blob drifts from the schema silently; constructing it means the migration
  fails loudly the day the schema changes.
- **GOTCHA**: **neither row is activated.** Fire names Google Places, whose terms of use are an open question
  that explicitly blocks marking it active; freight is activated by a person on the CLI, because making the
  migration a second path to ACTIVE would bypass the very gate this ticket builds.
- **GOTCHA**: `alembic/versions/` is excluded from ruff and mypy (`pyproject.toml`), so this file is not
  type-checked. Keep it simple and import the schema rather than being clever.
- **VALIDATE**: `DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head` then
  `uv run pytest tests/manifests/test_seeds.py`
- **SATISFIES**: AC4, AC12

### UPDATE `tests/conftest.py` — the database fixtures T4 and T11 inherit

- **IMPLEMENT**:
  - `TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")` and
    `requires_db = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="set TEST_DATABASE_URL — see tests/README or the plan; docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=x postgres:16")`
  - a session-scoped engine bound to `TEST_DATABASE_URL` that runs `alembic upgrade head` once
  - a function-scoped `db_session` that opens a connection, begins a transaction, binds an `AsyncSession` to
    it, yields, then **rolls back** — so no test leaks rows into the next
  - a `client_with_db` fixture that sets `app.dependency_overrides[get_db]` to yield that same session
- **PATTERN**: the SQLAlchemy "joining a session into an external transaction" recipe (linked above).
- **GOTCHA**: `TEST_DATABASE_URL` is a **different variable** from `DATABASE_URL`, which this file deliberately
  forces to a fake value at import. Do not change that line — its docstring explains that a real URL leaking in
  from a worktree's `.env` is how a test suite ends up writing to real Supabase.
- **GOTCHA**: the skip reason must name the exact `docker run` command. A skip nobody knows how to un-skip is a
  test that silently never runs.
- **VALIDATE**: `uv run pytest` (skips cleanly) **and** `TEST_DATABASE_URL=... uv run pytest` (runs)
- **SATISFIES**: AC13

### CREATE the six test modules under `tests/manifests/`

- **IMPLEMENT**:
  - `test_schemas.py` (no DB) — a source without a citation cannot be constructed; a `list` inside a cited
    value is rejected by `_assert_immutable`; a non-frozen nested model is rejected; `icp_band` min > max
    rejected; duplicate source names rejected; a `terms` entry naming an undeclared source rejected; a
    `predicate` rule missing `operator` rejected; a `judgment` rule carrying one rejected;
    `undecided_sources()` returns the right names; **`model_dump(mode="json")` → `model_validate` round-trips
    with `retrieved_at` still tz-aware and equal**.
  - `test_repository.py` (`requires_db`) — `create_draft` assigns version 1 then 2; **`get_active` returns
    `None` while only drafts exist** (the ticket's named test); `get` finds a specific version; `list` ordering
    is stable; the `(vertical, version)` unique constraint raises `IntegrityError`; **two ACTIVE rows for one
    vertical raise `IntegrityError`** — proving the invariant is in the database, not just the service.
  - `test_service.py` (`requires_db`) — **activating with a source whose terms are unrecorded raises
    `TermsOfUseNotRecordedError` and names the missing source** (the ticket's named test); `--accept-terms`
    naming an unknown source raises `UnknownSourceError`; activating an already-active row raises
    `ManifestNotDraftError`; a successful activate supersedes the previous version and leaves exactly one
    active; the cited fields are **byte-identical** before and after activation.
  - `test_routes.py` (`requires_db`) — `GET /manifests` shape and filters; `GET /manifests/active/{vertical}`
    404 with the structured error body when only drafts exist; a non-UUID `manifest_id` gives 422.
  - `test_cli.py` — `show` prints every citation's `source_url` and prints `UNDECIDED` for a source with no
    terms (`requires_db`); `activate` without `--accept-terms` exits 2; `activate` with a missing source exits
    1 and prints `error:` with no traceback; the happy path flips the status.
  - `test_seeds.py` (`requires_db`) — after `upgrade head`, exactly two manifests exist, **both DRAFT**;
    freight names `fmcsa`; fire names `tx_fire_marshal` and `places`; **fire cannot be activated with
    `--accept-terms tx_fire_marshal` alone** (Places is still undecided).
- **PATTERN**: `tests/shared/test_provenance.py` — a module docstring saying what the suite is load-bearing
  for, classes grouping behaviour, small builders rather than fixtures-for-everything.
- **GOTCHA**: build deliberately-invalid schema input through `model_validate`, not the typed constructor —
  the constructor would be a static type error and this project allows no suppression to silence it. The
  provenance suite's docstring explains this precedent.
- **GOTCHA**: repository/service tests must use throwaway vertical names (e.g. `f"test_{uuid4().hex[:8]}"`),
  never `freight` or `fire`. Reusing a seeded vertical couples these tests to the seed migration's content.
- **VALIDATE**: `TEST_DATABASE_URL=... uv run pytest tests/manifests/ -v`
- **SATISFIES**: AC5, AC6, AC7, AC8, AC9, AC12, AC13

### UPDATE `tests/test_structure.py` — the no-`if vertical ==` guard

- **IMPLEMENT**: `test_no_branch_on_the_vertical_name` — walk every `app/**/*.py` AST for `ast.Compare` and
  `ast.match_case` nodes whose subject is the name `vertical` or an attribute `.vertical`, and whose other side
  is a **string literal or a literal collection of strings**. Any hit fails, naming `file:line`.
- **PATTERN**: `test_no_any_annotations` in the same file — AST, not grep, and the docstring explains *why*
  (T1's report, deviation #2: a textual match fires on this project's own prose about the rule).
- **GOTCHA**: comparing two variables (`if manifest.vertical == vertical:`) is legitimate and must **not** fire.
  Only a comparison against a literal is the thing the rule forbids.
- **GOTCHA**: append a new test — do not restructure the file. Wave-2 prep deliberately rewrote one test here
  to stop parallel branches conflicting on the same line; keep that property.
- **VALIDATE**: `uv run pytest tests/test_structure.py -v`, then **prove it fails**: temporarily add
  `if vertical == "freight": pass` to a module in `app/`, confirm red, remove it.
- **SATISFIES**: AC10

### CREATE `app/manifests/README.md` + UPDATE `CLAUDE.md`

- **IMPLEMENT**: the slice README — what a manifest is, the `DRAFT → ACTIVE → SUPERSEDED` lifecycle with the
  terms gate, the three CLI commands, where the invariant lives (the partial index), the repository/service
  transaction convention, and a pointer to `.claude/references/adding-a-vertical.md`.
  In `CLAUDE.md`: move `app/manifests/` from *Planned* into the built map, and change the "**T2 builds these;
  they do not exist yet**" line above the `lpe` commands to say they exist — **but note `propose` is still T12**.
- **PATTERN**: `vertical-slice-architecture.md` Step 8 for README shape; `CLAUDE.md`'s existing terse register.
- **GOTCHA**: `CLAUDE.md` is the one file most likely to conflict with T3's branch, which will make the same
  kind of edit for `promotion/`. Keep the diff to the two lines that must change.
- **VALIDATE**: `uv run pytest` (full suite) — docs do not break tests, but this is the last gate before commit
- **SATISFIES**: AC11

### RUN full validation

- **IMPLEMENT**: the whole suite, both halves, then the manual walk below.
- **VALIDATE**: `/piv-validate`, then `TEST_DATABASE_URL=... uv run pytest`
- **SATISFIES**: AC14

---

## TESTING STRATEGY

### Unit Tests

No database: `test_schemas.py` and the structural guard. These carry the load — the schema is where the
immutability and citation constraints either hold or silently don't, and they run on every `uv run pytest`
with no infrastructure.

### Integration Tests

`requires_db`, against a throwaway Postgres 16 container, each test wrapped in a transaction that is rolled
back. The migration chain is applied once per session, so the seed rows are real and `test_seeds.py` is testing
the actual migration rather than a fixture that imitates it.

```bash
docker run --rm -d --name lpe-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres
uv run pytest
docker stop lpe-test-pg
```

### Edge Cases

- A manifest with **zero** sources — rejected at the schema, not at activation
- `--accept-terms` with trailing commas, whitespace, duplicates, or empty segments
- `--accept-terms` naming a source the manifest does not declare
- Activating a manifest that is already `active`, and one that is `superseded`
- Two activations racing for one vertical — the partial unique index must be what refuses the second
- A vertical with no manifest at all (`get_active` → `ActiveManifestNotFoundError`)
- A `retrieved_at` that survives the JSONB round-trip still tz-aware and still UTC
- A body whose `terms` names a source that was removed in a later version
- `lpe manifest show` on an id that does not exist → exit 1, one readable line, no traceback

---

## VALIDATION COMMANDS

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

### Level 3: Tests

```bash
uv run pytest                                   # DB tests skip, and say how to un-skip
TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres uv run pytest
```

### Level 4: Manual Validation

```bash
# migrations round-trip
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
DATABASE_URL=$TEST_DATABASE_URL uv run alembic downgrade base
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head

# the review surface, end to end
uv run lpe manifest list
uv run lpe manifest show <freight-id>            # every citation renders; terms show UNDECIDED
uv run lpe manifest activate <freight-id> --accept-terms fmcsa
uv run lpe manifest show <freight-id>            # now ACTIVE, terms accepted, citations unchanged
uv run lpe manifest activate <fire-id> --accept-terms tx_fire_marshal   # MUST refuse: places undecided

# the API
uv run uvicorn app.main:app --port 8000 &
curl -s localhost:8000/manifests | jq
curl -s localhost:8000/manifests/active/freight | jq
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/manifests/active/fire   # 404
```

### Level 5: Everything at once

```bash
/piv-validate
```

---

## ACCEPTANCE CRITERIA

- [ ] **AC1** — The manifest schema exists with `sources[]` (name · kind · base_url · rate limits),
      `disqualifier_rules[]`, `qualifying_signals[]`, `icp_band` (headcount min/max + office-function flag),
      `vocabulary`, `version` and `status`
- [ ] **AC2** — Every researched field is wrapped in `ProvenancedValue[T]`; a manifest field cannot exist
      without a source URL, timestamp and retrieval method
- [ ] **AC3** — `vertical_manifest` table + migration, with identity/lifecycle in columns and the cited body in
      JSONB; `alembic upgrade head` / `downgrade base` round-trips
- [ ] **AC4** — At most one ACTIVE manifest per vertical, enforced by a **database** partial unique index, not
      only by application code
- [ ] **AC5** — Repository provides `get_active(vertical)`, `get(vertical, version)`, `create_draft`,
      `activate`; a **DRAFT row is never returned by `get_active`** *(ticket-named test)*
- [ ] **AC6** — Versioning: a bump writes a new row and the old row is retained, not overwritten
- [ ] **AC7** — **A source with no recorded terms-of-use decision cannot be marked active** *(ticket-named
      test)*, and the refusal names which sources are missing
- [ ] **AC8** — Read-only routes list manifests, fetch one by id, and fetch the active one for a vertical;
      errors render through the existing centralized handler
- [ ] **AC9** — `lpe manifest show <id>` renders a draft with **every citation**;
      `lpe manifest activate <id> --accept-terms <sources>` records the per-source decision and flips the row
      active
- [ ] **AC10** — A test asserts there is **no `if vertical ==` branch anywhere in `app/`**, AST-checked
- [ ] **AC11** — `lpe` is a working entry point in `pyproject.toml`; `CLAUDE.md` and the slice README describe
      what now exists
- [ ] **AC12** — Freight and fire are both seeded, fully cited, and **both DRAFT**; fire cannot be activated
      while the Google Places terms decision is unrecorded
- [ ] **AC13** — Database-backed tests run against a real Postgres and skip with an actionable message when
      `TEST_DATABASE_URL` is unset
- [ ] **AC14** — `ruff`, `mypy`, `pyright` and `pytest` all green, **zero suppressions**, no new dependencies

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully, both with and without `TEST_DATABASE_URL`
- [ ] The structural guard was proven by making it fail, then passing
- [ ] No linting or type checking errors; no `# type: ignore`, no `Any`, no `# noqa`
- [ ] Manual CLI walk confirms the terms gate refuses fire and accepts freight
- [ ] Acceptance criteria all met
- [ ] `alembic/versions/` still has exactly one head

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decided in session (2026-09-27), before implementation:**

1. **Storage is hybrid** — identity and lifecycle in columns, cited content in one JSONB `body`. Chosen over
   full normalisation because the manifest is always read whole, nothing queries inside the rules, and a
   version is an immutable snapshot; chosen over a single blob because "one ACTIVE per vertical" deserves to be
   a database constraint.
2. **CLI is stdlib `argparse`** — no new dependency, and typed in typeshed so it survives Pyright strict.
3. **Test database is a throwaway Postgres via `TEST_DATABASE_URL`, skipping when unset.** Matches what T1 did
   for its migration round-trip and the per-worktree rule already recorded in `_tasks/todo.md`.
4. **Both seeds land DRAFT**; freight is activated by a human on the CLI. This keeps `activate` the only path
   to ACTIVE and answers no open question on anyone's behalf.

**Assumptions — flag rather than silently diverge if any is wrong:**

5. **`superseded` is a third status.** The ticket names only `DRAFT → ACTIVE`, but "at most one active per
   vertical" plus "the old row is retained" forces a terminal state for the row being replaced. Named here
   rather than invented quietly during implementation.
6. **`RetrievalMethod` gains `manual_research`.** Needed because the seeds are hand-authored and no existing
   member describes that honestly. If you would rather the seeds cite `web_lookup`, say so — but that member is
   defined as a per-request lookup, and misusing it is the sort of small lie the provenance primitive exists to
   prevent.
7. **`lpe manifest list` is added**, though the ticket names only `show` and `activate`. Without it there is no
   way to discover the id that `show` requires, short of querying the API or the database by hand.
8. **The dev Supabase password is rotated before this branch runs its migration** (`_tasks/todo.md` → B2). The
   throwaway test container makes every test independent of that, but `alembic upgrade head` against dev will
   fail until it is done.
9. **`geography` is not a manifest field.** It is a brief input (T4/T5) — DFW is the metro for every vertical
   in the MVP, so putting it in the manifest would duplicate a fact that belongs to the run.

**Questions that would change this plan if answered differently:**

- **Who reviews manifest *quality*, beyond terms of use?** This is an open question in both the architecture
  doc and the ticket gates, and it is *this slice's* open question — `activate` catches the legal question and
  nothing catches a plausible-but-wrong disqualifier or a mis-chosen authoritative source. **The plan does not
  answer it.** It bites at vertical #2 (T12), not here, but if you want a quality checklist recorded at
  `activate` time (even just a free-text `reviewed_note`), that is a schema change and it is cheaper now than
  after two manifests exist.
- **Should `Vocabulary` carry an `avoid` list?** E16's "never lead with AI" is a hard messaging constraint with
  three documented rejections behind it. It is left out here as out-of-scope gold-plating — T11 owns outreach
  copy — but if the vocabulary is the natural home for it, adding a field later means a schema version bump.

---

## NOTES (open canvas)

**Why terms-of-use sits outside the citation.** The tempting shape is `ManifestSource.terms`, one object
holding everything about a source. It does not survive contact with `ProvenancedValue`, and the reason is
worth keeping: a citation is a claim about *when and how a value was obtained*. `activate` does not re-obtain
the source declaration — it records a human's decision about it. If the decision lived inside the cited value,
activation would have to construct a new `ProvenancedValue` with a fresh `retrieved_at`, and the manifest would
then claim FMCSA's base URL was "retrieved" at the moment someone typed `--accept-terms`. Splitting them keeps
both facts honest, and it is the same split the whole system runs on: **Supabase holds what the machine
learned; a human's decision is recorded as a human's decision.**

**Why the invariant is in the database as well as the service.** The service check is the one that produces a
good error message; the partial unique index is the one that is still true when a future ticket writes to the
table through a path nobody anticipated. This project already made this choice once — provenance is a *type*
rather than a check, "because a check can be forgotten by a new slice; a type cannot be constructed wrongly".
A database constraint is the same argument aimed at the same failure.

**What this slice quietly decides for four later tickets.** T2 is the first slice, so its shape becomes the
house pattern whether or not anyone intends it. Three decisions to make deliberately rather than by accident:

| Decision | This plan | Who inherits it |
|---|---|---|
| Repository flushes; service commits | stated in the README | T4, T9, T11 |
| Slice exceptions derive from `LocalProspectEngineError` and never get caught in handlers | stated in *Patterns* | every slice with routes |
| Tests in `tests/<slice>/`, DB tests behind `requires_db` | `tests/conftest.py` | T4, T11 |

**On the rule schema and the pull toward a DSL.** `DisqualifierRule` could grow into a small expression
language — nested and/or, field paths, regex. Resist it here. T7 owns the evaluator and will discover what the
rules actually need against real candidates; a DSL designed in advance against two hand-written examples is how
you get an abstraction that fits neither vertical. The `RuleKind.judgment` escape hatch is what keeps the
mechanical half small: anything that needs real judgment is not a predicate, it is a call to the
`classify_rollup` node, and E7's "national rollup with no local owner" is exactly that.

**Sequencing note.** Phases 1 and 2 are the load-bearing half; Phases 4 and 5 (CLI and seeds) are mechanical
once the service exists. If this ticket gets split across two sessions, that is the seam — and if the CLI is
what runs out of time, the slice is still complete enough for T4/T5 to build against, because they consume
`get_active()`, not the CLI.

**Merge surface against T3.** T3 (`promotion/`) runs in a parallel worktree off the same commit. The files both
branches could touch: `app/main.py` (both mount a router — adjacent lines), `CLAUDE.md` (both update the
architecture map), `tests/test_structure.py` (both may append a guard), `app/shared/provenance.py` (only if T3
also adds an enum member), `uv.lock` (T2 changes it only via `[project.scripts]`; regenerate on conflict, never
hand-merge — `.claude/references/conventions.md` → *merge*). Migrations do **not** overlap: T3 adds none.

---

## AMENDMENTS

*(append-only; newest at the bottom; leave empty at creation)*
