# PR #16 review: the deferred T13 adoption test gaps (#15)

**Recommendation: approve.** The PR is tests only and no app code changed. Every gap in #15 is closed or
already covered, and each new test fails on the bug it targets. This review found nothing Critical, High or
Medium, and three Low polish items.

A fresh `code-reviewer` agent did this review. It read every new test, the code each one targets, and the fake
portal. The author ran validation on the PR head.

## Validation (`957d6a5`)

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean · 103 files formatted |
| `mypy .` · `pyright` | no issues in 102 files · 0 errors (strict, no suppressions) |
| `pytest` with a database (Postgres 16) | **682 passed, 0 skipped** (674 on `main` + 8 new) |
| `alembic check` | no new operations |
| `git diff main...HEAD -- app` | empty: tests only |
| Mutation check (author) | 6 of 6 caught; each targeted break fails its test |

## Does each test discriminate?

- **Start override:** this test genuinely tells the two anchors apart.
  - With the anchor at `since`, the two earlier calls would close 1-call and then voicemail, so the sync
    would report 2 touches closed. The test asserts 0.
- **No history:** `(1, 1)` holds for the right reason.
  - The new call closes 1/3 call and becomes the anchor, and the voicemail task is created.
  - An end-of-today anchor would put the anchor after the call and give `(0, 0)`.
- **`_violates`:**
  - `enrol` and `adopt_parked` test `UNIQUE_CONTACT not in str(exc.orig)`. The synthetic `orig` names real
    check constraints (`app/cadence/models.py`), so the re-raise branch runs.
  - The patch is on the class, and `*args` takes `self`.
- **Company-only "already in the cadence":** this test reaches the branch for the right reason.
  - `discover` skips adoption's keyed task, but the hand task still yields the contact.
  - `_plan_contact` raises `AlreadyEnrolledError`, so the contact lands in `already_enrolled` and not in
    `plans`, and the `adopted` branch fires.
- **CLI isolation:**
  - `fresh_contact` removes the only row these tests commit. The unknown contact never gets a row, and the
    lock test asserts none.
  - Every assertion matches a whole line, so a short task id cannot match inside a random contact id.
  - The lock test follows the existing sync and park lock tests, and reads stderr, where `error:` goes.

## Issues

### Low

**L1. Two older CLI tests still write their roster inline.** `tests/cadence/test_cli.py:453-456`, `:480-483`
- The new `_roster_file` helper does the same job.
- **Fix:** switch both to `_roster_file(tmp_path, (fresh_contact, "adopt"))`.

**L2. The no-history test's docstring claims more than the test proves.** `tests/cadence/test_adoption_service.py:146`
- For a contact with no history, the test cannot tell an anchor at `since` from one at `now`: any activity
  between the two would be history. What it proves is that the anchor is no later than the next logged call.
- **Fix:** reword the docstring to say that.

**L3. `_violates` returns `Callable[..., Awaitable[NoReturn]]`, the repo's only `...`-arity `Callable`.**
`tests/cadence/test_service.py:607`
- It is strict-clean, but the other test doubles use ParamSpec.
- **Fix (optional):** drop the return annotation's `...` by letting the inner `write` signature carry it, or
  leave it as is.

## Noted, not an issue

- **A non-unique `IntegrityError` after the task was created logs no `task_orphaned`.** That is correct. The
  task is not orphaned: it carries the touch's key, so the next run's look-before-create finds and reuses it.
  The first service test pins this with `task_creates_attempted() == 1`. Only the lost-race path leaves a task
  that nobody will reuse.

## Done well

- **Mutation check:** every new test was mutation-checked. The check also exposed a no-history test that could
  not fail, and the test was strengthened, not shipped.
- **Override test:** it puts evidence on both sides of the anchor.
- **Lock test:** it also asserts that no task was created and no row was written.
- **Partial failure:** the failing entry comes first, so "the rest still adopt" is really tested.
- **Company-only test:** it goes through the real apply-then-discover flow instead of seeding the database.

## Next

Nothing blocks the merge. L1–L3 are optional polish. A person reviews the PR and merges it, which closes #15.
