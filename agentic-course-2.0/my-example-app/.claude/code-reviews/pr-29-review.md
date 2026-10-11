# PR #29 Review: fix(qualification): label dry-run rows by an identifier column

**Recommendation: Approve.** No critical, high or medium issues. Two optional Low findings about test coverage.

## Summary

Socrata's census export puts `mcs150_date` first, so the manifest dry-run used to label each sample row with a filing date. `_row_id_column` now picks the first column named `id` or ending in `_id`, `_number` or `_no`, and falls back to the first column when none matches. The change is small, the helper is pure, and the PR body records the trade-off (a name heuristic can pick an id-like column that isn't the key).

There is no implementation report for this branch. The plan and review sit in `_tasks/todo.md` under "Dry-run row id (2026-10-11)", and they match the diff.

## What I checked

| Question | Finding |
|---|---|
| Is the label used for anything except display? | **No.** `row_id_column` and `row_id` appear only in the report schema, sample construction and the CLI print. The sha256 hashes the whole file. `activate` checks only that a dry-run exists, so changing the label can't affect gating or record identity. |
| Is the header normalised before the pick? | **Yes.** The file opens with `utf-8-sig`, so the BOM is removed. Every header cell is stripped (`dry_run.py:154`) before `_row_id_column` runs (`:162`). Matching is case-insensitive, and the report keeps the original casing. |
| What happens if the chosen cell is blank? | It falls back to `row N` (`dry_run.py:168`). That's safe, and only cosmetic. |
| Can the header be empty? | **No.** `None` and `[]` are refused at `dry_run.py:152`, so `columns[0]` is always valid. |
| Do the tests guard the fix? | **Yes.** `test_rows_are_labelled_by_the_first_identifier_column` fails if the change is reverted. It also checks that an id column wins over the first column. |

## Issues

### Critical / High / Medium
None.

### Low
1. **Case-insensitive matching is untested** (`tests/qualification/test_dry_run.py:177`). Every fixture uses a lowercase `dot_number`, so deleting `.lower()` in `dry_run.py:135` would not fail any test. *Fix:* add a header such as `DOT_Number` or `Id` and assert it is picked.
2. **Some branches have no test of their own.** These are the exact match on `id`, the `_id` and `_no` suffixes, and the `row N` label when the id cell is blank. The fallback test (`test_with_no_identifier_column_rows_are_labelled_by_the_first`) passes with or without the change, so it only guards against a future regression. *Fix (optional):* add one parametrised test over `["id", "carrier_id", "docket_no", "dot_number"]`.

## Validation

| Check | Result |
|---|---|
| `ruff check .` | Pass |
| `ruff format --check .` | Pass (172 files) |
| `mypy .` (strict) | Pass (173 files) |
| `pyright` (strict) | Pass (0 errors) |
| `pytest` | **755 passed, 336 skipped.** `TEST_DATABASE_URL` was not set, so the database tier was skipped. The change touches no database code. The author reports 1,089 passed and 2 skipped with the database tier on, and that total matches. |

## What's good

- The fix is the smallest one that works. It uses one pure helper, and no source is named in code, which keeps the rule that a vertical is data, not code.
- The heuristic can be audited. The report already records which column labelled the rows, so a wrong pick is visible rather than silent.
- The existing order is kept: the BOM is stripped, the header is stripped, duplicate columns are refused, and only then is the id column chosen.
- The PR body states the trade-off and the deferred `--row-id SOURCE=COLUMN` override honestly, and it notes that the recorded v8 dry-run keeps its old labels.

The reviewer was the `code-reviewer` agent, working in a fresh context. Validation was run locally on `fix/dry-run-row-id` at `cf4fe7f`.
