# Feature: T13 — Adoption: bring the hand-worked prospects into the cadence machine

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. `HubSpotClient`, `ObjectType`,
`SearchRequest`, `SearchFilter`, `SearchFilterGroup`, `SearchOperator`, `TaskStatus`, `HubSpotObject` come
from `app.promotion`; `OutcomeReader`, `plan_advance`, `find_signal` from `app.cadence.sync`;
`CadencePosition`, `Touch`, `CYCLES`, `CADENCE_TZ`, `due_at_enrolment`, `task_key`, `TASK_KEY_PREFIX` from
`app.cadence.machine`. Every exception derives from `app.core.exceptions.LocalProspectEngineError`.

## Feature Description

`lpe cadence adopt` puts the prospects the founders were already working by hand into the three-touch
cadence machine (T11). It **reconstructs** each prospect's place in the cycle from the activity already
logged on the contact. It runs that history through the same `plan_advance` the daily sync uses, so a
prospect touched twice by hand resumes at touch three. It never resets anyone to touch one. A **roster**
the human writes decides who is adopted and who is parked: the refusals get a parked row, so nothing can
ever enrol them again. A dry run comes first and shows, per contact, the evidence behind its reconstructed
position. The roster can override any position the evidence gets wrong.

## User Story

As the founder whose September prospects stalled after day 4 (Spike 1: 1 of 22 tasks done, nothing logged
since 09-29)
I want those prospects to enter the cadence at the touch they actually reached, with refusals kept out
So that the daily sync starts driving them tomorrow, and the queue stops being something nobody works.

## Problem Statement

T11's machine exists and its daily sync runs, but it has **zero live prospects**. T9 is the only other
enrolment path, and it is waves away. The hand-worked prospects carry no cadence state, their tasks are
untidy, and their history is mostly **notes** on unticked tasks. Enrolling them at touch one would make
the machine nag about work already done, which is the failure D5 exists to prevent.

**What the live portal showed (read-only, 2026-10-10), and what it changed:**
- **Tasks are not prospects.** 26 tasks in all. Jorge has 3, and Koetter, APS, Central and DSS have 2 each.
  Two are HubSpot sample tasks, and two are on contacts from February. The unit of adoption is the
  **contact** (`cadence_state` is keyed on it), not the task.
- **Kodiak has a company and no contact.** It cannot be enrolled until a person adds a contact in HubSpot.
- **One act was often logged twice.** The 09-22 calls each have a note on the *company* (~15:10, typed by
  hand) and a "Cold call 9/22" note on the *contact* (22:20). The sync reads contact activity only.
- **The refusals have no open tasks.** Five Star ("not interested"), Complete Fire ("not interested right
  now") and Lone Star ("do not call them again") can only be found from their notes, which is a judgment.
  No mechanical selection finds them.

## Solution Statement

**Decided (human, 2026-10-10)**, so these are inherited, not reopened:

1. **An explicit roster.** `adopt --dry-run` lists the candidates. A TOML roster marks each contact
   `adopt` or `park`, with an optional start override. `adopt --roster <file>` applies it.
2. **Evidence comes from the contact only.** That is exactly what the daily sync reads afterwards, so
   adoption and sync always agree. Notes logged only on the company are missed, and the roster's `start`
   override is the fix for those cases.
3. **Old hand tasks are left alone and listed.** Adoption creates the cadence task and prints the open hand
   tasks for the founder to close. The slice never writes to the founder's tasks.
4. **No `candidate` rows. The ticket is amended.** Adoption writes no prospect field, so the write-gate has
   nothing to check, and cadence keeps depending only on `core/` and the HubSpot client. Honest
   `manual_hubspot_entry` provenance belongs to T9, the first place one of these records' fields would be
   written.

**The reconstruction.** Per contact:
`plan_advance(CadencePosition.first(), anchor_at=contact.createdAt, anchor_ref=None, task=None, activities, now)`
- **Nothing matched** → start at (1, call), due by end of today, anchor `(createdAt, None)`.
- **Touches closed** → start at `plan.next_position`, due `plan.next_due_at` (already floored to today),
  anchor `(plan.anchor_at, plan.anchor_ref)`.
- **All nine closed** → a parked row. This is unlikely with this data, and handled anyway.
- **`start` override** → that position, due by end of today, anchor `(now, None)`. The human is saying
  "from here, from now".

**The anchor_ref bug this exposes, fixed here.** `enrol()` takes `anchor_at` but no `anchor_ref`.
`repository.create` hard-codes `anchor_ref=None` (`app/cadence/repository.py:136`). Suppose a
reconstruction ends on a note. That anchor is `(note.hs_timestamp, "notes:<id>")`. Stored with
`ref=None`, the note sorts *after* the anchor, and the next sync credits it a second time. That
over-advances by one touch on every adopted prospect whose history ends on an activity, which is almost
all of them. `enrol` and `create` gain `anchor_ref`.

**Re-runnable by construction.** A contact already enrolled is reported and skipped, never an error. The
deferred T11 review **L1** (`pr-9-review.md:150`) is fixed in `enrol` because adoption is its first real
caller:
- **Look before create.** Before creating the task, `enrol` looks for a task already carrying its key.
  `OutcomeReader.find_task_by_key` does this, the same way the sync's H1 does. A crash between the HubSpot
  create and the commit then adopts the orphan task on the re-run instead of making a duplicate on the
  founder's list.
- **IntegrityError mapping.** An `IntegrityError` on `uq_cadence_state_contact` becomes
  `AlreadyEnrolledError`.

**Under the sync lock.** A real adopt holds the run lock, as `park` does, and refuses with
`SyncRunningError` while a sync runs. The dry run takes no lock and writes nothing.

## Out of Scope / Non-Goals

- **Not included:** sourcing `candidate` rows or any `sourcing_run`; provenance for these records belongs to
  T9 (decided).
- **Not included:** reading company-associated activity, in adoption or in the sync (decided: contact only).
  A follow-up for the sync is noted under Open Questions.
- **Not included:** creating contacts (Kodiak). Adoption lists company-only tasks and says to add a contact
  by hand. Creating one would be a prospect field write, which is T9's.
- **Not included:** closing, editing or re-using the old hand tasks (decided: leave and list).
- **Not included:** a route for `adopt`. CLI only, like `park`: one user, a one-off job.
- **Not included:** counting reconstructed history toward M5. No `cadence.sync.touch_done` is logged by
  adoption. M5 counts touches the *machine* closed, and history is not that.
- **Not included:** note body previews in the dry run. It prints kind, id and Dallas time, and the human
  opens HubSpot to judge.
- **Not changing:** the sync, `plan_advance`, `find_signal`, the done-policy, task text, the
  `cadence_state` schema. **No migration.**

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium. The pure core is small and reuses `plan_advance`. The work is the
orchestration, the fake portal's new routes, and getting the anchor right.
**Primary Systems Affected**: `app/cadence/` (new `adoption.py`; `service`, `repository`, `schemas`, `cli`,
`exceptions`); one enum member in `app/promotion/schemas.py`
**Dependencies**: none new. `tomllib` is stdlib (3.11+).

## Related Work

**Implements**: T13 (`docs/tickets/local-prospect-engine.md` → *T13 — Adoption*) · **Epic**:
`docs/local-prospect-engine.architecture.md` (*Spike 1*, *Cadence*), `docs/local-prospect-engine.prd.md`

**Back-references:**
- `.claude/plans/t11-cadence.md` — the state machine, D5 policy, and the `enrol` seam this calls.
- `.claude/code-reviews/pr-9-review.md:150` — L1 (enrol race and orphan task), deferred until enrol had a
  caller; closed here.
- `.claude/code-reviews/pr-12-review.md` — the `_RowView` single-moment lesson; the dry run here also plans
  from one read.
- `_tasks/todo.md` → *Spike 1 closed* (the evidence) and *Before T13* (`park` and `sync --dry-run`, both
  built for this run).

**Forward-references:**
- T9 inherits the honest-provenance obligation for adopted records (ticket amended in this work).
- T10: **M5 checkpoint**. Read M5 on the adopted prospects before T10's first real run.

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `app/cadence/service.py` (lines 151-229) — `CadenceService.__init__` (factory and clock injection) and
  `enrol`, the seam you extend: task created **before** the row, one commit.
- `app/cadence/service.py` (lines 256-345) — `_run`/`_record_failure`: the per-prospect try/except
  (`HubSpotError`, `ValidationError`, `SQLAlchemyError`) → rollback → `SyncFailure` → carry on. **Mirror it
  exactly** for per-roster-entry failures.
- `app/cadence/service.py` (lines 514-563) — `park`: how the sync lock is taken around a write, and
  `SyncRunningError`.
- `app/cadence/sync.py` (lines 111-214) — `find_signal` and `plan_advance`. **Reuse; do not reimplement.**
- `app/cadence/sync.py` (lines 250-313) — `OutcomeReader`: `tasks`, `find_task_by_key`, `activities`. You
  call `activities(contact_id)` and `find_task_by_key`.
- `app/cadence/repository.py` (lines 113-147, 190-207) — `create` (add `anchor_ref`) and `park` (shape for
  a new `create_parked`). Read the docstring at the top for the rule: the repository flushes, the service
  commits.
- `app/cadence/models.py` (lines 77-96) — the check constraints. A parked row may have no task, no due date
  and no pending key. A live row must have `due_at` plus a task or a pending key.
- `app/cadence/machine.py` (lines 59-111, 168-200) — `CadencePosition` (frozen, validated),
  `due_at_enrolment`, `task_key`, `TASK_KEY_PREFIX`.
- `app/cadence/schemas.py` (lines 133-249) — `ProspectPlan`, `ParkResult`, `SyncFailure`, `SyncReport`:
  the style for the new adoption schemas (Pydantic, field docstrings, `Field(default_factory=list[X])`).
- `app/cadence/cli.py` (whole file, 235 lines) — `register`/`dispatch`, `_with_service`, the print style.
- `app/cadence/exceptions.py` (lines 22-67) — exception pattern: `default_code`, `status_code`, kwargs.
- `app/promotion/client.py` (lines 423-433) `search`; (580-607) `batch_read`; (609-668)
  `list_associated_ids`, which works for any `from_type`/`to_type` pair.
- `app/promotion/schemas.py` (lines 49-56, 150-190) — `SearchOperator` has only `eq`; `SearchRequest` has
  no paging cursor; `SearchResponse.total`.
- `tests/cadence/conftest.py` (whole file) — `FakeHubSpot`, `FakeClock`, `START`, `iso`; **route order
  matters** (`MockPortal` matches the first `(method, fragment)`; see `tests/promotion/conftest.py:79-103`).
- `tests/cadence/test_service.py` — DB-tier service test style (uses `service`, `hubspot`, `clock`
  fixtures; `requires_db`).
- `tests/cadence/test_cli.py` (lines 1-80, 204-270) — CLI tests commit their own rows under unique contact
  ids and patch `get_hubspot_client` in `app.cadence.cli`.
- `app/shared/provenance.py` (lines 85-92, 165-177) — `manual_hubspot_entry` and `is_promotable`. Read to
  understand what T13 deliberately does **not** do now.

### New Files to Create

- `app/cadence/adoption.py` — roster model and parser, the pure `reconstruct`, and `Adopter` (discover /
  rehearse / apply).
- `tests/cadence/test_adoption.py` — pure tests: roster parsing, `parse_position`, `reconstruct`. No DB.
- `tests/cadence/test_adoption_service.py` — DB tier: adopt, park, re-run, anchor regression, zero-field
  record, lock, failures.

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [Python `tomllib`](https://docs.python.org/3/library/tomllib.html#tomllib.loads) — `tomllib.loads(str)`
  → `dict[str, Any]`; raises `tomllib.TOMLDecodeError`. Why: roster parsing.
- [HubSpot CRM search](https://developers.hubspot.com/docs/api/crm/search#filter-search-results) —
  `NEQ` operator; at most 200 per page; `paging.next.after`. Why: discovery lists open tasks.
- [HubSpot associations v4](https://developers.hubspot.com/docs/api/crm/associations) —
  `GET /crm/objects/2026-03/{from}/{id}/associations/{to}`. Why: task→contact and contact→company.
  `list_associated_ids` already wraps this.
- `app/cadence/README.md` → *The done-signal (D5)* and *Enrolment — the seam for T9 and T13*. Why: the
  ordering rule that makes `anchor_ref` load-bearing.

### Patterns to Follow

**Naming:** snake_case functions, PascalCase Pydantic models, `StrEnum` for closed sets. The CLI words
match the domain: "adopt", "park", "would start", "evidence".

**Error handling:** deliberate failures are `CadenceError` subclasses with `default_code`/`status_code`;
per-prospect HubSpot/validation/DB errors are **caught, rolled back and reported**, never raised
(`service.py:286-302`). A bad roster is one `RosterError` line from `lpe`, not a traceback.

**Logging:** structlog, `domain.component.action_state`:
`cadence.adoption.run_started`, `cadence.adoption.prospect_adopted`, `cadence.adoption.prospect_parked`,
`cadence.adoption.already_enrolled`, `cadence.adoption.prospect_failed`, `cadence.adoption.run_completed`,
`cadence.service.task_reused` (enrol's look-before-create hit).

**Service pattern (from `enrol`):**
```python
created = await self._hubspot().create_task(task_for(...), contact_id=..., company_id=...)
state = await self._repository.create(..., task_id=created.id, ...)
await self._session.commit()
```

**Lock pattern (from `park`):**
```python
async with self._repository.sync_lock() as acquired:
    if not acquired:
        raise SyncRunningError("a cadence sync is running — adopt again when it has finished")
    ...
```

**Fake-portal pattern:** add routes to `FakeHubSpot.__init__`'s list **above** the generic `"/batch/read"`
and `"/associations/"` entries, so the specific route wins.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — the seam fixes

`enrol`/`create` take `anchor_ref`. `create_parked` is added. `enrol` looks before it creates and maps
`IntegrityError`. `SearchOperator.neq` is added. These are small, independently testable, and every later
phase rests on them.

### Phase 2: Core — `adoption.py`

The roster model and parser, `parse_position`, and the pure `reconstruct` (wraps `plan_advance`). Then
`Adopter`, which reads and decides each prospect from one moment and then applies. It reuses
`CadenceService.enrol` and a new `CadenceService.adopt_parked`.

**Depends on:** Phase 1.

### Phase 3: Integration — CLI, exports, docs

`lpe cadence adopt [--roster PATH] [--dry-run]`, the package exports, the README section, the `CLAUDE.md`
commands, and the ticket and reference amendments.

**Depends on:** Phase 2. **Independent of:** Phase 4's pure tests, which can be written first (TDD).

### Phase 4: Testing & Validation

Pure tests first, failing first, then the DB tier, then the CLI. Then the live rehearsal, which is
human-gated.

---

## STEP-BY-STEP TASKS

Write each test **before** its code and watch it fail (house rule from T11/PR #12). Run
`uv run pytest tests/cadence -q` after every task. Per the memory notes, `uv` and git writes need the
sandbox disabled.

### UPDATE `app/promotion/schemas.py`

- **IMPLEMENT**: add `neq = "NEQ"` to `SearchOperator`, and update its docstring ("Members as callers need
  them: `eq` for dedupe, `neq` for adoption's open-task listing").
- **PATTERN**: `app/promotion/schemas.py:49-56`
- **GOTCHA**: the docstring says "one member, deliberately". Keep the reasoning, change the count.
- **VALIDATE**: `uv run pytest tests/promotion -q`
- **SATISFIES**: AC-1 (discovery)

### UPDATE `app/cadence/repository.py`

- **IMPLEMENT**:
  - `create(..., anchor_ref: str | None = None)`, written to the row instead of the hard-coded `None`.
  - New `create_parked(*, contact_id, company_id, owner_id, position, anchor_at, anchor_ref, enrolled_at,
    parked_at) -> CadenceState`, with `status=parked`, no task, no due date and no pending key. It flushes
    and logs `cadence.repository.state_created` with `status="parked"`.
- **PATTERN**: `create` at `repository.py:113-147`; constraints at `models.py:77-96`
- **GOTCHA**: `create_parked` must satisfy `ck_cadence_state_parked_has_no_pending`. Leave
  `pending_task_key` unset.
- **VALIDATE**: `uv run pytest tests/cadence/test_models.py -q` (add: a parked row with no task inserts; a
  live row created with `anchor_ref` round-trips it)
- **SATISFIES**: AC-3, AC-5

### UPDATE `app/cadence/service.py` — `enrol`

- **IMPLEMENT**:
  - `enrol(..., anchor_ref: str | None = None)`, passed through to `create`. Document the rule: **when
    `anchor_at` came from an activity, its ref must come too**, or the next sync re-credits that activity.
  - **Look before create.** `key = task_key(contact_id, position)`, then
    `existing = await OutcomeReader(client).find_task_by_key(contact_id, key)`. If found, use
    `existing.task_id` and log `cadence.service.task_reused`; otherwise `create_task` as today.
  - Wrap the `repository.create` flush. On `IntegrityError` naming `uq_cadence_state_contact`, roll back
    and raise `AlreadyEnrolledError`.
- **PATTERN**: H1's `_resolve_pending`, `service.py:475-512`
- **IMPORTS**: `from sqlalchemy.exc import IntegrityError`; `OutcomeReader` is already imported
- **GOTCHA**: call `self._hubspot()` **once** in `enrol` and reuse the client. The look-before-create costs
  1–2 reads per enrol, which is acceptable for T9 too. Keep the task-before-row order. Match the constraint
  name in `str(exc.orig)`, so other integrity errors still raise.
- **VALIDATE**: `uv run pytest tests/cadence/test_service.py -q`. New tests:
  - (a) enrol with `anchor_ref="notes:X"`, then sync at once: nothing closes.
  - (b) a task carrying the key already exists on the contact: no create, its id is attached.
  - (c) a row inserted under the same contact after the `get_by_contact` check (monkeypatch that check to
    return `None` once): `AlreadyEnrolledError`, not `IntegrityError`.
- **SATISFIES**: AC-3, AC-6; closes pr-9 L1

### UPDATE `app/cadence/service.py` — `adopt_parked`

- **IMPLEMENT**: `async def adopt_parked(self, contact_id, *, company_id, position, anchor_at, anchor_ref)
  -> CadenceStateResponse`. It raises `AlreadyEnrolledError` when a row exists, then calls
  `repository.create_parked(enrolled_at=now, parked_at=now, owner_id=None)`, commits and logs
  `cadence.service.prospect_parked` with `by="adoption"`. It never reaches HubSpot. It does not take the
  lock itself, because the caller (`Adopter.apply`) holds it.
- **PATTERN**: `park`, `service.py:514-563`
- **VALIDATE**: `uv run pytest tests/cadence/test_service.py -q`. New test: an `adopt_parked` contact then
  `enrol`s → `AlreadyEnrolledError`, and `sync` does not list it.
- **SATISFIES**: AC-5

### UPDATE `app/cadence/exceptions.py`

- **IMPLEMENT**: `RosterError(CadenceError)`, with `default_code = "invalid_roster"` and
  `status_code = 422`. The message names the file, plus the entry index or contact where known.
- **PATTERN**: `exceptions.py:37-45`
- **VALIDATE**: `uv run ruff check app/cadence`
- **SATISFIES**: AC-2

### UPDATE `app/cadence/schemas.py`

- **IMPLEMENT** (each model and field with a one-line docstring, as in the file):
  - `RosterAction(StrEnum)`: `adopt`, `park`.
  - `HandTask`: `task_id`, `subject`, `completed: bool`, `due_at: datetime | None`. A hand-made task on the
    contact (any task whose body lacks `TASK_KEY_PREFIX`). Display only; never stored.
  - `AdoptionPlan`:
    - identity: `hubspot_contact_id`, `display_name: str | None` (read for the screen, never stored),
      `company_id: str | None`
    - `action: RosterAction`
    - the reconstruction: `steps: list[AdvanceStep]` (the evidence), `start: CadencePosition | None`
      (`None` when it would park), `due_at: datetime | None`, `anchor_at: datetime`,
      `anchor_ref: str | None`, `overridden: bool`, `finished: bool` (all nine touches already done)
    - `hand_tasks: list[HandTask]`, `warnings: list[str]` (e.g. "2 companies — set `company` in the
      roster", "no company associated")
    - `already_enrolled: bool`
  - `CompanyOnlyTask`: `task_id`, `subject`, `company_ids: list[str]`. An open task with no contact (Kodiak).
  - `AdoptionReport`:
    - `ran_at`, `dry_run`
    - `plans: list[AdoptionPlan]`
    - `adopted: list[str]` (task ids created or reused), `parked: list[str]`, `already_enrolled: list[str]`
    - `failures: list[SyncFailure]` (reuse it), `company_only: list[CompanyOnlyTask]`, `warnings: list[str]`
- **PATTERN**: `schemas.py:133-249`
- **GOTCHA**: `CadencePosition` is frozen and validated. Never construct cycle 0 or cycle 4.
- **VALIDATE**: `uv run mypy app/cadence && uv run pyright app/cadence`
- **SATISFIES**: AC-1, AC-4

### CREATE `app/cadence/adoption.py`

- **IMPLEMENT**:
  - **Module docstring.** Why adoption exists (Spike 1). The four decisions. Contact-only evidence. No
    fields written, so the write-gate is not involved (T9 owns provenance for these). The anchor rule. No
    `touch_done` logged.
  - `parse_position(text: str) -> CadencePosition`: `"2-call"` → `(2, call)`. This is the suffix format of
    `task_key`. It raises `ValueError` on anything else.
  - `RosterEntry(BaseModel, extra="forbid", frozen=True)`: `contact: str` (non-blank, digits only),
    `action: RosterAction`, `start: str | None` (validated by `parse_position`; **only with
    `adopt`**), `company: str | None`.
  - `Roster(BaseModel, extra="forbid")`: `prospect: list[RosterEntry]` (min 1), with unique contacts.
  - `load_roster(path: Path) -> Roster`, which wraps read, `tomllib.loads` and `model_validate`. A missing
    file, a `TOMLDecodeError` or a `ValidationError` all become `RosterError` with the path in the message.
  - The pure core, `reconstruct(activities: Sequence[Activity], *, since: datetime, now: datetime,
    override: CadencePosition | None) -> Reconstruction` (a frozen dataclass or model holding `steps`,
    `start`, `due_at`, `anchor_at`, `anchor_ref`, `finished`, `overridden`). It runs
    `plan_advance(CadencePosition.first(), since, None, None, activities, now)` and maps the four cases
    exactly as in the *Solution Statement*.
  - `class Adopter`, built with `(session, *, hubspot: HubSpotFactory, clock: Clock)`. It builds a
    `CadenceService` and a `CadenceRepository` on the same session and clock.
    - `discover() -> AdoptionReport` (dry run, no roster):
      1. Search tasks with `hs_task_status NEQ COMPLETED`, properties `hs_task_subject`, `hs_task_body`,
         limit 200. If `total > len(results)`, add a warning ("more than 200 open tasks — listing
         truncated").
      2. Drop the tasks whose body contains `TASK_KEY_PREFIX` (already ours).
      3. For each remaining task, read its task→contacts association. A task with no contact reads
         task→companies and becomes a `CompanyOnlyTask`.
      4. Take the distinct contacts and `_plan_contact(contact, RosterAction.adopt, None, None)` each.
    - `rehearse(roster) -> AdoptionReport`: `_plan_contact` for each entry. Nothing is written and no lock
      is taken.
    - `apply(roster) -> AdoptionReport`:
      1. **Under `repository.sync_lock()`** (`SyncRunningError` if it is held), plan each entry first,
         then apply it.
      2. `adopt`, not finished → `service.enrol(contact, company_id=..., start=plan.start,
         anchor_at=plan.anchor_at, anchor_ref=plan.anchor_ref, due_at=plan.due_at)`.
      3. `park`, or `adopt` that is finished → `service.adopt_parked(...)` at the reconstructed position.
      4. `AlreadyEnrolledError` → `already_enrolled`, and continue.
      5. `HubSpotError` / `ValidationError` / `SQLAlchemyError` → roll back, add a `SyncFailure`, and
         continue.
    - `_plan_contact(contact, action, override, company) -> AdoptionPlan`:
      1. `repository.get_by_contact` → `already_enrolled`.
      2. `batch_read(contacts, [id], ["firstname", "lastname", "createdate"])`. If absent, raise a
         per-entry failure with code `contact_not_found`.
      3. `since = record.created_at`. If it is missing, fail with code `contact_created_at_missing`.
      4. `OutcomeReader.activities(contact)`.
      5. The contact's tasks: associations, then `batch_read` of the subject, status, body and
         `hs_timestamp`. Keep those without the key prefix → `hand_tasks`.
      6. Company: the roster's value, else the contact→companies association. Exactly one → use it.
         Zero → `None` plus a warning. More than one → `None` plus a warning.
      7. `reconstruct(...)`.
  - Use **one** `now = clock()` per run for every decision, the single-moment rule from PR #12.
- **PATTERN**: `service.py:256-345` (loop and failures); `service.py:95-125` (`_RowView`: capture plain
  values before writing)
- **IMPORTS**: `tomllib`, `pathlib.Path`, `pydantic.ValidationError`, `sqlalchemy.exc.SQLAlchemyError`,
  `app.promotion.exceptions.HubSpotError`, `app.promotion.schemas.{ObjectType, SearchRequest,
  SearchFilterGroup, SearchFilter, SearchOperator, TaskStatus}`
- **GOTCHA**:
  - `tomllib.loads` returns `dict[str, Any]`. Pass it straight to `Roster.model_validate`, and annotate
    nothing as `Any`, because the rules allow no `Any` escape hatch.
  - `list_associated_ids(ObjectType.tasks, task_id, ObjectType.contacts)` is valid. The client is
    generic.
  - `find_task_by_key` is **not** used for hand tasks, which carry no key.
  - Do not log or print note bodies.
  - The search index lags writes by seconds. That is fine for a one-off listing, and the per-contact reads
    use associations, which do not lag.
- **VALIDATE**: `uv run pytest tests/cadence/test_adoption.py -q`
- **SATISFIES**: AC-1 to AC-7

### UPDATE `app/cadence/cli.py`

- **IMPLEMENT**:
  - **Register the command.** `adopt` takes `--roster PATH` and `--dry-run`. Its help: "bring hand-worked
    prospects into the cadence at the touch they reached; run with --dry-run first". With neither flag,
    `parser.error("adopt needs --roster; run adopt --dry-run to list candidates")`.
  - **Dispatch.** No roster → `discover()`. Roster plus dry run → `rehearse()`. Roster alone → `apply()`.
    `RosterError` is already rendered as one line by `lpe`; check how `app/cli.py` handles
    `LocalProspectEngineError`.
  - **Print each plan:**
    ```
    contact 552902851267 (Jorge Rodriguez) · company 346975458005
      evidence: 1/3 call ← note 401433689810, 2026-09-22 09:12 (Dallas)
                1/3 voicemail ← note 401831733986, …
      would start 2/3 call, due 2026-10-10 23:59 (Dallas)     | would park — <reason>
      open hand tasks — close them in HubSpot after adopting: 401480532700 "Follow up with …"
      ticked hand tasks (not counted as evidence — set `start` if they were touches): …
      warning: …
    ```
  - **Print the rest of the report:** the company-only tasks ("no contact — add one in HubSpot, then put it
    in the roster"), already-enrolled entries, failures, and a summary line. On an applied run, also print
    "log nothing here — outcomes stay in HubSpot" and the hand tasks to close.
  - **Exit code** 1 if there are failures.
- **PATTERN**: `_print_plan`, `_activity_text` (`cli.py:143-192`, reuse it), `_run_park` (`cli.py:195-212`)
- **GOTCHA**: update the module docstring. "There is deliberately no enrol subcommand" stays true, and
  `adopt` is the reconstruction-only path, so say why it differs.
- **VALIDATE**: `uv run pytest tests/cadence/test_cli.py -q`
- **SATISFIES**: AC-1, AC-2, AC-4

### UPDATE `app/cadence/__init__.py`

- **IMPLEMENT**: export `Adopter`, `RosterError`, `reconstruct`, and add the module docstring line.
- **VALIDATE**: `uv run python -c "import app.cadence as c; print(c.Adopter)"`

### UPDATE `tests/cadence/conftest.py`

- **IMPLEMENT**, all **above** the generic routes:
  - **Seed helpers.** `add_contact(contact_id, created_at, first=None, last=None)`,
    `add_company(company_id)`, `link_company(contact, company)`, and
    `add_hand_task(subject, *, contact=None, company=None, completed_at=None, due=None) -> str`. A hand task
    has no `Ref:` line.
  - **Routes:**
    - `POST /tasks/search`: filter `hs_task_status` `NEQ` against the stored tasks, and return `total` and
      `results`.
    - `POST /contacts/batch/read`
    - `GET /tasks/<id>/associations/<kind>`
    - `GET /contacts/<id>/associations/companies`
  - **Associations regex.** Generalise `_ASSOCIATIONS` to `/(?P<from>contacts|tasks)/(?P<id>[^/]+)/
    associations/(?P<kind>[a-z]+)$` and keep the existing contact behaviour.
  - **New fixture.** `adopter(db_session, hubspot_client, clock)`.
- **GOTCHA**:
  - `contacts/batch/read` and `tasks/batch/read` both contain `"/batch/read"`. Order the routes most
    specific first.
  - Created tasks are associated to contacts in `contact_engagements` as `("tasks", id)`, and hand tasks
    must be too, so `find_task_by_key`, `open_tasks` and the hand-task listing all see them.
- **VALIDATE**: `uv run pytest tests/cadence -q` (nothing regresses)

### CREATE `tests/cadence/test_adoption.py` (pure, no DB)

- **IMPLEMENT**:
  - **`parse_position`:** `"1-call"`, `"3-email"` ✓; `"0-call"`, `"4-call"`, `"2-door"`, `"call"` ✗.
  - **The roster, accepted:** a valid file parses.
  - **The roster, refused:** an unknown key, a duplicate contact, `start` on `park`, an empty
    `prospect`, a non-digit contact, a missing file and malformed TOML each raise `RosterError`, and the
    message names the file.
  - **`reconstruct` on history:**
    - no activity → (1, call), due end of today, anchor `(since, None)`
    - **two logged calls → (1, email)** (the ticket's test)
    - three notes → (2, call), due floored to today
    - the anchor is the last credited activity's `(timestamp, ref)`
    - an activity before `since` is ignored
    - a future-dated meeting is ignored
    - nine matching notes in order → `finished`
  - **`reconstruct` with an override:** the start is the override, `anchor == (now, None)`, the steps are
    still listed (shown as evidence), and `overridden=True`.
- **VALIDATE**: `uv run pytest tests/cadence/test_adoption.py -q`
- **SATISFIES**: AC-2, AC-3

### CREATE `tests/cadence/test_adoption_service.py` (DB tier, `requires_db`)

- **IMPLEMENT**:
  1. **Zero citable fields adopts** (the ticket's test). The contact has only an id and `createdAt`.
     Adoption creates one task and one live row. Assert **no request** to `/contacts` or `/companies` other
     than `batch/read` and associations: no `POST` creates and no `PATCH`es. That proves the gate is not in
     play.
  2. **Two prior calls resume at touch three.** The row is at (1, email) and the created task subject is
     `Cadence 1/3 · email draft (send by hand)`.
  3. **The anchor regression.** History ends on a note. Adopt, then `service.sync()` straight away →
     `touches_closed == 0` and `tasks_created == 0`.
  4. **Notes count, and history is not M5.** Adopt a notes-only history; then a new note logged after
     adoption closes the next touch on sync. Assert adoption logs no `cadence.sync.touch_done` (use
     `structlog.testing.capture_logs`).
  5. **Park entries.** A parked row, no task created, then `enrol` → `AlreadyEnrolledError`.
  6. **Re-run is a no-op.** Applying the same roster twice gives the second run `already_enrolled` for all
     entries and no new task creates.
  7. **Interrupted enrol.** `lose_create_responses = 1` on the first apply → that entry fails with
     `server_error`. The re-run finds the task by key and reuses it: `task_creates_attempted()` grows by 0
     on the re-run.
  8. **One bad entry does not stop the others.** An unknown contact gives `contact_not_found`, and the
     next entry still adopts.
  9. **The lock.** With the advisory lock held on another connection, `apply` → `SyncRunningError`, and
     `rehearse` still works.
  10. **Discover.**
      - Sample-shaped and our own keyed tasks are excluded.
      - A company-only task is listed under `company_only`.
      - A contact with 3 hand tasks appears once, with all 3 listed.
      - An already-enrolled contact is flagged.
  11. **Override.** `start="2-call"` → row at (2, call), due today, `anchor_at == now`.
  12. **Hand tasks untouched.** After apply, every hand task is still `NOT_STARTED` and none was PATCHed.
  13. **Company choice.** Two linked companies plus no roster `company` → a warning and a task with no
      company association. With roster `company` → associated.
- **PATTERN**: `tests/cadence/test_service.py`
- **VALIDATE**: `TEST_DATABASE_URL=... uv run pytest tests/cadence/test_adoption_service.py -q`
- **SATISFIES**: AC-1 to AC-7

### UPDATE `tests/cadence/test_cli.py`

- **IMPLEMENT**:
  - `adopt` with no flags → exit 2 and the usage hint.
  - `adopt --dry-run` prints evidence and "would start", and writes nothing (no rows, no creates).
  - `adopt --roster f.toml` prints the adopted contact and the hand tasks to close.
  - A malformed roster → one `invalid_roster` line and exit 1.
  - Use `tmp_path` for the roster file, and committed rows under unique contact ids as the file's pattern
    requires.
- **PATTERN**: `test_cli.py:204-270`
- **VALIDATE**: `TEST_DATABASE_URL=... uv run pytest tests/cadence/test_cli.py -q`

### UPDATE docs (ticket amendment and slice docs)

- **IMPLEMENT**:
  - `docs/tickets/local-prospect-engine.md` → T13:
    - Replace the "Adopted candidates carry honest provenance…" criterion with: **"Adoption writes no
      prospect field and creates no `candidate`. Honest `manual_hubspot_entry` provenance applies at the
      first field write, which is T9 (decided 2026-10-10)."**
    - Add the roster, the contact-only evidence and the leave-and-list decisions.
    - Update **Files** to add `schemas`, `service`, `repository`, `cli`, `exceptions` and
      `app/promotion/schemas.py` (`neq`).
    - Add to T9: "adopted contacts already exist in HubSpot and carry no provenance; the first field write
      cites `manual_hubspot_entry`".
  - `.claude/references/hubspot-integration.md` → **Adoption**: rewrite the "they become candidates"
    sentence to match.
  - `app/cadence/README.md`: a new **## Adoption** section covering:
    - roster format with an example
    - dry run first
    - contact-only evidence and the `start` override
    - the anchor rule
    - hand tasks left and listed
    - Kodiak-style company-only tasks
    - not M5
    - re-runnable
    - under the lock

    Then update **Enrolment — the seam** (enrol now has `anchor_ref`, look-before-create) and trim
    **Deferred** if anything closed.
  - `CLAUDE.md` → *Commands*: replace "There is no enrol command by design — enrolment arrives with T9 and
    T13." with "`uv run lpe cadence adopt --dry-run` · `uv run lpe cadence adopt --roster <file>` (T13; no
    plain enrol command by design)". In the *Architecture map*, mark T13 built when it lands.
- **VALIDATE**: `/rules-check-drift` (advisory)

---

## TESTING STRATEGY

### Unit Tests
Pure functions only (`parse_position`, roster validation, `reconstruct`), driven by hand-built `Activity`
lists and fixed datetimes around `START` in `tests/cadence/conftest.py`. Exhaustive on the four
reconstruction cases and the anchor.

### Integration Tests
DB tier through `FakeHubSpot` + `MockPortal`, which raise on any unmodelled request, so "no field write"
is a provable property, not an assertion of absence. The CLI tests run the real `lpe` entry point against
the throwaway Postgres.

### Edge Cases
- History that ends on an activity (anchor_ref) is the regression that matters most.
- An activity exactly at `since`, which counts toward done.
- A future-dated meeting.
- All nine touches already done, which parks.
- A contact with no company, or with two.
- A contact deleted in HubSpot (`contact_not_found`).
- An interrupted create, then a re-run.
- A sync running during apply.
- More than 200 open tasks (warning).
- A roster `start` on a `park` entry (refused).

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style
`uv run ruff check . && uv run ruff format --check .` · `uv run mypy . && uv run pyright`

### Level 2: Unit Tests
`uv run pytest tests/cadence/test_adoption.py -q`

### Level 3: Integration Tests
```
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres
uv run pytest -q          # the skip count must be ~0: 162 skipped means 162 tests did not look
```
Or `/piv-validate` (full tier). `alembic check` must be clean, because there is no migration.

### Level 4: Manual Validation — live portal, **human-gated**
1. `uv run lpe cadence adopt --dry-run` (reads only) → the human reviews every contact's evidence against
   HubSpot.
2. The human writes the roster outside the repo, e.g. `~/lpe/adopt-2026-10.toml`. Expected contents:
   - `park` for Five Star (Justin), Complete Fire (Paul) and Lone Star (Jason/Deborah)
   - `start` overrides where the evidence lives only on the company (APS, Firewise, Koetter and DSS's
     09-22 calls)
   - the two February contacts and the sample tasks left out
3. `uv run lpe cadence adopt --roster <file> --dry-run` → the human confirms.
4. **Only on explicit approval:** `uv run lpe cadence adopt --roster <file>`. This creates real tasks in
   the founder's HubSpot.
5. `uv run lpe cadence sync --dry-run` → expect **nothing closing**, which proves the anchor round-trip on
   live data. Then `uv run lpe cadence overdue`.
6. Confirm that the `lpe-cadence:` key survives in a real task body. This is still unconfirmed per
   `_tasks/todo.md`.

### Level 5: Additional Validation
The HubSpot MCP connector, read-only, to spot-check created tasks' associations and owner after step 4.

---

## ACCEPTANCE CRITERIA

- [ ] **AC-1 Discover.** `adopt --dry-run` lists every contact on an open hand task, once each, with the
      evidence, the would-start position and due date, and its open and ticked hand tasks. It lists
      company-only tasks separately and flags already-enrolled contacts. It writes nothing and takes no
      lock.
- [ ] **AC-2 Roster.** A TOML roster with `adopt`/`park`, an optional `start` (adopt only) and an
      optional `company`. Malformed input is one `invalid_roster` line.
- [ ] **AC-3 Reconstruct, never reset.** Position is reconstructed from contact activity through
      `plan_advance`, and notes count. Two prior logged calls resume at touch three, and a `start` override
      wins. The stored anchor carries `anchor_ref`, so the first sync after adoption closes nothing that
      adoption already credited.
- [ ] **AC-4 Old tasks.** Hand tasks are never written. Their ids are printed for the human to close.
- [ ] **AC-5 Refusals.** `park` entries get a parked row with no task, and can never be enrolled later.
- [ ] **AC-6 Safe to re-run.** A second apply adopts nothing new. An interrupted create is found by key and
      reused, not duplicated. A unique violation becomes `AlreadyEnrolledError`.
- [ ] **AC-7 No field writes.** A record with zero citable fields adopts, and no contact or company write
      is issued. No `candidate` row is created and no `touch_done` is logged.
- [ ] Ruff, mypy, pyright and pytest (database tier) all green, with zero suppressions and no migration.
- [ ] The ticket, the hubspot-integration reference, the cadence README and the `CLAUDE.md` commands are
      amended.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order, each test seen failing first
- [ ] Each task validation passed immediately
- [ ] `/piv-validate` green on the **full** tier (with database)
- [ ] Live dry run reviewed by the human; real apply only on explicit approval
- [ ] `_tasks/todo.md` T13 section ticked, with a review appended

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decided (human, 2026-10-10):** an explicit roster · contact-only evidence · old tasks left and listed ·
no `candidate` rows (ticket amended).

**Assumptions this plan makes. Say so if any is wrong:**
1. **Evidence counts from the contact's `createdate`.** Activity before it is ignored. The alternative was
   all history, which is the same thing here, because notes are associated after the contact exists.
2. **Ticked hand tasks are not evidence.** GFS's task, ticked 10-09, is printed, not counted. They have no
   touch type that maps cleanly onto call/voicemail/email, and the `start` override covers GFS.
3. **An override restarts the evidence clock at now.** Anything already logged is ignored for that
   prospect.
4. **`enrol`'s look-before-create and IntegrityError fix (pr-9 L1) land here,** not as a separate ticket.
   This is a small scope expansion, justified because adoption is enrol's first real caller and a re-run
   after a crash would otherwise duplicate tasks on the founder's list.
5. **The roster file is not committed.** It holds only HubSpot ids, but it is one-off operational data.
6. **Kodiak is listed, not adopted.** Someone adds a contact in HubSpot by hand, and the next roster
   includes it.

**Open, not blocking T13:**
- **The sync reads contact activity only, and founders log on companies.** On 09-22, 5 of the calls were
  logged on the company first. Going forward, a touch logged only on the company will not close its touch
  and will show as overdue. Options for a later ticket: teach the founders to log on the contact (free),
  or read company activity with de-duplication (not free). **Raise this when the adopted prospects run
  through their first real sync.**
- **The M5 checkpoint before T10's first real run** reads M5 on these adopted prospects, from
  `touches_closed` in the daily sync logs.

## NOTES (open canvas)

**Why not reconstruct from tasks?** The task is the human's to-do, not the record of the act. In Spike 1,
1 of 22 was ticked while 10+ notes recorded real touches. D5 already says notes count. Tasks also lie the
other way: Jorge's three tasks are one prospect at one stage.

**Why the anchor needs the ref.** Activities are ordered by `(hs_timestamp, "<type>:<id>")`, and the
anchor `(t, None)` sorts *before* `(t, "notes:123")`. So an anchor at a note's own timestamp with a `None`
ref re-admits that note. During a sync this cannot happen, because `repository.advance` stores the ref.
Only `create` dropped it, because until now enrolment always anchored at "now", which no activity shares.

**Data at planning time** (2026-10-10, read-only, display names for the human only):
- **26 tasks.** 1 is completed (GFS, 10-09). 2 are samples, and 2 are February tasks (Shawn Ahmed,
  "contact back on linkedin").
- **Company-only:** Kodiak.
- **Contacts named after their company:** APS, Firewise, Crisp-LaDew, DSS FireGuard and Koetter were
  created by hand to carry tasks.
- **33 notes since 09-15.** The duplicate pairs fall on 09-22.

Expect roughly 15–18 adopted contacts and 3 parked. The exact set is the human's call in the roster.

**Rejected:** a separate `adoption` table, since `cadence_state` plus `enrolled_at` already records it;
auto-detecting refusals by note keywords, which is a judgment, and a wrong one parks a live prospect
forever; creating contacts for company-only prospects, which is a field write and T9's job.

**Rough size:** ~350–450 lines of app code and ~550–700 of tests, inside the ticket's 500–800 estimate for
code, and over it once the tests are counted.

## AMENDMENTS

