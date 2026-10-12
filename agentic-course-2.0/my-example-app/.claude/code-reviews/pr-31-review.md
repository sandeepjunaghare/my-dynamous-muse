# PR #31 review: define the owner field before T6 and T9 split

**Verdict: approve, after the two Mediums are settled or explicitly deferred.** No Critical or High issues. Validation is green.
The deep pass was done by the `code-reviewer` agent in a clean context, because the reviewing session also wrote the PR.
Findings were checked against the code and live FMCSA data before being written up.

## Validation (head `4f1e97f`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 173 files formatted |
| mypy strict | ✅ 175 files, no issues |
| pyright strict | ✅ 0 errors |
| pytest (DB tier on) | ✅ 1,118 passed, 2 skipped (paid live eval) |
| alembic check (test DB) | ✅ no drift; the PR has no migration (JSONB) |

No implementation report exists. This is a seam PR, and the plan is the "Seam PR before T6 ∥ T9" section of `_tasks/todo.md`.

## Issues

### Medium

**M1. The designator list misses dotted and spelled-out forms, and stored rows are validated again on every read.** `app/sourcing/schemas.py:121`
- `LP` and `LLP` take no optional dots (unlike `L\.?L\.?C`), so `Acme Partners L.P.` and `Acme L.L.P.` are accepted. So are `LIMITED` and `COMPANY`. All four were confirmed by constructing a `Principal`.
- **There is no current impact.** Every one of those forms has zero rows among the TX broker census officers (checked 2026-10-11).
- **The risk is in tightening later.** `upsert_candidate` and `list_candidates` run `model_validate` on stored rows. Once T6 has written owners, a stricter regex would make old rows fail to load, and one bad row makes `list_candidates` raise for the whole run.
- **Fix now, while nothing is stored:** optional dots on every alternative (`L\.?P`, `L\.?L\.?P`), add `LIMITED`, and add `L.P.` / `L.L.P.` to the refusal test.
- **Leave out on purpose:** `CO`, `LC` and `PC`. "Co" is a surname, so these would refuse real people. Add a comment saying why they are excluded.

**M2. The docs don't say what happens when `Principal` refuses a census officer.** `app/sourcing/stages.py:35`, `app/sourcing/README.md:83`, ticket T6
- The docs say the judgment node runs "only where the census names nobody". A census officer of `KZ TRANS INC` is not nobody, but it cannot be cited either.
- If T6 lets the `ValidationError` escape, the whole run fails, because the runner fails on any exception from a stage.
- **Fix:** add one sentence to the T6 ticket and the README: a census officer that `Principal` refuses counts as absent, so the judgment node runs for that candidate, and the stage catches the error per candidate. T6 adds the test.

### Low

- **L1.** `has_principal` (`app/sourcing/sources/fmcsa.py:227`), which orders the backlog, is true for officers that are company names. Those candidates rank ahead of ones with no principal, then need the paid judgment call anyway. Out of scope here. Follow-up: T6 or T5 should compute it with the same designator test, which means exporting a helper rather than duplicating the regex.
- **L2.** No database-tier test of the ownership merge for `owner` (`resolve_owner` overwrites its own citation; another stage only fills an empty field). The merge is generic, so this is not a bug. T6 can add the test.
- **L3.** The regex's word-boundary checks are ASCII-only (`[A-Za-z]`), so an accented letter next to `INC` or `LP` counts as a boundary. Unlikely in practice.
- **L4.** "480 TX broker census rows (2026-10-11)" in the `Principal` docstring will go stale. The ticket and `todo.md` already keep the dated figures, so the docstring could keep only the reasoning. Optional.

## Done well

- **Ownership is complete and enforced.** `owner` has its `FIELD_OWNERS` row, and the existing coverage tests make leaving it out impossible.
- **Nothing that depends on field order was missed.** That covers `unprovenanced_fields`, the README example and the database test. The D13 Google Maps guard already covers `owner`, and there is a test for it. `to_stored` drops a missing `title`.
- **The design was checked against data.** Rejecting name equality was decided from live census counts, not assumed, and a test pins the sole-proprietor case.
- **Validation runs in the right order.** `Field(min_length=1)` rejects an empty name, the validator rejects a whitespace-only one, and a blank title becomes `None`.
- **Strict typing holds.** No `Any`, no suppressions, and docstrings match the surrounding code.
- **Docs agree with each other.** The README table, `stages.py`, the field docstring, and tickets T6 and T9 agree.

## Recommendation

Fix M1 now; it is about three lines plus test cases, and it gets harder once owners are stored. Add M2's sentence to the docs. Then merge. L1 and L2 go to T6's plan.

## Resolution (2026-10-11)

- **M1 fixed.** Every alternative takes optional dots (`L\.?P`, `L\.?L\.?P`, `P\.?L\.?L\.?C`), and `LIMITED` is added. CO, LC and PC stay out on purpose, and a test pins that. The regex docstring warns that changing the list changes which stored owners load. Checked against all 14,347 TX broker census officers: 16 refused, all companies, and no person refused.
- **M2 fixed.** The T6 ticket, the sourcing README and `PipelineStage.resolve_owner` now say that a census officer `Principal` refuses counts as absent, that the judgment node runs for that candidate, and that the error is caught per candidate.
- **L3 fixed.** Word edges are letter-based and Unicode-aware (`[^\W\d_]`). Tests: `Lpé Martín` and `Ángel Incé` are accepted, and `José Ramírez INC` is refused.
- **L4 fixed.** The dated count moved out of the `Principal` docstring.
- **L1 and L2 deferred to T6**, recorded in the T6 ticket under "Carried from PR #31 review".
- Validation: 1,127 passed, 2 skipped; ruff, format, mypy and pyright clean; `alembic check` clean.
