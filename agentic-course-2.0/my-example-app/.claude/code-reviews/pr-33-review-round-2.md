# PR #33 review, round 2: the round-1 fixes (`8df4fe1`)

**Verdict: approve.** All 11 round-1 findings are closed. Each fix has a test that fails if the fix is reverted.
There are no Critical, High or Medium issues, and validation passes. GitHub won't let the author approve their own
PR, so this verdict is posted as a comment. The five Lows below are each small and none blocks the merge.

The deep review was done by a `code-reviewer` agent in a clean context: the fixes were written in another
context. L1, L3 and L5 were then checked against the code at `8df4fe1`.

## Validation (head `8df4fe1`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean |
| mypy strict | ✅ 203 files, no issues |
| pyright strict | ✅ 0 errors, 0 warnings |
| pytest (DB tier, Postgres 16 on port 5434) | ✅ 1,518 passed, 2 skipped (paid live eval); round 1 had 1,503 |
| alembic check | ✅ no drift; no migration in the fixes |

## Round-1 findings

| # | Status | How it was checked |
|---|---|---|
| M1 duplicate company | Closed | `_stamp_attempt` (`service.py:447`) commits the app clock before every company create, and the guard (`:572`) reads `updated_at`. The two-lost-creates test fails without the stamp. |
| M2 rejected prospect gets a task | Closed | `enrolling` rows join the batch read (`:719`) and follow the current value. `test_a_founder_who_then_rejects_gets_no_task` fails on revert. |
| M3 lost acceptance | Closed | Only an explicit `OBJECT_NOT_FOUND` withdraws a row; any other absence is counted `failed`. The dropped-read test fails on revert. |
| M4 `_Row` untested | Closed | The two-row lost-race test would fail with an expired-attribute error if the snapshot were removed. |
| L1–L7 | Closed | Each has a test except L7, which is documentation. |

**Regressions checked:**
- **Clock mixing in the recovery guard:** the stamp is the only thing that moves `updated_at` on a row without a
  company id. A row that was never stamped waits at most one extra interval.
- **Sync outcomes:** a row can't be enrolled twice or stranded in `enrolling`, and a dry run still equals the real
  pass.
- **Chunked reads:** batch reads merge not-found ids across 100-id chunks.
- **Cadence callers:** they catch `HubSpotError` before `ValidationError`, so an unreadable body takes the same
  per-prospect path with a new code.
- **A shape error after a create** is still ambiguous and is never retried.

## Issues

### Low

- **L1. The README still says recovery waits until the row is 10 minutes old.** `app/promotion/README.md:136`.
  - The wait now counts from the last create attempt.
  - **Fix:** "10 minutes after its last create attempt".
- **L2. `decided_at` keeps the accept time when an `enrolling` row moves back to `pending_review`.**
  `app/promotion/service.py:825`.
  - `transition` never clears a column, so the row keeps its accept time.
  - `enrolling → rejected` also overwrites the accept time with the reject time.
  - The effect is cosmetic.
  - **Fix:** document that `decided_at` is the latest decision a sync acted on, or clear it on that move.
- **L3. `_decide` ignores the compare-and-set result of its moves.** `app/promotion/service.py:784`, `:806`, `:825`.
  - **Failure:** promotion sync has no run lock. A manual sync overlapping the 07:00 launchd job can report the
    same rejection or withdrawal twice.
  - No data changes, but it's the pattern round-1 L6 fixed for `_mark_duplicate`.
  - **Fix:** skip the count and the decision when `_move` returns `None`.
- **L4. The dry-run test never includes an `enrolling` row.** `tests/promotion/test_review_sync.py:285`.
  - **Fix:** add one `enrolling` row that flips to `rejected` and one that flips to blank, then assert
    `dry.counts == real.counts`.
- **L5. The cadence's `invalid_hubspot_response` message no longer fits what it catches.**
  `app/cadence/service.py:381`, `app/cadence/adoption.py:602`.
  - Unreadable HubSpot bodies now arrive as `HubSpotResponseShapeError`. These branches now catch only validation
    done outside the gateway, but the message still says "HubSpot answered a body that failed validation".
  - **Fix:** reword it to name a validation failure in the cadence.

### Unconfirmed, not counted
HubSpot may answer a batch read with 404 when **none** of a chunk's ids exist. The reviewer recalls this from
experience but couldn't confirm it. If it's true, a lone deleted company makes `sync_reviews` raise on every sync
instead of withdrawing the row. This is not a regression: round 1's code behaved the same way. Check it on the first
live sync with a deleted company, which is already a Level 4 step.

## Routing

**AGENT FIXES**
- L1 `README.md:136`, L3 `service.py:784`, L4 `test_review_sync.py:285`, L5 `cadence/service.py:381`

**HUMAN DECIDES**
- L2 `service.py:825`: document `decided_at` as "the latest decision acted on", or clear it on the move back to
  `pending_review`.

**HUMAN READS**
- `app/promotion/service.py:700-850`: the reworked sync. One path handles both `enrolling` and `pending_review`
  rows.

**HUMAN TESTS**
- Level 4 at `app/promotion/service.py:772`: delete one promoted company in the portal, then run
  `lpe promotion sync --dry-run`. That answers the 404-versus-207 question above.

**FYI**
- No `Any` or suppressions. The new events `promotion.hubspot.body_unrecognised` and `promotion.service.review_unread`
  follow the naming convention.

## Recommendation
Merge. If you want the Lows in before merging, run `piv-fix-review-findings` on this report. L1, L3 and L5 take a
few lines each. Otherwise defer them to one issue. Then work through the PR body's Level 4 steps against the live
portal.
