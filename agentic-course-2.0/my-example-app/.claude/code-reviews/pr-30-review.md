# PR #30 review: run-scoped candidate foreign keys on route_assignment and disqualification

**PR:** https://github.com/sandeepjunaghare/my-dynamous-muse/pull/30 · `fix/candidate-run-scoped-fks` → `main` · 8 files, +171/−15 · head `593ba86` · closes #20
**Reviewed:** 2026-10-11. The `code-reviewer` agent did a deep pass with a fresh context, and the author's session then checked each finding.
**Recommendation:** **Approve.** There are no Critical, High or Medium issues, and validation is green. All three findings are Low, and none needs a code change.

No implementation report exists. The issue, the plan section in `_tasks/todo.md` and the PR body set the scope. Including `disqualification` as well as the `route_assignment` named in #20 was decided before the work started, and the PR says so.

## Validation

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 173 files already formatted |
| `mypy .` (strict) | No issues, 175 files |
| `pyright` (strict) | 0 errors |
| `pytest` (database tier, fresh Postgres) | **1091 passed**, 2 skipped (the paid live eval, opt-in) |
| `alembic check` | No new upgrade operations |

## Critical / High / Medium
None.

What was checked and found sound:
- **The migration matches the models exactly.**
  - The composite FK columns `["run_id", "candidate_id"]` reference `candidate(run_id, id)`, in the same order on both sides.
  - The constraint names agree.
  - Neither model's `candidate_id` column still carries a `ForeignKey()`. If one did, autogenerate would report a stray FK.
- **The downgrade runs in the right order.** It drops the composite FKs and recreates the single-column ones first, then drops the unique constraint. That constraint backs the composite FKs, so dropping it any earlier would fail.
  - The dropped names are the Postgres defaults that `0007` and `0008` produced. They were read from a migrated database, not guessed.
- **No join depended on FK inference.** `app/` has no `relationship()`. The only join on `Candidate`, at `app/routing/repository.py:69`, has an explicit ON clause.
- **Both writers already obey the rule.**
  - Routing writes only the run's own candidates.
  - Qualification writes `candidate_id=candidate.id, run_id=run.id` from one run's context.
  - Nothing else references `candidate`, and nothing deletes `candidate` or `sourcing_run` rows.
- **The tests pin the right constraint.** Each one matches on the constraint's name, and both failed ("DID NOT RAISE") before the migration.

## Low

- **L1. The `unique(run_id, id)` index is redundant for uniqueness**, because `id` is already the primary key.
  - Where: `app/sourcing/models.py:154` and `0009_candidate_run_scoped_fks.py:38`.
  - Why it exists: Postgres requires a unique target for a composite FK.
  - Cost: one extra btree on `candidate`, which is negligible at about 150 rows per run.
  - Fix: none needed. The inline comment already explains why it is there.
- **L2. The deploy note overstates the error.** It says "the upgrade names it" (`_tasks/todo.md`, PR body). Postgres reports the constraint and the offending `(run_id, candidate_id)` key, not which row it is.
  - Fix (optional): before upgrading dev or prod, run this pre-flight check, and the same with `disqualification` in place of `route_assignment`:
    `SELECT ra.id FROM route_assignment ra JOIN candidate c ON c.id = ra.candidate_id WHERE c.run_id <> ra.run_id;`
- **L3. The `run_id → sourcing_run` FKs are now implied** by the composite key, through `candidate.run_id`.
  - Keeping them was the stated intent and costs nothing, so this is only a note.

## What's good

- **Minimal and correct.** A composite FK onto a unique target is the textbook fix, with no trigger and no service check.
- **Model and migration mirror each other exactly**, and every constraint is named explicitly.
- **Old constraint names were verified against a real database**, and the upgrade/downgrade round trip was exercised.
- **Test-first, on both tables.** The tests assert on the constraint's name, so a different error can't satisfy them.
- **Failing loudly on bad existing rows is deliberate and documented.**

## Next step

Merge. Before upgrading the hosted dev and prod databases, you can run the L2 pre-flight query.
