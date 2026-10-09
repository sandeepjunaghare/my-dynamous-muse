# Code Review: PR #9, T11 cadence slice (the three-touch state machine)

**Reviewed**: `feat/t11-cadence` → `main` at `6446db7`. 36 files, +3902/−10. The worktree HEAD matches the PR head.

**Method**: a fresh-eyes pass in a clean context, with the `code-reviewer` agent running the deep pass in parallel. Every changed file was read in full. The full validation suite was run with and without a throwaway Postgres. The three policy findings were also reproduced against the pure functions with a scratch script, so they are verified rather than inferred. **No live HubSpot call of any kind was made.**

**Recommendation: request changes. Fix the two High findings before merge.** The pure state machine, the timezone math and the schedule/outcome split are all sound, and validation is green on every gate. Two things are not yet true, though:

- **"Idempotent by construction" holds only for sequential syncs where nothing fails.** A task create that HubSpot executed but answered with a 5xx produces a duplicate task on the next sync.
- **The done-signal can credit the wrong touch.** It then re-fires a task for work that is already done and logged, which breaks the ground rule that the machine never nags about something already handled.

Both are small, local fixes. Posted as a comment, not a review verdict: the human merge decision is the gate.

## Validation

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 79 files |
| `uv run mypy .` | pass, 78 source files |
| `uv run pyright` | 0 errors, 0 warnings, 0 informations |
| `uv run pytest` (no DB) | 262 passed, 84 skipped. Each skip prints the command that un-skips it. |
| `TEST_DATABASE_URL=…5434/lpe_t11 uv run pytest` | **346 passed**, 0 skipped, 2 warnings |
| New tests only (`tests/cadence`, `tests/promotion/test_reads.py`) | 112 tests: 71 pass and 41 skip without a DB, all pass with one |
| `alembic heads` (throwaway DB) | one head: `0005_cadence` |
| `alembic check` (throwaway DB) | "No new upgrade operations detected" |
| Suppression scan (`type: ignore`, `pyright: ignore`, `noqa`, `Any`) in `app/`, `tests/`, `alembic/` | **zero**. The only `Any` hits are in prose: docstrings and comments in pre-existing files. |

The 2 warnings are Starlette's `HTTP_422_UNPROCESSABLE_ENTITY` deprecation from T1. That finding is already tracked.

## What the review verified, rather than took on trust

- **Each activity closes at most one touch, within one sync.**
  - `order_key = (hs_timestamp, "<type>:<id>")` is a total order, and `_is_after` is strict.
  - `find_signal` always credits the minimum candidate, so every remaining activity at the same instant still sorts after the new anchor.
  - The "consumed activity never counts again" claim holds. The exceptions are in H2, where the problem is *which* activity gets consumed.
- **The 9-step bound is right.** `plan_advance` returns at park from any start position, so `range(TOTAL_TOUCHES)` can never be the binding limit for a valid position.
- **Due-date and DST math is correct.**
  - `datetime.combine(day, 23:59, tzinfo=ZoneInfo)` cannot land in a DST gap or fold, because Chicago's transitions happen at 02:00.
  - The 4-day wait is calendar-date arithmetic before localising.
  - The 2026-11-01 test pins the fall-back case. Spring-forward has the same property for the same reason.
- **No outcomes are stored.** `cadence_state` holds position, task pointer, due date and the anchor cursor. `anchor_ref` is a pointer, not a disposition. Nothing reads or writes `hs_call_disposition`, `hs_note_body`, `hs_email_*` or similar.
- **Nothing sends.**
  - The only write method reachable from the slice is `create_task`.
  - `TestNothingSends` observes on the wire that a full walk writes only to `/crm/objects/2026-09/tasks`.
  - `TestReadOnly` asserts that the gateway has no engagement-create method.
- **The gateway reads follow the retry/throttle table.**
  - `batch_read` passes `retry_on_server_error=True`, which is correct for a read-only POST, and is tested.
  - Association GETs retry on 5xx and follow `paging.next.after` with a 20-page breaker.
  - Batch reads chunk at 100 and dedupe ids.
  - No search endpoint is used, so search pacing does not apply. Both reads go through `_request`, so they get the 429 handling and the header-driven throttle like every other call.
- **Transaction discipline.** The repository only flushes and the service commits per prospect. Because the plan is computed before the one remote write, a *failed* create leaves the ORM row untouched and nothing needs rolling back.

## Findings

### High

#### H1. A task HubSpot created but answered with a 5xx, or a crash before commit, is created again on the next sync

`app/cadence/service.py:244-257` · `app/cadence/service.py:157-169` · `app/promotion/client.py:568`

`_sync_one` calls `create_task`, then `advance()`, then `commit()`. Nothing records that the create was attempted, and there is no idempotency key or lookup. Exactly what happens:

1. **5xx or timeout on the create.** This is the case the gateway's own retry table calls ambiguous. `create_task` passes `retry_on_server_error=False` precisely because *"HubSpot may have created it and failed to answer"*.
   - `sync()` catches the resulting `HubSpotError`, reports the prospect under `failures` and leaves the row at the old position, pointing at the old, already-completed task.
   - The next sync reads the same completed task and the same activities, computes **the same plan**, and creates the task again.
   - If HubSpot had in fact created the first one, the founder now has two identical open tasks, for example two "Cadence 1/3 · voicemail". The machine tracks only the second.
   - The orphan is never superseded, never reported and never closed. Ticking it does nothing, and the tracked twin stays open and shows as overdue.
2. **Crash or DB error between `create_task` returning and `commit()`.** Examples: a process kill, a connection drop, or the per-row `UPDATE` failing.
   - The task exists in HubSpot, but the transaction rolls back, or never commits.
   - The next sync creates it again, with the same orphan as in case 1.
   - A non-`HubSpotError` here also escapes the per-prospect `try`; see M4.

The test that claims to cover this (`test_a_failed_task_create_leaves_the_schedule_alone_and_is_retried`) uses a fake that returns 500 **before** recording the task (`tests/cadence/conftest.py:130-132`). So it only models the unambiguous failure, the one case where retrying is safe.

**Fix**: make the create look before it leaps. Before `create_task`, read the contact's associated tasks with the two read methods this PR already adds: `list_associated_ids(contacts, id, tasks)` plus `batch_read` with `hs_task_subject` and `hs_task_status`. Adopt an open task whose subject equals `task_for(plan.next_position, …).subject` and whose `createdAt` is after the anchor, instead of creating a new one. The cost is one GET and one batch read, and only on the syncs that advance. It also covers most of M1. Add a fake mode that records the task and *then* answers 500, and assert that the retry adopts the task rather than duplicating it.

#### H2. A completed task can consume the *next* touch's activity, so the next task re-fires for work already done

`app/cadence/sync.py:106-121`

`find_signal`'s "one act, recorded twice" rule pairs a completed task with **any** matching activity after the anchor, however long after the completion it was logged. Notes match every touch, so this is common. When the task has no pairing activity, the anchor jumps to `completed_at`, which can skip activity logged earlier than a late tick. Both behaviours were reproduced with the pure functions:

| Sequence in HubSpot | Sync sees | Result |
|---|---|---|
| Call task ticked 10:00 · voicemail logged as a call 10:05 · email logged 10:30 · sync runs later | call: `both`, consumes the **voicemail's** call | voicemail task created for a voicemail already left. The email is not credited either, because the voicemail touch does not match an email. |
| Call task ticked 10:00 · voicemail note 10:05 · email note 10:30 | call: `both`, consumes the voicemail note · voicemail: consumes the email note | email task created for an email already sent |
| Voicemail left but not logged; its task ticked on Thursday · email logged Monday | voicemail: `task_completed`, anchor jumps to Thursday | Monday's email sorts before the anchor and is never credited, so the email task re-fires |

Each row is the machine nagging about something already handled. CLAUDE.md's ground rule is *"the machine must never nag about something already handled"*, and D5 breaks ties toward done.

There is also a race in the other direction. Tick the call task, let a sync run, then log the same call with `hs_timestamp ≥ completed_at`. The call is then credited to the voicemail. This advances early, which is tolerable under D5, but it contradicts the "each activity closes at most one touch" claim made in the README and the PR body.

**Fix (two lines of policy):**
- (a) In the `completed` branch, pair only with a matching activity whose `occurred_at ≤ completed_at`, optionally plus a small grace such as 15 minutes. Otherwise treat the completion as task-only.
- (b) On a task-only signal, **leave the anchor where it was** (`anchor_at, anchor_ref` unchanged) and use `completed_at` only for `done_at`. No activity was consumed, so there is nothing to move past, and every later activity stays eligible for the next touch.

Add the three sequences above to `test_policy.py`. Document the residual race (a call logged after the tick, with a later timestamp) as advancing early.

### Medium

#### M1. Two overlapping syncs both create the next task

`app/cadence/service.py:145-158` · `app/cadence/routes.py:37`

`list_live()` takes no lock. Two overlapping syncs can each see the completed task, each create the next one and each `UPDATE` the row; the last writer wins and the other task is orphaned as in H1. Overlap is possible between `POST /cadence/sync`, `lpe cadence sync` from launchd, and T10's run later. The route docstring says *"Safe to call as often as wanted"*, which is true only sequentially.

**Fix**: `pg_try_advisory_lock(<constant>)` at the top of `sync()`. If the lock is already held, return a report saying "sync already running". This needs no Redis, which keeps CLAUDE.md's "Redis only if a run-lock is needed" intact. H1's look-before-create covers the residue.

#### M2. A future-dated activity, such as a booked meeting, closes the current touch and pushes the anchor into the future

`app/cadence/sync.py:95-102` · `app/cadence/sync.py:253`

Candidates have a lower bound (the anchor) but no upper bound at `now`. A meeting's `hs_timestamp` is its **start time**. So a meeting booked today for eight days out closes the call *now*. Its `done_at`, eight days ahead, sets the voicemail's due date eight days out, and the anchor moves eight days into the future. Every note or call logged before then is invisible to the policy. Reproduced: one booked meeting gives `next: cycle 1 · voicemail, due 2026-10-14` for a call due 2026-10-05.

**Fix**: drop activities with `occurred_at > now` in `find_signal` (or in `OutcomeReader.activities`), and add a policy test. Whether a booked meeting should *end* the cadence is the "no exit on a reached conversation" decision below. It should not silently corrupt the schedule in the meantime.

#### M3. Same-day touches need at least a daily sync, but the docs name T10's weekly run as a trigger

`app/cadence/sync.py:174` · `app/cadence/machine.py:114-123` · `app/cadence/README.md:78` · `app/cadence/routes.py:4`

The next due date comes from the previous touch's `done_at`, which is correct for the cadence. But the voicemail and email tasks only exist once a sync has seen the previous touch close. With a weekly sync:

- every voicemail and email task is **born up to 7 days overdue**;
- `sync()` lists it as overdue in the same run;
- a 12-day cadence takes about 9 weeks.

Enrolling at 23:59:30 Dallas time also produces a task that is already overdue. Nothing documents the required sync frequency, and both the README and `routes.py` cite "T10's weekly run". The look-ahead ("already done by hand") softens this but does not remove it.

**Fix**: state in the README and the CLI help that the cadence sync must run at least daily (launchd every few hours is cheap: about 8 reads per live prospect). Separately, decide whether a task created after its touch's day should be due `max(due, end of today)`. That hides lateness but avoids a task that is overdue the moment it is created.

#### M4. Only `HubSpotError` is isolated per prospect; anything else aborts the run without a rollback

`app/cadence/service.py:157-169`

A pydantic `ValidationError` from an unexpected response body, or a SQLAlchemy error from `advance()`/`commit()`, escapes the loop. The remaining prospects are skipped, the session is left in a failed transaction, the route returns 500, and in the commit case the HubSpot task already exists (H1). The service docstring promises that one prospect failing does not stop the others.

**Fix**: catch `HubSpotError`, `ValidationError` and `SQLAlchemyError` per prospect. Call `await self._session.rollback()` on the non-HubSpot cases before continuing, and report the failure with a code. Add a test where the batch read returns a malformed body for one prospect.

#### M5. An unrecognised associations body is silently treated as "no activity"

`app/promotion/client.py:632-637` · `app/promotion/schemas.py:332-357`

`list_associated_ids` drops any result that has neither `toObjectId` nor `id`. If the live portal's shape differs from both (the PR's decision 12 says it is unverified), every engagement disappears with no error and no log. The machine then advances only on ticked tasks and nags about everything logged by hand, which is exactly how the founders log today (E14). That failure is silent and total.

**Fix**: when `page.results` is non-empty but no ids were extracted, log `promotion.hubspot.association_shape_unrecognised` at error level, or raise. Before T9 or T13 ships, make one read-only GET against a known contact and save the body as the fixture.

### Low

- **L1. `enrol()` race or crash orphans a task and surfaces a raw `IntegrityError`.** `app/cadence/service.py:102-124`. The check-then-create-then-insert sequence has H1's crash window. A concurrent double enrol passes the `get_by_contact` check twice, creates two tasks, and the second insert hits `uq_cadence_state_contact` as an `IntegrityError` rather than `AlreadyEnrolledError`. The callers (T9, T13) are serial today. **Fix**: map `IntegrityError` on the flush to `AlreadyEnrolledError`, and give enrol the same look-before-create as H1.
- **L2. The paging cursor is interpolated unencoded.** `app/promotion/client.py:625`. `after` is opaque. Build the query with `params=` (or `urllib.parse.urlencode`) rather than an f-string.
- **L3. `completed_at` falls back to `updatedAt`.** `app/cadence/sync.py:205-209`. Any later edit to a completed task, such as a comment or a reassignment, moves `updatedAt` and therefore the touch's `done_at`. Harmless once H2(b) stops task-only signals from moving the anchor; until then it can skip activity. Prefer `hs_task_completion_date`, then `hs_lastmodifieddate` only if the status changed, or accept and document it.
- **L4. The "no outcome columns" test is a substring blacklist.** `tests/cadence/test_models.py:84-89`. A column named differently (`spoke_to`, `answered`) passes. An allowlist of the expected columns would make the invariant exact.

## The tests: proof or restatement?

Mostly proof. The walk runs through the real service, repository and Postgres against a stateful fake that raises on any unmodelled request. Gaps:

- **Three-cycle walk**: genuine, but both walks advance the clock 4 days between *every* touch. So the same-day due dates inside a cycle are never asserted in the walk; only subjects and types are. `test_the_next_cycle_waits_four_days_after_the_email` covers the 4-day wait once.
- **Done by hand**: genuine for "log everything, then sync". The tick-then-log-the-next-touch orderings in H2, which are the realistic ones, are not tested, so the suite passes over the bug.
- **Same-day idempotency**: proves the sequential case only. It passes because the row moved. Nothing exercises a create that HubSpot executed but answered with a 5xx (H1), or two syncs at once (M1).
- **Overdue**: genuine, at the service, route and CLI levels.
- **Missing**: future-dated activity (M2), a sync that runs the day after (M3), a non-HubSpot failure mid-run (M4), and an unrecognised association body (M5).

## Documented deviations: checked, not flagged

All the deviations in `.claude/reports/t11-cadence-report.md` (fixture location, `enrol(due_at=…)`, no `CadenceStateNotFoundError`, the sync exit code, the event rename, `machine.py` and `cli.py`) are intentional and not counted. The same goes for `0005_cadence`'s `down_revision`, which gets rebased onto `0004_sourcing` at merge.

The only **undocumented** divergence is a claim rather than code: "Safe to call as often as wanted" (`routes.py:37`) and "idempotent by construction" (`service.py:7-9`, README) overstate what holds, per H1 and M1.

## The decisions for review: a reasoned view

- **Notes count as a matching activity.** Right default given E14: excluding notes would re-fire every touch logged the way the founders actually log. The real damage today comes from H2's pairing, which is much worse with notes and goes away with the H2 fix.
  - *Cheapest mitigation:* print each note-closed touch in the sync output, e.g. `contact 123: call closed by note 61240001`. One line in the CLI loop means a wrong close is seen in the founder's weekly glance at no policy cost.
  - *Next step if that proves noisy:* count only notes created by a user (`hs_created_by_user_id` set), which excludes integration or system notes.
- **No enrol route or CLI.** Agree; a bare enrol would reset a cycle. The cost is that D5 first meets real data on T13's 22 real prospects.
  - *Mitigation:* add `lpe cadence sync --dry-run`, which prints the plan and creates nothing, so T13's first run can be inspected before any task is created.
- **No exit when a conversation is reached.** Acceptable for T11 only if a human can stop a prospect. Today one can't: deleting the task just reports it as missing every sync, and the row stays live and overdue forever. A reached prospect will be asked for a voicemail after a good conversation, and M2 makes a booked meeting actively harmful.
  - *Recommendation:* a minimal `lpe cadence park <contact_id>` before T13 adopts the 22. It is a schedule write, so it fits D4.
- **Open tasks made redundant are reported, not completed.** Agree with not writing to the founder's tasks. The M5 consequence is real, but the fix is in the metric, not the machine.
  - *Recommendation:* define M5 from D5 closures (the `cadence.sync.touch_done` events, or the count of positions advanced), not from HubSpot task status. Decide that before T10 builds the Friday report on task completion.
- **Task owner is left to the caller, so tasks are unassigned by default.** This one is risky. Unassigned tasks don't appear in anyone's "My tasks", which recreates E15 (tasks nobody works).
  - *Recommendation:* a single `HUBSPOT_DEFAULT_OWNER_ID` setting applied when the caller passes none, decided before T9 calls `enrol`. One user, one setting; config, not code.
- **The associations response shape is unverified against the live portal.** Accept, with the M5 guard added. Today the failure mode is silent and total, which is worse than a crash. One read-only GET on a known contact before T9 or T13, saved as the fixture, closes it.

## What's good

- **The pure core is the right shape.** `machine.py` and `find_signal`/`plan_advance` do no I/O and read no clock, so D5 is testable in table form, and T13 can replay history through the same functions.
- **The anchor as a total-order cursor** is a clever, minimal answer to "which activity is new" that stores no outcome. It is the most important design choice in the PR, and it is right; H2 is about *which* activity it consumes, not about the mechanism.
- **The database states the invariants.** `ck_cadence_state_live_has_task` turns "nobody is silently dropped" into a constraint, and the status, touch and cycle checks take PR #6's lesson on board. Each constraint has a test.
- **The timezone handling is careful.** It refuses naive datetimes, uses the Dallas day rather than the UTC day for "same day", and tests DST.
- **The fake portal is honest.** `FakeHubSpot` serves fixture-shaped bodies on top of a `MockPortal` that raises on anything unmodelled, so "no live calls" is a property the suite proves rather than one it asserts.
- **Spike 4 is honoured in three places**: the email-task body, the absence of any engagement-create method, and the on-the-wire assertion.
- **The decisions are surfaced, not buried.** All 13 conservative readings are listed in the PR body and report, which is what made this review's decision section possible.

## Recommendation

**Request changes: fix H1 and H2 before merge.** H1 is about 20 lines plus a fake mode. H2 is two policy lines plus three table tests. Both use code that is already in this PR.

M1 to M5 are each small. M1 (the advisory lock) and M5 (the shape guard) are worth doing in the same pass, because both turn silent failures into visible ones. M2 to M4 can be deferred with a log entry if the author prefers.

Then decide owner assignment and the park command before T9 or T13 calls `enrol`.

## Fixes applied

Commit `cf8d72e` on `feat/t11-cadence` was pushed and summarised on the PR (comment 6074035499). Each fix has a test that failed before it and passes after.

- **H1, fixed.** The advance is committed before the create, as `cadence_state.pending_task_key`. The key is `lpe-cadence:<contact>:<cycle>-<touch>` and also ends the task body. A pending row is resolved through the contact's associated tasks: an existing task is adopted (`pending_tasks_adopted`), and if none exists the task is created once. The POST is never retried. Tests: `tests/cadence/test_service.py::TestAmbiguousCreates` and the updated `TestFailures::test_a_failed_task_create_leaves_the_schedule_alone_and_is_retried`.
- **H2, fixed.** A tick pairs only with matching activity at or before it. A tick alone moves the anchor up to the tick, but never past the first uncredited activity. Tests: `tests/cadence/test_policy.py::TestPairingStopsAtTheTick` (all three reproductions) and `test_service.py::TestTickThenLogNextTouch`. The residual race (logging after the tick advances early) is documented in `sync.py` and the README.
- **M1, fixed.** A session advisory lock is held on a dedicated connection, and an overlapping sync returns `skipped`. Tests: `TestConcurrentSyncs` and `test_cli.py::TestSync::test_a_second_sync_while_one_runs_says_so_and_exits_cleanly`.
- **M2, fixed.** Activities with `ts > now` are ignored. Tests: `test_policy.py::TestFutureDatedActivity` and `test_service.py::TestFutureDatedActivity`.
- **M3, fixed (docs).** The sync runs daily via launchd, independent of T10's weekly run. The README, CLI help, `routes.py` and the architecture doc are updated.
- **M4, fixed.** HubSpot, `ValidationError` and `SQLAlchemyError` failures are each caught per prospect, rolled back, given a code and reported. Test: `TestNonHubSpotFailures`.
- **M5, fixed.** `HubSpotResponseShapeError` is raised, `promotion.hubspot.associations_unrecognised` is logged, and the error is reported under `failures`. Tests: `tests/promotion/test_reads.py::TestAssociationShapeGuard` and `test_service.py::TestAssociationShape`.
- **Decisions.** `HUBSPOT_DEFAULT_OWNER_ID` is added (`TestDefaultOwner`). Metric M5 is defined as the number of touches the machine closed (`SyncReport.touches_closed`).
- **L2 and L4, fixed.** The cursor is URL-encoded (`TestPagingCursor`), and the column test is now an allowlist. **L1 and L3 are deferred**: enrol has no caller yet, and the completion-date fallback no longer moves the anchor past visible activity.
- **Deferred by the human:** `--dry-run`, `lpe cadence park` and a per-touch "closed by a note" line.
- **Validation:** ruff and format pass; mypy and pyright report 0 errors; pytest has 378 passed with the DB, and 276 passed plus 102 skipped without it; there is one alembic head; `alembic check` is clean.
