# PR #14 review, round 2: T13 adoption after the review fixes (`c9943ec`)

**Recommendation: approve.** All six round-1 findings are resolved, and each is pinned by a test that fails
without its fix. This round found nothing Critical, High or Medium, and two Low items that can be fixed or left.

A fresh `code-reviewer` agent did the review. It read round 1, the fix commit, and every touched file in full.
The author ran validation on the PR head.

## Validation (`c9943ec`)

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean · 103 files formatted |
| `mypy .` · `pyright` | no issues in 102 files · 0 errors (strict, no suppressions) |
| `pytest` with a database (Postgres 16) | **672 passed, 0 skipped** (666 in round 1, plus 6 new) |
| `alembic check` | no new operations |
| New tests against the round-1 head `7fccdd0` | all 7 new cases fail there and pass on `c9943ec` |

## Round-1 findings

| ID | Status | Evidence |
|---|---|---|
| M1 | Resolved | `load_roster` has a separate `UnicodeDecodeError` branch, so it doesn't rely on `exc.strerror`. A UTF-16 roster gives one `RosterError` naming the file. |
| M2 | Resolved | `adopt_one` appends the plan after `_apply`. `rehearse` and `discover` still go through `_plan_all`, which is unchanged. `to_close` and `run_completed planned=` are now accurate. The tests cover the race-skip path and the database-error path. |
| L1 | Resolved | `created_here` is true only when no task was found by key, so a reused task is never logged as orphaned. The log fires only on the `uq_cadence_state_contact` violation, after rollback. |
| L2 | Resolved (doc) | The README text is accurate. `overdue` reads `state.due_at`, and the sync never reads a task's due date. |
| L3 | Resolved | `isascii() and isdigit()` checks ids, and `_POSITION` uses `re.ASCII`. Tests cover `²` and `١-call`. |
| L4 | Resolved | `_them` handles both plural branches, and the CLI test asserts the plural text. |

The remaining round-1 test gaps are deferred to #15 on purpose, so they are not flagged again here.

## New issues

### Low

**N1. The orphan-task id never reaches the person running `adopt`.** `app/cadence/adoption.py:588-590`
- `_guarded` catches `AlreadyEnrolledError` and records only the contact id. So the loser of an enrol race
  prints `contact N: already has a cadence — skipped`, and the "task X … left open — close it by hand" text
  is dropped.
- The `cadence.service.task_orphaned` log line still carries the id. But the README says the error names it
  "for the person to close", and `adopt` is today's only caller.
- **Fix:** carry the orphan task id on the exception and print it in the CLI's skipped line. Or narrow the
  README to say the id is in the log.
- It's Low because it needs two enrolments to collide, and `apply` holds the sync lock.

**N2. The CLI's `failed` filter is now dead for a real apply.** `app/cadence/cli.py:253-256`
- After the M2 reorder, an entry that failed on apply is no longer in `report.plans`, and a dry run
  already bypasses the filter. So the condition can't matter.
- **Fix:** loop over `report.plans` directly.
- This doesn't change behaviour; it only makes the code mislead the next reader.

## Tests

- **Class-level patches:** the patches on `OutcomeReader.activities`, `OutcomeReader.find_task_by_key` and
  `CadenceRepository.create` all go through `monkeypatch`, so nothing leaks between tests.
- **`_deadlock_for`:** it is a strict-clean ParamSpec wrapper, and it matches on `contact_id=`, which is how
  `enrol` passes it.
- **Ordering:** the plural CLI assertion's contact order is deterministic, because the fake portal links
  companies in insertion order.

## Next

The PR is mergeable as it stands. N1 and N2 are small. They can go in a follow-up or ride along now; neither
blocks the merge.
