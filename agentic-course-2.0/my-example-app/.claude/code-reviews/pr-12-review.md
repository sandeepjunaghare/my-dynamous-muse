# PR #12 review: `lpe cadence park` and `sync --dry-run`

**Recommendation: approve after the follow-ups below.** No Critical or High issues. The decide/apply split
behaves the same for a real sync, the dry run writes nothing, and `park` respects the database constraints
and the lock. One Medium finding: a dry run running alongside a real sync can make a false "deleted"
claim. The tests also miss the riskiest path of the refactor.

The review was done in a fresh context by the `code-reviewer` agent, reading every changed file in full.
The agent had no shell, so the author ran validation on the PR head (`29e3576`).

## Validation

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean · 100 files formatted |
| `mypy .` | no issues, 99 files |
| `pyright` | 0 errors, 0 warnings |
| `pytest`, with a database (Postgres 16) | **562 passed** (the 2 warnings are already on `main`) |
| `pytest`, without one | 382 passed, 180 skipped |
| `alembic check` | no new operations (no migration) |

## Issues

### Medium

**M1. A dry run alongside a real sync can claim a task was deleted.** `app/cadence/service.py` `_run` /
`_plan_one`
- The task batch is read once, from the `list_live` snapshot. Each row is then re-read with
  `populate_existing`.
- Scenario: a real sync advances prospect A between those two reads, and attaches T2. The dry run's batch
  holds only T1, so `tasks.get(T2)` is `None`, `task_missing` comes out `True`, and the CLI prints "its
  cadence task was deleted in HubSpot — not recreated".
- That is a false statement a person could act on. The docs say a dry run "can run while a real sync does".
- *Confirmed against the code by the author.*
- **Fix:** in dry-run mode, plan from the `list_live` row instead of re-reading it, so the batch and the row
  come from the same moment.

### Low

**L1. The riskiest path of the refactor has no test.**
- The equivalence test only covers tick plus notes, and compares only the count and the next position.
- Nothing runs a pending row with **no task found** through a dry run and then a real sync. On that path,
  `_decide` now plans before `_resolve_pending` creates the task.
- The plans are identical by reasoning (`find_signal` only tests `task.completed`), but no test pins it.
- **Fix:** add that test. Assert step sources, `next_due_at`, `tasks_created` and `open_tasks_superseded`
  agree.

**L2. The "no `touch_done` in a dry run" promise is untested.**
- It holds today, because the event is emitted only in `_sync_one`.
- **Fix:** assert it with `structlog.testing.capture_logs`.

**L3. The park test can't detect a leaked lock.**
- `test_park_finishes_the_cadence_and_leaves_the_task_alone` checks `sync().checked == 0`. A sync skipped
  because `park` leaked the lock also gives 0.
- **Fix:** assert `skipped is False` too.

**L4. `park` reads through the identity map.**
- `get_by_contact` has no `populate_existing`, so a caller reusing a session could check a stale status under
  the lock. The CLI uses a fresh session, so this is safe today.
- **Fix:** re-read inside `park`.

**L5. The dry run under-reports a superseded task on the "would create" path.**
- When a pending row has no task found and an activity closes the touch, the real sync creates the task and
  then lists it as superseded. The dry run lists nothing.
- **Fix:** report it, or say so in the dry-run output. (Creating a task only to supersede it is a separate
  behaviour question.)

**L6. Counter and wording nits.**
- A row skipped as no-longer-live counts as `checked`, and the dry-run summary then shows it as unchanged.
- `touch_overdue` and `prospect_failed` are still logged in a dry run.
- `assert "note" in out` is a weak assertion.
- The pending-key warning line and the `SyncRunningError` CLI message are untested.

## Scrutiny points

- **Decide/apply equivalence:** holds. `task=None` and a fresh open snapshot give the same plan. Superseded
  reporting still sees the created task, and `task_missing` still cannot fire on the pending path. One benign
  change: activities are read before the create, so a read failure no longer leaves a created task behind.
- **The dry run writes nothing:** confirmed. Only reads; no commit, flush, attach, create or `touch_done`.
- **`park` and the lock:** the advisory lock is on its own connection, and parking satisfies both check
  constraints.
- **Type safety, errors, naming:** nothing found. No suppressions, the right status codes, and event names
  fit the convention.

## What's good

- The `_decide`/apply split is clean and small. A frozen `_Decision` makes "a dry run is the sync minus the
  writes" true by construction.
- The dry-run output names what closed each touch.
- `park` is honest: it never reaches HubSpot, leaves the founder's task alone and warns about an interrupted
  create.
- The CLI tests commit through a separate engine, so they genuinely prove persistence.
- The docs moved with the code.

## Next

Fix M1 and L1 with `piv-fix-review-findings`, re-run the suite with the database, then a human merges. L2–L6
are cheap and can ride along.

---

## Fixes (round 1)

Each fix has a test that failed first, against a real Postgres.

| Finding | Outcome | Test |
|---|---|---|
| **M1** | **Fixed.** A dry run plans from `_RowView`, plain values captured from `list_live` in the same read as the task batch. It no longer re-reads the row. A real sync still re-reads, under the lock | `test_a_sync_advancing_mid_dry_run_is_not_reported_as_a_deleted_task`: a sync lands between the batch read and the plan |
| **L1** | **Fixed.** The pending, no-task, closed-by-a-note path runs as a dry run, then a real sync. Steps, sources, `next_due_at`, `tasks_created` and superseded all agree | `test_a_pending_row_closed_by_a_note_plans_what_the_sync_then_does` |
| **L2** | **Fixed** (test). A dry run logs no `touch_done` | `test_a_dry_run_logs_no_touch_done_and_no_overdue_warning` |
| **L3** | **Fixed** (tests). The park tests assert `skipped is False` after a park and after both refusal paths | the three `TestPark` tests |
| **L4** | **Fixed.** `get_by_contact` re-reads with `populate_existing` | `test_park_sees_a_park_its_session_has_not_loaded`. The first draft passed on the old code, because an ORM `update()` also refreshes the session's copy. It now uses `synchronize_session=False` |
| **L5** | **Fixed.** `ProspectPlan.pending_task_superseded`, and a CLI line | covered by the L1 test |
| **L6** | **Fixed:** a row skipped as no-longer-live is not counted as `checked`; a dry run does not log `touch_overdue`; the dry-run summary counts `unchanged` from the plans; the weak `"note"` assertion is tightened; the `SyncRunningError` CLI line is tested. **Not done:** a CLI test of the pending-key warning (needs a committed pending row; the service test covers the value) | `test_park_while_a_sync_runs_is_one_error_line` |

**Validation after fixes:** ruff and format clean, mypy and pyright clean, **567 passed with the database**,
382 passed / 185 skipped without, `alembic check` clean.
