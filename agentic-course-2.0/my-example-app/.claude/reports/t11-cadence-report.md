# Implementation Report — T11: Cadence slice, the three-touch state machine

**Plan**: `.claude/plans/t11-cadence.md`   **Branch**: `feat/t11-cadence`   **Status**: COMPLETE

## Summary

`app/cadence/` now owns the cadence that Sales Hub Starter cannot hold: **call → voicemail → same-day
email draft → wait four days → ×3 cycles → parked**. `cadence_state` stores the schedule only (which
touch, which cycle, live/parked, due date, plus the anchor cursor the done-policy needs). Every touch is a
HubSpot **Task**; the email touch is an EMAIL-typed task to draft and send by hand, so nothing sends. A sync
re-reads task status and the contact's logged calls/emails/notes/meetings and applies D5 as a pure function,
advancing through anything already done by hand before creating exactly one task. Overdue touches come back
in the sync report, `GET /cadence/overdue` and `lpe cadence overdue`. T3's gateway gained two transport-only
read methods.

## Meta

- **Files added (29):** `app/cadence/{__init__,README.md,exceptions,machine,schemas,sync,models,repository,service,routes,cli}`,
  `alembic/versions/0005_cadence.py`, `tests/cadence/{__init__,conftest,test_machine,test_policy,test_reader,test_models,test_service,test_routes,test_cli}.py`,
  `tests/promotion/test_reads.py`, six fixtures in `tests/promotion/fixtures/`, `.claude/plans/t11-cadence.md`.
- **Files modified (6):** `app/promotion/client.py` (+`batch_read`, +`list_associated_ids`),
  `app/promotion/schemas.py` (engagement `ObjectType`s, batch-read and associations models),
  `app/promotion/README.md`, `alembic/env.py` (model registration), `app/main.py` (+2 lines),
  `app/cli.py` (+4 lines).
- **Lines changed:** +3780 / −10 (before this report).

## Validation results

| Check | Command | Result |
|---|---|---|
| Lint | `uv run ruff check .` | ✓ all checks passed |
| Format | `uv run ruff format --check .` | ✓ 79 files already formatted |
| Types | `uv run mypy .` | ✓ no issues in 78 source files |
| Types | `uv run pyright` (strict) | ✓ 0 errors, 0 warnings |
| Tests | `uv run pytest -rs` with `TEST_DATABASE_URL` set | ✓ **346 passed, 0 skipped** (234 baseline + 112 new) |
| Structure | `tests/test_structure.py` | ✓ no `Any`, no suppressions, one migration head |
| Migration | `alembic upgrade head` · `alembic check` · `downgrade 0003` · `upgrade head` (throwaway DB) | ✓ "No new upgrade operations detected" |

New tests per file: `test_policy` 29 · `test_service` 21 · `test_machine` 18 · `test_reader` 13 ·
`test_models` 11 · `test_reads` 11 · `test_routes` 5 · `test_cli` 4. No live HubSpot call of any kind —
every HubSpot request goes through `MockPortal`, which raises on unrouted requests.

## What went well

- Keeping the done-policy pure (`find_signal`, `plan_advance`) made the hard part — D5, ties, consumption,
  "already done by hand" — testable in 29 table-style cases with no database or portal.
- The anchor as a **total order** `(hs_timestamp, "<type>:<id>")` replaced a set of credited ids: one cursor
  guarantees each activity closes at most one touch, including two activities in the same millisecond.
- "Compute the plan, then the one remote write, then mutate and commit" means a failed task create leaves the
  ORM row untouched, so no rollback was needed mid-loop (and no expired-instance lazy load in async).
- A stateful `FakeHubSpot` on top of T3's `MockPortal` let the full nine-touch walk run against the real
  service, repository and database in milliseconds with a hand-moved clock.
- Existing guard tests earned their keep: `test_logging`'s event-name check caught a non-conforming event.

## Challenges

- **D5 vs. the "already done by hand" case.** D5 anchors on the task's creation; a touch with no task yet
  has nothing to anchor on. Resolved by anchoring on the previous touch's signal, which always precedes the
  next task's creation, so D5's window is a subset of what is counted.
- **One act, two records.** A founder who ticks the call task and also logs the call would close two touches
  under a naive OR. Resolved by treating completion + matching activity as one act that consumes the activity.
- **Unverified wire shapes.** The dated associations endpoint's body was not exercised live (no live calls
  allowed). `AssociatedObject` accepts both v4 `toObjectId` and the v3 `id` key.

## Divergences from plan

**Fixtures location**
- Planned: `tests/cadence/fixtures/*.json`.
- Actual: recorded fixtures live in `tests/promotion/fixtures/` beside T3's, and the cadence suite uses a
  stateful `FakeHubSpot` serving the same shapes.
- Reason: the fixtures pin transport (the client's methods), which is promotion's; the walk needs state a
  static fixture cannot hold. Type: Better approach found.

**`enrol(due_at=...)`**
- Planned: `enrol(start, anchor_at)` as the T13 seam.
- Actual: also `due_at`.
- Reason: a reconstructed position has a due date implied by history, not "today". Type: Plan assumption wrong.

**No `CadenceStateNotFoundError`**
- Planned: "only if used". Nothing needed it. Type: Other.

**`lpe cadence sync` exits 1 when any prospect failed**
- Not in the plan. A launchd job should see a partial failure; the report still prints. Type: Better approach.

**Event renamed** `promotion.hubspot.association_pages_capped` → `promotion.hubspot.associations_capped` to
satisfy the `domain.component.action_state` guard. Type: Other.

**`machine.py` and `cli.py`** are not in the ticket's file list (`{models,schemas,repository,service,sync,routes,exceptions,README}`);
both were in the plan. `machine.py` keeps the transitions pure and reusable by T13; `cli.py` gives launchd a
trigger that needs no running server and gives the founder the overdue list in the terminal.

## Skipped items

- Live smoke test (boot + curl): skipped — routes, error handlers and the CLI are exercised in-process
  against the real app and a real Postgres; no startup code changed beyond one `include_router`.
- `CLAUDE.md` architecture map and `_tasks/todo.md`: not edited, to avoid conflicts with T4/T12 in sibling
  worktrees. The map's "Today" line should gain `app/cadence/` at merge.

## Decisions for review (conservative readings, not silently answered)

1. Voicemail is its own touch and task (literal reading of the ticket).
2. Notes count as evidence for every touch; meetings for the call (E14/E17).
3. Completed task + matching activity = one act.
4. No escalation or auto-skip when a human touch is skipped — it stays open and overdue (PRD §9 *Autonomy*, open).
5. No exit on a reached conversation (not in T11).
6. Superseded open tasks are reported, never auto-completed — no writes to the founder's tasks. Note this
   leaves M5's task-completion measure understating cadence completion when touches are closed by activity.
7. Deleted tasks are reported, not recreated.
8. Email draft task carries no copy — opener rewrite is open (PRD §9, E16).
9. Due = 23:59 America/Chicago, a constant (DFW-only).
10. No enrol route/CLI — enrolment without T13's reconstruction would reset a cycle.
11. Task owner = whatever the caller passes to `enrol`; unassigned otherwise.
12. Migration `0005_cadence` → `0003_seed_freight_and_fire`; rebase onto `0004_sourcing` at merge.

## Recommendations

- **Plan skill:** when a ticket's acceptance criterion quotes a rule (D5) that has an undefined case (no task
  yet), make "enumerate the undefined cases of every quoted rule" an explicit planning step.
- **CLAUDE.md:** at merge, add `app/cadence/` to *Today* and note that `alembic/env.py`'s
  `_REGISTERED_MODELS` is a shared append point for parallel slices.
- **Before T13 or T9 ship:** one read-only live call to confirm the associations body shape, and a decision
  on owner assignment so tasks land in a founder's "My tasks".
