# Code Review — PR #6, round 2 (after the fix commit)

**Reviewed**: `feat/t2-vertical-manifest` @ `3328565` → `main` · 32 files, +4277/−13
**Method**: fresh-eyes pass by the `code-reviewer` agent in a clean context, re-verifying both round-1 fixes
from first principles rather than from the report, plus a full validation run and three findings checked
directly against a live Postgres 16.

Round 1's report is kept at `.claude/code-reviews/pr-6-review.md` rather than overwritten — a second review
of the same PR should not erase the record of the first.

**Recommendation: merge after one wording fix** (or an explicit defer note, which is this project's own
established pattern). **0 Critical · 0 High · 1 Medium (new) · 0 open from round 1.**

## Round-1 findings: both closed

### 1 · CHECK constraint on `status` — **closed, verified live**

The exact insert that *succeeded* in round 1 now fails by name:

```
ERROR:  new row for relation "vertical_manifest" violates check constraint "ck_vertical_manifest_status"
DETAIL:  Failing row contains (…, probe_v, 1, Active, {}, …).
```

And the constraint is neither too narrow nor too wide — all three lifecycle values insert cleanly, while the
partial unique index still refuses a second `active` on top of it:

```
INSERT 0 3                                    -- draft, active, superseded
ERROR:  duplicate key value violates unique constraint "uq_vertical_manifest_one_active_per_vertical"
```

Also confirmed: identical name and value set in `0002:52-55` and `models.py:65-68`; the set matches
`ManifestStatus` exactly; `downgrade()` still round-trips (an inline `CheckConstraint` on `create_table`
needs no separate drop); and every writer of `status` in the codebase goes through `ManifestStatus.*.value`
— the one raw literal is the test that writes `"Active"` on purpose to prove the constraint fires.

### 2 · CLI catching `IntegrityError` — **closed**

The catch is at the one place the module docstring names as where a failure becomes an exit code, the exit
code matches the `LocalProspectEngineError` branch, the message is prose rather than `str(exc)` (the test
asserts `"INSERT INTO" not in stderr`, so the SQL dump cannot leak), and the test drives the real
`app.cli.main` entry point rather than a stand-in. It was proven red before the fix.

## New finding

### Medium — the catch's message is `activate`-specific, but it sits in the file every slice's CLI shares

`app/cli.py:83-94`

`app/cli.py`'s own docstring says the file is "thin on purpose… each slice contributes its own command
group". The new `except IntegrityError` block breaks that: `main()`'s `try` wraps dispatch for *every*
current and future command, but the message names one failure mode —

> "another activation for this vertical may have landed first; run `lpe manifest show <id>`"

The code comment calls the message "deliberately generic". It isn't; it names activation.

**Why this bites the very next ticket, not a distant one.** `vertical_manifest` has a second unique
constraint, `uq_vertical_manifest_vertical_version`, and `ManifestRepository.create_draft` reaches it by a
select-max-then-insert that is itself racy (`repository.py:106` → `next_version`). T12 adds
`lpe manifest propose`, which calls `create_draft`. Two concurrent proposals for one vertical both read
`next_version` as *N*, both insert, and the loser hits the *(vertical, version)* constraint — then gets told
a losing **activation** race happened and is advised to run `manifest show <id>`, which is wrong advice at a
call site where no `<id>` exists.

**Fix — pick one, consciously, the way finding 2 itself was decided:**

- **(a) Push it down**: move the catch into `app/manifests/cli.py`'s per-command handlers, so each command
  owns wording matching its own failure mode and `app/cli.py` goes back to carrying no slice semantics.
- **(b) Generalise the message** in place: drop the `activate` noun and the `manifest show` suggestion —
  "the database refused the write — a concurrent write may have landed first" is true of any command.

(b) is two lines and makes the comment's claim honest. (a) is the cleaner architecture and costs a little
duplication. Either is defensible; leaving the specific wording in the shared file is the option worth
arguing against.

## The three explicit decisions — all upheld

- **Amending `0002` rather than adding a revision**: agreed. `conventions.md` → *merge* governs a forked
  chain, not amending an unapplied revision, so nothing is violated; the safety condition ("no environment
  has applied it") holds while the branch is unmerged. **Worth saying out loud: that reasoning expires at
  merge.** Once anyone runs `alembic upgrade head` anywhere real, the next change to this table's shape must
  be a new revision, not another amend.
- **Minimal CLI catch over a modelled 409**: agreed, and the premise was re-verified independently —
  `activate()` has exactly one caller, the routes are read-only, and T10's trigger endpoint runs the
  sourcing pipeline, which *reads* the active manifest and never activates one. The `IntegrityError` that
  still escapes the service has no non-CLI caller today.
- **Declining the argparse `Namespace` note and the repository-layer `assert`**: agreed with both. The first
  has no fix that does not add a suppression this project forbids; the second would duplicate across two
  layers a rule the plan puts in the service once, which is how the two drift apart.

## Validation

| Gate | Result |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | pass, 45 files |
| `uv run mypy .` | pass, 45 source files |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | **125 passed, 43 skipped** |
| `TEST_DATABASE_URL=… uv run pytest` | **168 passed**, 2 warnings |
| `alembic downgrade base` → `upgrade head` | round-trips clean |
| `alembic check` / `heads` | no pending operations · one head |

The 2 warnings remain T1's `HTTP_422_UNPROCESSABLE_ENTITY` deprecation, tracked as deferred finding #4.

## Recommendation

**Merge once the message wording is settled.** Nothing else from round 1 or the broader re-check remains
open: the terms gate, the one-ACTIVE invariant, citation immutability and transaction discipline were all
re-checked and hold. The one finding is a future-correctness papercut in shared code, cheap now and
annoying to discover from a confusing error message during T12.

Posted as a comment rather than a formal approval: GitHub does not allow approving your own pull request,
and the human merge decision is the real gate regardless.
