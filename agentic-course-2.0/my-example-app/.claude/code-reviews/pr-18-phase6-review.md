# PR #18 review (Phase 6): the merge with `main` and the predicate wiring

**Scope:** `a1448b3` (merge `main`, `0008` re-chained) and `3cb6799` (T7 Phase 6). Phases 1–5 were reviewed in `pr-18-review.md`.

**Verdict: approve once M1 is decided.** There are no Critical or High issues, and validation is green. Two Mediums remain:
- **M1 needs a decision before the first live run under the new semantics.** It is the same backlog-wipe failure class as the misrouted-rule guard, through a path that guard does not cover.
- **M2 is latent:** no current manifest triggers it.

**Method:** The `code-reviewer` agent reviewed both commits in a clean context. It read the conflict resolutions and every Phase 6 file in full, and treated the owner's two decisions as intended. The one claim it was unsure of (L5) was checked live.

## Validation (on `3cb6799`)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | ✓ / ✓ (172 files) |
| `mypy .` / `pyright` | ✓ (173 files) / 0 errors; zero suppressions |
| `pytest`, throwaway Postgres on 5435 | **1,081 passed, 2 skipped** (the paid live evals) |
| `alembic heads` / `alembic check` | one head, `0008_qualification` / no drift |
| Mutation check | 6/6 caught |

## Issues

### Medium

**M1. A negated rule on a lookup source can still disqualify, and permanently suppress, a whole batch.**
- **Where:** `app/tools/search_registry.py` (`_refuse_misrouted_rules` covers only the discovery source) and `app/sourcing/sources/fmcsa.py` (`_scalars`).
- **What happens:**
  - QCMobile fields that are booleans, nulls or nested objects are not stored. A typo in a field name is never present either.
  - A negated rule on such a field therefore sees "absent" on every found carrier, fires on every one, and each lands in `suppressed_registry_ids` for good.
  - Suppression is keyed on `(vertical, registry_id)` across manifest versions, so a corrected manifest does not undo it.
  - The dry-run cannot catch it, because it is offline and reads bulk files only.
- **Fix options (human decision):**
  - **(a) Recommended: a per-batch circuit breaker.** If one rule fires on all, or nearly all, of the candidates that have a record for its source, raise before recording any disqualification. The run fails and its batch goes back.
  - **(b) Lookup sources declare `known_fields`**, as the census does. QCMobile's live key set is known (2026-10-10), but it can change without notice.
- **Not blocking the merge:** freight runs refuse until v7 exists, and the v7 rules are spelled from the live record.

**M2. `records_from` judges a repeating source only when a carrier happens to have exactly one row.**
- **Where:** `app/qualification/records.py:39`.
- **What happens:** a predicate rule on `fmcsa_revocations` would judge a carrier with one revocation row but leave a carrier with two `source_missing`. The verdict would depend on the row count.
- **Latent:** freight's `authority_revoked` is a judgment rule.
- **Fix:** refuse a *predicate* rule routed to a join source, in `_refuse_misrouted_rules`, or have `records_from` skip join sources by role rather than by row count.

### Low
- **L1. A malformed rule fails only after the batch's free work is spent.** `check_shape` is first reached inside `disqualify_by_predicates`. **Fix:** call `check_shape` on every predicate rule in `_refuse_misrouted_rules`, before suppression and the refresh.
- **L2. The `IN (...)` lists grow with the pull.** `suppressed_registry_ids(vertical, <all pulled ids>)` binds about 4,500 parameters. **Fix:** query with `None`, bounded by the number of disqualifications, or anti-join in SQL. asyncpg's limit is 32,767.
- **L3. The misrouted-rule test doesn't prove "before the backlog is touched".** **Fix:** also assert there are no `sourcing_pool` rows for the vertical.
- **L4. The parity test infers SoQL's null behaviour from the substring `"IS NULL OR"`.** **Fix:** parametrise an explicit expected outcome per operator, and keep the substring check as a cross-check.
- ~~**L5. "Passes by construction" assumed SoQL `contains()` was case-insensitive.**~~ **Closed: checked live.** `contains(carship,'B')` gives 19,593 Texas rows and `'b'` gives 0, so SoQL is case-sensitive, like the evaluator.

## Checks that came back clean
- **Commits and failure state:**
  - Disqualifications committed before a later failure persist, and the failed run hands its batch back.
  - The next run excludes the suppressed ids, and `record` is idempotent on `(candidate, rule)`.
- **The guard:**
  - It runs after the pull, before suppression, the refresh and selection.
  - It skips judgment rules and rules with an explicit non-discovery `source`.
  - It cannot false-positive on a census column, because it uses the same `CENSUS_COLUMNS` set the pushdown uses.
- **Citations:** each disqualification cites its own source's record URL. The one-record-per-source validator guarantees that.
- **`observed=None` on a fired outcome** is handled everywhere: the dry-run guards it, the service writes "absent", and the helpers key on `kind`.
- **`pool_available == 7` in the two-run test** is correct: 8 fixture rows minus 1 suppressed.
- **The merge loses nothing from either side:**
  - `alembic/env.py`, `0008` → `0007`, both `CandidateFields` additions, the D13 walk, both `FIELD_OWNERS` entries, both test classes, and the `CLAUDE.md` map.

## What's done well
- `app/sourcing/` still never imports `app/qualification/`.
- An absent *record* is distinguished from an absent *field*, and `rows=()` is never a drop.
- The parity test ties the Python evaluator to the SoQL keep clauses.
- The two-run test builds the exact failure scenario.
- The evidence text separates "absent" from a value.
- `classify_rollup` sees stage 1's disqualifications, so free-before-paid holds.

## Recommendation
Merge after deciding M1. Option (a), the circuit breaker, is about 20 lines plus a test, and the same PR can take M2 and L1. L2–L4 can be deferred.

## Resolution (2026-10-11)
- **M1: fixed with option (a).** `rules.check_batch` trips when one rule fires on ≥90% of the batch candidates
  that have a record for its source (at least 5). `QualificationService.disqualify_batch` evaluates the whole
  batch before recording anything. On a trip, the run fails with `BatchWideDisqualificationError`, the batch goes
  back, and nothing is suppressed.
- **M2: fixed.** A predicate routed to a join source refuses before the pull.
- **L1: fixed.** `check_shape` runs on every predicate rule before the pull.
- **L2–L4: deferred**, as recommended.
- **Validation:** 1,087 passed, 2 skipped; ruff, mypy and pyright clean. Mutation check 5 of 5.
