# PR #25 review: routing marks the run degraded when geocodes fail, and the qualified-only rule moves to T9/T10

**PR:** https://github.com/sandeepjunaghare/my-dynamous-muse/pull/25 · `fix/t8-routing-degraded` → `main` · 5 files, +112/−9 · head `7c2cc16`
**Reviewed:** 2026-10-10. The deep pass was run by the `code-reviewer` agent, starting from a fresh context. The author's session then checked each finding against the code.
**Recommendation:** **Approve.** There are no Critical, High or Medium issues, and validation is green. Of the four Low items, L1 and L2 are worth a quick follow-up; L3 and L4 are optional.

No implementation report was written for this branch. It is a two-item follow-up from #19, and the PR body states its scope.

## Validation

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 144 files already formatted |
| `mypy .` (strict) | No issues, 144 files |
| `pyright` (strict) | 0 errors |
| `pytest` (database tier, throwaway Postgres) | **874 passed**, 0 skipped |
| `alembic check` | No new upgrade operations |

## Critical / High / Medium
None.

What was checked and found sound:
- **No `KeyError` is possible.** `routing_geocode_failed` and `routing_eligible` are always present in the counts literal at `app/routing/service.py:126-134`. That literal is built after the all-failed raise and returned every time. `counts` is `dict[StageName, StageCount]`, so strict typing holds without a suppression.
- **The denominator is right.** Geocodes are attempted only for eligible candidates, so "N of M" reads as "of those attempted".
- **Zero eligible gives no reason.** Because `failed > 0` implies `eligible > 0`, the message can never read "of 0".
- **The all-failed path is unchanged.** The service still raises before any write, and the runner finishes the run `failed`.
- **The runner composes the reason cleanly.** It prefixes the stage name (`cluster_routes: …`) and joins multiple reasons with `; ` (`app/sourcing/pipeline.py:100-115`).
- **The T9 and T10 ticket bullets agree with the T8 decided note.**

## Low

- **L1.** `tests/tools/test_cluster_routes.py:104-135`: no stage-level test covers "ambiguous is not degraded". If `ambiguous` ever started counting toward `failed`, nothing would catch it. The service test checks the count but not `degraded_reason`. **Fix:** add a `census_ambiguous` street to the unmatched test, then assert `routing_geocode_ambiguous == 1` and that `degraded_reason` is `None`.
- **L2.** `tests/tools/test_cluster_routes.py:30-135`: the three DB tests repeat about 25 lines of setup each: the stub, monkeypatch, manifest, run, record, `try/finally` and `StageContext`. **Fix:** extract one `_run_stage(db_session, monkeypatch, census, streets) -> StageResult` helper.
- **L3.** `app/tools/cluster_routes.py:33`: the wording differs slightly from `search_registry` (`"{failed} lookup(s) failed; those candidates lack that record"`, with no "of N"). The routing form is the more informative one. **Fix:** keep it. If wanted, align `search_registry` the next time that file is touched.
- **L4.** The stage now reads two keys from the service's counts. If either key were renamed, the stage would only fail with a `KeyError` on a run where some geocode failed. `tests/routing/test_service.py:72` already pins the full key set, so this is mostly covered. **Fix:** optional; add a comment by the counts literal.

## What's good

- **Small and test-first.** The degraded test failed before the change and passes after. The reason is worked out in the stage from the counts, so `RoutingService` is untouched.
- **One rule, stated the same way in four places:** the stage docstring, the README, the tickets doc and the work log. The rule: failed geocodes make the run degraded; unmatched and ambiguous results are answers, not failures.
- **Tests assert the exact reason alongside the counts** (`failed == 1`, `clustered == 2`), and cover the clean, partial-failure and unmatched cases.
- **The T9 bullet explains why filtering alone is not enough.** The remaining routes would be lopsided, so the re-route decision is left to T10 rather than settled here.

## Next step

Merge it as is, or first run `piv-fix-review-findings` for L1 and L2 (about 15 minutes).
