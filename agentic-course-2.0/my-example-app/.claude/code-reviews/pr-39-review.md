# PR #39 review — feat(runs): weekly run, its trigger, and the Friday report (T10)

**Recommendation: Approve, with one Medium worth fixing before the first real run.** No Critical or High.
Validation is green on the PR head (`a95fc92`). The one Medium code finding is that a reaped run can
keep going into promotion; it is cheap to close. The funnel gap is the founder's call, already disclosed.

## Summary

The PR delivers T10 as planned. One weekly run chains sourcing and promotion, triggered by `POST /runs`
(202, background task) or `lpe runs start`. It records M7 on the database clock, keeps one run at a time
with a partial unique index plus a compare-and-set finish and a 2h reaper, and reports the week on Friday
from prefixed HubSpot notes.

Checked and sound:
- **The 409 mapping** rolls back and maps only `uq_weekly_run_one_running`, in the same way
  `CadenceService` matches `UNIQUE_CONTACT`. Two concurrent inserts are serialised by the index: the second
  waits for the first to commit, then gets the 409 naming it.
- **Background failures:** an `Exception` is recorded as `failed` and swallowed; a `BaseException`
  (cancellation) is recorded, then re-raised. If the record itself cannot be written, the original error
  surfaces (`raise exc from None`), so `_fail` never masks it.
- **`on_started`** runs after `start_run` commits and inside the `try`, so a stage's rollback cannot undo
  the link, and a raising callback fails the sourcing run on a fresh session. Two tests pin this.
- **Migration `0011`** matches the model (columns, defaults, four CHECKs, the unique constraint, the
  partial index's `postgresql_where`, the FK); downgrade drops indexes then the table. `alembic check` is
  clean.
- **The week window** is built from local midnights in `America/Chicago`, half-open, with exact epoch-ms
  integers. Paging follows `after`, capped at 20 × 200, well under HubSpot's 10,000-result search limit,
  and refuses past the cap instead of truncating.
- **No note text** is logged. `PlaybookNumber` makes paid-sold-as-0 unconstructable.
- **Routes:** `/weekly-report` is declared before `/{run_id}`. The background service uses the session
  factory, never the request session. Only the CLI disposes the engine and the HubSpot client.
- **House rules:** services commit, repositories flush; DB-clock timestamps; no HubSpot writes outside
  `PromotionService`; no suppressions, no `Any`.

The documented deviations (the plan's AMENDMENTS and the report) are intentional and not flagged.

## Issues

### Critical

None.

### High

None.

### Medium

**M1 — A reaped run carries on into promotion and writes to HubSpot**
(`app/runs/repository.py:69-73`, `app/runs/service.py:171-181`).

The plan covers a reaped run's *finish*: the compare-and-set returns `None` and the run logs
`finish_lost`. It does not cover the work before the finish. `mark_sourced` is an unconditional
`update … where id`, and `_execute` never re-reads its row before `promote_run`.

**Failure scenario:** a run is still alive past 2h (a slow Places or SDK pass, or the Mac asleep
mid-run). An ad hoc `lpe runs start` reaps it to `failed` and starts a second run. The first run then
finishes sourcing, stamps `sourced_at` on a `failed` row, and calls `promote_run`, creating Companies and
Contacts in HubSpot. Two runs are live at once, which is what the index exists to prevent. The reaped row
reads `failed` although it wrote to HubSpot, and its promotion counts are never recorded. The promotion
ledger keeps the HubSpot side consistent (no duplicate records), so this is an accounting and guard
problem, not data loss.

**Fix:**
- Make `mark_sourced` a compare-and-set (`where status = 'running'`, `returning`) that returns the row or
  `None`.
- In `_execute`, on `None`, return `await self._lost(run_id)` without promoting.
- Test: start, reap (or flip the row to `failed`), then `execute`; assert `promote_run` is never called
  (the existing spy) and the row stays `failed`.

This cannot stop a run already inside `promote_run`. That window is small against a 60-minute target; if
it matters later, a heartbeat column is the fix, not a longer stale window.

### Low

**L1 — The funnel cannot be reconciled** (`app/runs/schemas.py`, `FUNNEL_COUNTERS`). Disclosed in the PR;
listed so the decision is not lost. Without `places_not_found` and `routing_unverified_address`, a dev dry
run showed "batched 5, disqualified 4" and then zeros. Adding the two names is the plan's list amended,
which is the founder's call. (`PROMOTION_COUNTERS` also omits `refused_dead_score` and `already_promoted`,
matching the plan.)

**L2 — `classify_note` has no word boundary** (`app/runs/touches.py:50-53`). "Door knocking planned
Thursday" and "Owner conversations to book" count as a door knock and an owner conversation. Fix: accept
the prefix only when it ends the text or the next character is not alphanumeric, and add both cases to
`test_touches.py`.

**L3 — `lpe runs start --vertical Freight` prints a traceback** (`app/runs/cli.py:108-113`).
`WeeklyRunRequest` is built in `dispatch`, and its `ValidationError` is neither a
`LocalProspectEngineError` nor an `IntegrityError`, so `app/cli.py` does not catch it. Fix: catch
`ValidationError` in `dispatch` and print one `error:` line with exit 1.

**L4 — `lpe runs show <id>` exits 1 for any past failed run** (`app/runs/cli.py:144-152`). `_emit` is
shared by `start` and `show`. Inspecting a failure is not a command failure; only `start` should map the
run's status to the exit code.

**L5 — New SQLAlchemy deprecation warning** (`app/promotion/repository.py:154`). `status_counts_by_run`
uses `result.tuples()`, deprecated in 2.1; it is the only use in `app/`. Fix: iterate `result` directly,
then re-run mypy and pyright.

**L6 — `.claude/skills/piv-validate/SKILL.md` still says port 5433.** Disclosed; a one-line doc fix.

## Validation

Run on the PR head `a95fc92`, test Postgres on 5434:

| Check | Result |
|---|---|
| `ruff check .` | pass |
| `ruff format --check .` | pass (230 files) |
| `mypy .` | pass (233 files) |
| `pyright` | 0 errors, 0 warnings |
| `pytest` | **1855 passed, 4 skipped** (the paid live evals), 7 warnings (one new: L5) |
| `alembic upgrade head` + `alembic check` | head `0011_weekly_run`, no drift |
| Suppressions (`type: ignore`, `pyright: ignore`) | none |

## What's good

- **The guard is designed, not bolted on:** an index, a compare-and-set finish and a reaper, with the
  reason it is not an advisory lock (the work outlives the request) written in the model's docstring.
- **Failure semantics are explicit and tested.** Degraded sourcing still promotes; failed sourcing promotes
  nothing (a spy proves it); a stopped or partly failed promotion is `degraded` with every reason kept.
- **The `on_started` amendment** came from a failing test and is documented with its cause.
- **`PlaybookNumber`** puts "not configured" in the type, so the 0-versus-unmeasured mistake cannot be made.
- **`touches.py`** is a pure core tested against the portal's real notes, the DST week, and the
  Monday-03:00-UTC boundary.
- **The report fails loudly** on a page-cap overrun or a HubSpot error, never returning a partial week.
- **`configured_stages` takes its stage list as an argument,** so the `lpe sourcing run` tests stay
  untouched.

## Recommendation

**Approve.** Fixing M1 before the first real run is recommended; it is a small change with a test.
L2–L5 can ride along; L1 waits on the founder. Posted as a comment: GitHub does not let a PR's author
approve their own PR, so the human merge is the approval.
