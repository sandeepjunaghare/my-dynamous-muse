# PR #14 review: T13, adopting hand-worked prospects (`lpe cadence adopt`)

**Recommendation: approve after fixing M1 and M2.** There are no Critical or High issues.

The anchor logic holds in all four reconstruction cases (nothing matched, touches closed, all nine done,
override). The first sync after adoption cannot credit an activity twice or miss one. The live run backs
this up: `sync --dry-run` straight after apply checked 14 and would advance 0.

- **M1** breaks the CLI's "never a traceback" rule, and the author reproduced it.
- **M2** makes the report and CLI show a plan for an entry that was not applied.

Both are small.

The review was done in a fresh context by the `code-reviewer` agent, which read every changed app file in
full. That agent had no shell, so the author ran validation on the PR head (`7fccdd0`) and reproduced the
findings marked below.

## Validation

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean |
| `mypy .` · `pyright` | no issues in 102 files · 0 errors (strict, zero suppressions) |
| `pytest` with a database (Postgres 16) | **666 passed, 0 skipped** |
| `alembic check` | no new operations, no migration |
| Mutations (from the implementation report) | dropping `anchor_ref` in `enrol` fails 3 tests; disabling look-before-create fails 3 |
| Live portal (approved by the founder) | 14 adopted, 3 parked, 0 failures; the follow-up `sync --dry-run` would advance 0 |

## Issues

### Medium

**M1. A roster that is not UTF-8 crashes `lpe` with a traceback.** `app/cadence/adoption.py:194-196`
(`load_roster`)
- `Path.read_text` raises `UnicodeDecodeError`, which is a `ValueError`, not an `OSError`. So it escapes
  `main()`, which handles only `LocalProspectEngineError` and `IntegrityError`.
- This contradicts `load_roster`'s docstring ("every way it can be wrong is one `RosterError`") and
  `app/cli.py`'s "never a traceback". A roster saved as UTF-16 from a Windows editor or Excel is a
  realistic way to hit it.
- **Reproduced by the author.** A UTF-16 roster gives `Traceback … UnicodeDecodeError: 'utf-8' codec can't
  decode byte 0xfe`, with exit code 1.
- **Fix:** add `except UnicodeDecodeError as exc: raise RosterError(f"roster {path}: not UTF-8 text — save
  it as UTF-8") from exc`. Do not widen the `OSError` branch: it reads `exc.strerror`, which a decode error
  lacks. Add a `test_adoption.py` case that writes `b"\xff\xfe…"`.

**M2. A plan is recorded before it is applied, so a failed or race-skipped entry still appears in
`report.plans`.** `app/cadence/adoption.py:417-420` (`adopt_one`)
- `report.plans.append(plan)` runs before `_apply`. If `_apply` fails, or loses the enrol race and raises
  `AlreadyEnrolledError`, the plan stays in the list.
- The CLI hides failed entries (`cli.py:253-256`) but not race-skipped ones. Such a contact prints
  `start 1/3 call, due …` followed by `already has a cadence — skipped`.
- The `run_completed` count `planned=` also includes entries that were not applied.
- **Fix:** append the plan only after `_apply` returns. In a dry run, appending at plan time is right and
  stays as is.

### Low

**L1. The enrol race can leave an orphan task, and nothing records it.** `app/cadence/service.py:218-249`
- If two enrolments both pass `find_task_by_key` before either creates a task, both create one. The loser
  maps the `IntegrityError` to `AlreadyEnrolledError`, and its task stays open in HubSpot, carrying the
  winner's key.
- Because `find_task_by_key` adopts the *oldest* task with a key, a later pending-create resolution could
  pick the orphan.
- The window is narrow: `Adopter.apply` holds the sync lock, but `enrol` itself takes no lock, and T9 will
  call it.
- **Fix:** log `cadence.service.task_orphaned` with the task id before raising, and name the id in the
  message.

**L2. A task reused by key keeps its old due date in HubSpot.** `app/cadence/service.py:218-221`
- After a lost create response, the re-run reuses the task but writes its own `due_at` and `anchor_at` to
  the row. The portal and the overdue view can then disagree by the time between the two runs.
- This is a rare edge case. Document it in the README's *Enrolment* section, or log the mismatch.

**L3. Unicode digits pass id and position validation.** `app/cadence/adoption.py:150` (`isdigit`) and
`:103` (`\d`)
- **Reproduced by the author.** `contact = "²"` and `start = "١-call"` are both accepted. The id then
  surfaces as a misleading `contact_not_found`.
- **Fix:** use `stripped.isascii() and stripped.isdigit()`, and `re.ASCII` on `_POSITION`.

**L4. The plural company-only messages have the wrong pronoun.** `app/cadence/cli.py:310-322`
- The verb now agrees with the count, but the pronoun does not: "contacts 1, 2 are listed above — adopting
  **it** covers this" and "… put **it** in the roster".
- **Fix:** use "them" when there is more than one contact.

### Test gaps (Low)
- **`_guarded`:** the database-error and validation-error branches are untested, as is "roll back, record,
  and the next entry still adopts". Inject a database error on the first entry and assert the second adopts.
- **`enrol`:** the re-raise branch for an `IntegrityError` on another constraint is untested.
- **"The first sync closes nothing"** is pinned only for a history that ends on a note. Add the
  nothing-matched anchor `(createdAt, None)` and the override anchor `(now, None)`.
- **CLI:** parked output, exit code 1 on an apply failure, `SyncRunningError`, the plural and
  already-in-the-cadence company-only branches, and the race-skip output from M2 are all untested.
- **Lock test name:** `test_apply_waits_for_a_running_sync…` says "waits", but `apply` refuses.

## Documented deviations (not flagged)
These are all in `.claude/reports/t13-cadence-adoption-report.md`:
- the report lists carry `CadenceStateResponse`;
- an already-enrolled contact is skipped before any reads;
- `parked_position`;
- `UnadoptableContactError`;
- the failure code is `hubspot_response_error`;
- discovery cannot detect sample tasks;
- the new test files were not watched failing first;
- the test database runs on port 5434.

## Done well
- **The anchor rule holds end to end.** `enrol` and `create` carry `anchor_ref`, `reconstruct` maps all four
  cases correctly, and the mutation check proves the tests catch a regression.
- **One done-policy.** Adoption reuses `plan_advance`, so adoption and the sync cannot disagree about what
  history means.
- **"No field writes" is proved structurally.** The fake portal rejects any request it does not model, and
  the test allows only GET and POST to `/batch/read` and `/tasks`. No `candidate` row is written and no
  `touch_done` is logged.
- **Concurrency is handled.** `apply` holds the sync lock for the whole run and a dry run takes none.
  `IntegrityError` handling rolls back before checking the constraint name, and other integrity errors are
  re-raised.
- **The roster validation is thorough.** It handles unquoted integer ids, forbids extra keys, rejects
  duplicates and `start` on `park`, and reports errors as `prospect 2.start`.
- **Project rules are followed.** Typing is strict with no `Any` and no suppressions; cadence imports only
  `core` and promotion's client and schemas; event names follow `domain.component.action_state`.
- **The live dry run fed back into the code.** The company→contacts lookup came from real output, and the
  README and report record it.

## Next
Run `piv-fix-review-findings` on this report for M1 and M2. L1 to L4 and the test gaps are each small and
can ride along, or be deferred with a note.
