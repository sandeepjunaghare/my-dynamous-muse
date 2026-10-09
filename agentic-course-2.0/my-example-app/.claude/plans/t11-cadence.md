# Feature: T11 — Cadence slice: the three-touch state machine

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. `HubSpotClient`, `ObjectType`,
`TaskCreate`, `TaskStatus`, `TaskType`, `HubSpotObject` come from `app.promotion`; every exception derives
from `app.core.exceptions.LocalProspectEngineError`; `Base` is `app.core.database.Base`.

## Feature Description

`app/cadence/` owns the three-touch cadence schedule that Sales Hub Starter cannot hold (Spike 3: no
sequences). For each enrolled prospect it keeps **only the schedule** — which touch is due, which cycle we
are in, live or parked — in a `cadence_state` table, and it writes each touch to HubSpot as a **Task**, the
human surface. Outcomes are never stored here: on every sync the slice **reads** HubSpot (task status and
the contact's logged calls, emails, notes and meetings) and applies the D5 done-signal to decide whether the
current touch is finished.

The cadence: **call → voicemail → same-day email draft → wait four days → repeat twice → park after three
cycles.** Nine touches, one open task at a time, then parked.

## User Story

As the founder working prospects in the ~4 hours a week I have for new business
I want each prospect's next touch to appear in HubSpot as a task, advance on its own when I have done it,
and tell me what is overdue
So that nobody is contacted once and silently dropped (E15: 22 tasks, 0 completed, 11 past due).

## Problem Statement

E15: every follow-up task in the portal is NOT_STARTED and half are past due. E17: the "three-touch"
cadence is in practice two touches. The tier has no sequences and Starter workflows cannot branch, so
nothing keeps cadence state today. Without a state machine, the engine T9 will feed adds rows to a queue
nobody works.

## Solution Statement

- **`machine.py`** (pure): `Touch`, `CadencePosition`, `next_position()`, due-date rules in America/Chicago.
- **`sync.py`**: the outcome sync — `OutcomeReader` turns HubSpot reads into immutable `TaskSnapshot` /
  `Activity` values, and the **D5 done-policy** (`find_signal`, `plan_advance`) is a pure function over them.
- **`models.py` / `repository.py`**: the `cadence_state` table (schedule only) and its queries; the
  repository flushes, the service commits (T2 house rule).
- **`service.py`**: `enrol()` (the seam T9 and T13 call), `sync()` (idempotent), `overdue()`.
- **`routes.py`**: `POST /cadence/sync` (the trigger launchd/T10 hits), `GET /cadence/overdue`.
- **`cli.py`**: `lpe cadence sync`, `lpe cadence overdue` — the founder's surface, and runnable from
  launchd without a server.
- **Transport additions in `app/promotion/client.py`**: `batch_read()` and `list_associated_ids()`;
  engagement object types added to `ObjectType`. No policy there.

## Out of Scope / Non-Goals

- **Adoption (T13).** No reconstruction of cycle position from history, no `manual_hubspot_entry`
  candidates. T11 leaves the seam: `enrol(start=..., anchor_at=...)`, pure `next_position`, and a reusable
  `OutcomeReader`.
- **No enrol route or CLI.** Enrolling an existing contact without reconstruction would *reset* its cycle,
  which the ground rules forbid. Enrolment arrives via T9 (fresh prospects) and T13 (the 22).
- **No sending, no calling.** The email touch is an EMAIL-type *task* the founder drafts and sends
  by hand (Spike 4). No outreach copy is generated — the opener rewrite is an open question (E16).
- **No outcome-based exit** (e.g. stop on a reached conversation) — not in the ticket; see Decisions.
- **No writes to the founder's tasks** (no auto-completing a task a logged activity superseded).
- **No door touch** in the machine — the ticket's state machine is call/voicemail/email.
- No scheduler, Celery, Redis. No CLAUDE.md / `_tasks/todo.md` edits (sibling worktrees edit them too).

## Feature Metadata

**Feature Type**: New Capability · **Estimated Complexity**: High
**Primary Systems Affected**: new `app/cadence/`; `app/promotion/{client,schemas}.py` (transport only);
`alembic/` (`0005_cadence`, env.py registration); `app/main.py`, `app/cli.py` (registration lines)
**Dependencies**: none new (`zoneinfo` is stdlib; `python:3.12-slim` ships tzdata)

## Related Work

**Implements**: T11 (`docs/tickets/local-prospect-engine.md`) · **Epic**: `docs/local-prospect-engine.architecture.md` → *Cadence*

**Back-references**: `.claude/plans/t2-vertical-manifest.md` (slice layout, repository-flushes/service-commits,
CLI pattern) · `.claude/plans/t3-hubspot-gateway.md` (client, retry table, MockPortal, the activity-read
sizing note).

**Forward-references**: T13 adoption builds on `enrol(start, anchor_at)` + `OutcomeReader`; T9 calls
`enrol()`; T10 may call `CadenceService.sync()`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `app/promotion/client.py` — `_request(... retry_on_server_error=...)`, `create_task` (ungated), `API_VERSION`.
- `app/promotion/schemas.py` — response models `extra="ignore"`, request models `extra="forbid"`, split aliases.
- `app/promotion/README.md` — retry table; the activity-read sizing note (association walk + batch read).
- `tests/promotion/conftest.py` — `MockPortal` (raises on any unrouted request), `json_responder`, `make_client`.
- `app/manifests/{models,repository,service,exceptions,routes,cli}.py` — the house slice pattern.
- `app/cli.py`, `app/main.py` — registration points.
- `tests/conftest.py` — `requires_db`, `db_session` (create_savepoint, service may commit), `client_with_db`.
- `tests/manifests/conftest.py` — committed-row fixtures for CLI tests.
- `alembic/versions/0002_vertical_manifest.py` — hand-written migration style; `alembic/env.py` model registration.
- `tests/test_structure.py` — no `Any`, no suppressions, single migration head, no empty packages.

### New Files to Create

```
app/cadence/__init__.py  README.md  exceptions.py  machine.py  schemas.py  models.py
app/cadence/repository.py  sync.py  service.py  routes.py  cli.py
alembic/versions/0005_cadence.py
tests/cadence/__init__.py  conftest.py  fixtures/*.json
tests/cadence/test_machine.py  test_policy.py  test_reader.py  test_models.py
tests/cadence/test_service.py  test_routes.py  test_cli.py
tests/promotion/test_reads.py  (+ fixtures for batch read / associations)
```

### API facts this plan depends on

- **Batch read** `POST /crm/objects/{v}/{type}/batch/read`, body `{"properties":[...],"inputs":[{"id":...}]}`,
  ≤100 inputs, response `{"status":"COMPLETE","results":[{id,properties,createdAt,updatedAt}]}`; ids that
  do not exist (or are archived) are simply absent (HTTP 207 with `errors`). It is a POST but a **read**, so
  `retry_on_server_error=True`.
- **Associations** `GET /crm/objects/{v}/contacts/{id}/associations/{calls|emails|notes|meetings}?limit=500`
  → `{"results":[{"toObjectId":123,"associationTypes":[...]}],"paging":{"next":{"after":"..."}}}`. Parse
  `toObjectId` (int or str) — and tolerate the older `id` key, since the shape is not exercised live.
- Task properties read: `hs_task_status`, `hs_task_completion_date`, `hs_timestamp`. Engagement time:
  `hs_timestamp` on calls, emails, notes and meetings. Datetimes come back as ISO-8601 strings; parse epoch
  ms defensively too.
- Engagements are gated by the contacts scopes T3 already lists — no new scope.

### Patterns to Follow

- Logging: `cadence.<component>.<action>_<state>` e.g. `cadence.sync.touch_done`, `cadence.service.prospect_enrolled`.
- Exceptions: `CadenceError(LocalProspectEngineError)`; `AlreadyEnrolledError` 409. Routes never catch.
- Pydantic: frozen value models; `from_attributes=True` response models.
- Timezone-aware datetimes everywhere; reject naive.

---

## THE STATE MACHINE (decided here)

Positions in order — `(cycle, touch)`:
`(1,call) (1,voicemail) (1,email) (2,call) (2,voicemail) (2,email) (3,call) (3,voicemail) (3,email)` → **parked**.

| Next position | HubSpot task type | Due |
|---|---|---|
| (1, call) at enrolment | CALL | end of the local (America/Chicago) day of enrolment |
| (n, voicemail) | CALL | end of the local day the call was done — "same day" |
| (n, email) | EMAIL | end of the local day the voicemail was done — "same-day email" |
| (n+1, call) | CALL | end of the local day **four days after** the email was done |
| after (3, email) | — | **parked**, no task |

"End of the local day" = 23:59 America/Chicago, stored UTC. A touch is **overdue** when it is live and
`due_at < now`.

## THE DONE-SIGNAL (D5), precisely

Each live row carries an **anchor** `(anchor_at, anchor_ref)`: the point after which logged activity counts
toward the current touch. Activities are totally ordered by `(hs_timestamp, ref)` where `ref = "<type>:<id>"`.
An activity counts iff it matches the touch's kinds and `(ts, ref) > (anchor_at, anchor_ref or "")`.

- The anchor at enrolment is the enrolment instant, which precedes the first task's creation; afterwards it
  is the signal that closed the previous touch, which precedes the next task's creation. So "logged after
  that task's creation timestamp" (D5) is always covered, and an activity logged in the gap between the
  previous touch closing and the next task existing is also credited — that is exactly the **"already done
  by hand advances rather than re-fires"** case.
- **Ties break toward done**: an activity at exactly the anchor instant counts (`""` sorts below any ref).
- **Each activity is credited to at most one touch**: the anchor moves to the credited activity, and the
  total order makes the move strictly monotonic.

Kinds per touch: **call** ← call, meeting, note · **voicemail** ← call, note · **email** ← email, note.
(Notes count because E14 shows the founders log touches as NOTE objects; see Decisions.)

Signal for the current touch:
1. Task COMPLETED **and** a matching activity exists → one act, recorded twice: credit the earliest matching
   activity; done at `min(completed_at, activity_ts)`; anchor → that activity.
2. Task COMPLETED, no matching activity → done at `completed_at` (`hs_task_completion_date`, falling back to
   the task's `updatedAt`, then `now`); anchor → `(completed_at, None)`.
3. Not completed, matching activity → done at the activity; anchor → that activity.
4. Neither → not done.

After a touch closes, the **next** touch is checked against the same activities (no task yet) before a task
is created — repeat until a touch is not done, then create **one** task for it, or park. Bounded at 9 steps.

**Idempotency** is structural: a task is created only when the position advances, and every live row always
has exactly one open cadence task. A second sync with nothing new in HubSpot creates nothing.

**Failure isolation**: the plan for a prospect is computed purely first; the only remote write is the one
`create_task`. If it raises, nothing in the DB was mutated for that prospect — it is reported and retried
next sync. Each prospect commits separately. A HubSpot read failure for one prospect is reported, not fatal.

**A missing task** (deleted/archived in HubSpot): not recreated, not advanced on its own; reported in
`missing_tasks`. A matching activity can still close the touch.

**A superseded open task** (touch closed by an activity while its task is still open): left untouched,
reported in `open_tasks_superseded` for the human. We never write status to the founder's tasks.

---

## STEP-BY-STEP TASKS

### 1. UPDATE `app/promotion/schemas.py`
- ADD `ObjectType` members `calls`, `emails`, `notes`, `meetings`; update docstring.
- ADD request `BatchReadInput{id}`, `BatchReadRequest{properties, inputs}`; response `BatchReadResponse{results}`,
  `AssociatedObject{to_object_id via toObjectId|id → str}`, `AssociationsPage{results, next_after}` (parse `paging.next.after`).
- VALIDATE: `uv run mypy app/promotion && uv run pyright app/promotion`

### 2. UPDATE `app/promotion/client.py`
- ADD `batch_read(object_type, ids, properties) -> list[HubSpotObject]`: dedupe ids, chunk 100, `retry_on_server_error=True`, empty ids → no request.
- ADD `list_associated_ids(from_type, object_id, to_type) -> list[str]`: GET with `limit=500`, follow `after`, cap pages (safety: 20).
- Transport only; no cadence vocabulary. Update module/README "Deliberately absent → Activity reads" to say they now exist and where the policy lives.
- VALIDATE: `uv run pytest tests/promotion -q`

### 3. CREATE `tests/promotion/test_reads.py` + fixtures `tasks_batch_read.json`, `associations_calls_page1.json`, `associations_calls_page2.json`, `calls_batch_read.json`
- dated paths; body shape; chunking at 100; empty → zero requests; 5xx retried on batch read (unlike a create); 207 with missing ids; paging followed; `id`-shaped association tolerated.

### 4. CREATE `app/cadence/exceptions.py` — `CadenceError`, `AlreadyEnrolledError` (409, `contact_id`), `CadenceStateNotFoundError` (404) only if used.

### 5. CREATE `app/cadence/machine.py` — `Touch`, `CadenceStatus`, `CadencePosition` (frozen, validated cycle 1..3), `FIRST_POSITION`, `CYCLES=3`, `WAIT_DAYS=4`, `CADENCE_TZ`, `next_position`, `end_of_local_day`, `due_for_first`, `due_after`, `task_for(position) -> TaskCreate` (subject/body/type; email body states it is a draft, sent by hand, never by the system, and the never-lead-with-AI constraint; no generated copy).

### 6. CREATE `app/cadence/schemas.py` — `ActivityKind`, `TaskSnapshot`, `Activity`, `Signal`, `AdvancePlan`, `OverdueTouch`, `SyncFailure`, `SyncReport`, `CadenceStateResponse`.

### 7. CREATE `app/cadence/sync.py` — `MATCHING_KINDS`, `find_signal`, `plan_advance` (pure), `OutcomeReader(client).tasks(ids)` / `.activities(contact_id, since)`; timestamp parsing tolerant of ISO and epoch ms; unparseable → skipped with a log.

### 8. CREATE `app/cadence/models.py` + `alembic/versions/0005_cadence.py` (down_revision `0003_seed_freight_and_fire`) + register in `alembic/env.py`
- Columns: id, hubspot_contact_id (unique), hubspot_company_id, hubspot_owner_id, status, cycle, touch, hubspot_task_id, due_at, anchor_at, anchor_ref, enrolled_at, updated_at, parked_at.
- Checks: status in (live, parked); touch in (call, voicemail, email); cycle 1..3; `status='parked' or (hubspot_task_id is not null and due_at is not null)`.
- Index `(status, due_at)`. Declared in the model too so `alembic check` is clean.
- VALIDATE: `uv run pytest tests/test_structure.py -q`; `alembic upgrade head` + `alembic check` against the test DB.

### 9. CREATE `app/cadence/repository.py` — `get_by_contact`, `list_live`, `list_overdue(now)`, `create`, `advance`, `park`. Flushes, never commits.

### 10. CREATE `app/cadence/service.py` — `CadenceService(session, hubspot_factory=get_hubspot_client, clock=utc_now)`: `enrol`, `sync`, `overdue`. Per-prospect commit.

### 11. CREATE `app/cadence/routes.py` — `POST /cadence/sync` → `SyncReport`, `GET /cadence/overdue` → `list[OverdueTouch]`; `get_cadence_service` dependency (overridable in tests).

### 12. CREATE `app/cadence/cli.py`; UPDATE `app/cli.py` (import + register + dispatch branch) and `app/main.py` (import + include_router).

### 13. CREATE `app/cadence/README.md` and `app/cadence/__init__.py`.

### 14. Tests (`tests/cadence/`)
- `conftest.py`: `FakePortal` — a stateful, schema-accurate HubSpot built on `MockPortal` (tasks create/batch-read, associations, engagement batch-read), sharing a controllable clock; helpers to complete a task / log an activity.
- `test_machine.py`: nine-step walk ends parked; due rules incl. the 4-day wait and DST end (2026-11-01); task types; email body is a draft and never claims to send.
- `test_policy.py`: each D5 branch; tie at anchor counts; before-anchor ignored; anchor_ref excluded; non-matching kinds ignored; completed+activity credits the activity; earliest wins; multi-step "already done by hand"; bounded at park.
- `test_reader.py`: OutcomeReader against fixtures; missing tasks absent; timestamps parsed.
- `test_models.py` (requires_db): check constraints and unique contact.
- `test_service.py` (requires_db): enrol; duplicate enrol 409; **idempotent second sync**; **full three-cycle walk to park**; **already done by hand advances**; overdue surfaced; create-task failure leaves state and is reported; missing task reported; superseded open task reported; parked rows untouched by sync.
- `test_routes.py` (requires_db): sync and overdue through the app with the service dependency overridden.
- `test_cli.py` (requires_db): `lpe cadence overdue` on committed rows; `lpe cadence sync` with the HubSpot factory patched to the fake portal; no traceback on a missing token.

---

## VALIDATION COMMANDS

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/lpe_t11
uv run ruff check . && uv run ruff format --check .
uv run mypy . && uv run pyright
uv run pytest -rs            # DB tests must run, not skip
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head && DATABASE_URL=$TEST_DATABASE_URL uv run alembic check
DATABASE_URL=$TEST_DATABASE_URL uv run alembic downgrade 0003_seed_freight_and_fire && DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
```
No live HubSpot call of any kind: every HubSpot test goes through `MockPortal`, which raises on unrouted requests.

## ACCEPTANCE CRITERIA

- [ ] AC1 `cadence_state` + `0005_cadence` migration: schedule only — no outcome columns.
- [ ] AC2 call → voicemail → same-day email draft → wait 4 days → ×3 cycles → parked.
- [ ] AC3 Touches are HubSpot Tasks; the email touch is a draft task, nothing sends; task creation ungated.
- [ ] AC4 D5 done-signal: task complete OR matching activity after the anchor, ties toward done; outcomes read, not stored.
- [ ] AC5 Overdue touches surfaced (sync report, route, CLI).
- [ ] AC6 Idempotent: second same-day sync creates nothing.
- [ ] AC7 Tests: full three-cycle walk incl. park; already-done-by-hand advances; fixtures only.
- [ ] AC8 Transport reads in `app/promotion/client.py`; policy in `app/cadence/`.
- [ ] AC9 ruff, mypy, pyright, pytest green; zero suppressions; no `Any`; DB tests ran.

## OPEN QUESTIONS / ASSUMPTIONS — conservative readings taken (→ PR "Decisions for review")

1. **Voicemail is its own touch/task** (literal reading of call → voicemail → email), CALL-typed, due the same day.
2. **Notes and meetings count as matching activity** (E14: touches are logged as notes). Each activity credits at most one touch. Leans toward done, per D5's stated preference.
3. **A completed task and a matching activity are one act** — the activity is consumed so one call does not close two touches.
4. **Skipped human touch → no escalation, no auto-skip.** PRD §9 *Autonomy, revisited* is open; the touch stays open and is surfaced as overdue.
5. **No exit on a reached conversation.** Not in the ticket; the machine runs three cycles. A human-facing stop is a follow-up.
6. **Superseded open tasks are reported, not auto-completed** — we never write to the founder's tasks.
7. **Missing (deleted) tasks are reported, not recreated.**
8. **Email draft task carries no copy** — opener rewrite is open (PRD §9, E16); the body states the constraints only.
9. **Due = 23:59 America/Chicago**; DFW-only per Non-goals, so a module constant, not config.
10. **No enrol route/CLI**: enrolment without T13's reconstruction would reset a cycle.
11. **Task owner** is whatever the caller passes to `enrol` (stored, applied to every task); unassigned otherwise.
12. **Migration `0005_cadence` → `0003`** here; rebased onto T4's `0004_sourcing` at merge.

## NOTES

Rejected: creating the next cycle's call only when due (needs a sync on that exact day and hides the
schedule from HubSpot); engagement *search* filtered by `associations.contact` (unconfirmed, 5 req/s,
index lag — T3's note); storing the full set of credited activity ids (the total order makes one anchor
sufficient); auto-completing superseded tasks (a write to human data nobody asked for).

Sizing: per live prospect per sync ≈ 4 association GETs + ≤4 batch reads; tasks batch-read once per 100.
At ~22–100 prospects that is well under the 100/10 s budget with the header throttle.

## AMENDMENTS
