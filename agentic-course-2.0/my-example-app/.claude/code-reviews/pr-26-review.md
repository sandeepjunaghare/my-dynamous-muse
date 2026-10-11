# PR #26 review: match QCMobile's live field spelling, `allowedToOperate`

**Verdict: APPROVE.** There are no Critical, High or Medium issues, and validation is green. Three Lows follow, all optional. *The PR was merged (`6071b12`) at the owner's request while this review was still running; nothing below would have blocked it.*

**Method:** The `code-reviewer` agent reviewed the PR in a clean context. It read every changed file in full against `CLAUDE.md` and the PR's stated intent. There is no implementation report, since this is a small follow-up. Validation was run on the PR's head commit, `0968c80`.

## Validation

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | ✓ / ✓ (144 files) |
| `mypy .` / `pyright` | ✓ / 0 errors; zero suppressions |
| `pytest`, with a throwaway Postgres | **876 passed, 0 skipped** |
| Live, read-only | `/carriers/7989` → 200, `allowedToOperate`. Unknown USDOT → `200 {"content": null}`. Bad key → 404 "Webkey not found". Missing key → 404 "Must provide Webkey" |

## Issues

### Low
- **L1. `app/sourcing/sources/fmcsa.py:13`: the module docstring still says "a v4 is needed".** v4 now exists, kept the wrong spelling, and v5 is the one to activate. **Fix:** reword it to "a later freight version declares Motus (v5 is the one to activate)". *(The reviewer also asked for confirmation of the ticket's v5 id, `6454f442…`. Checked: it is the draft `create_draft` returned in dev Supabase.)*
- **L2. `app/sourcing/README.md:195`: "manifests up to freight v4 used that and never matched".** This reads as if a rule ran and failed. T7 doesn't evaluate QCMobile rules yet. **Fix:** change it to "would never have matched".
- **L3. Pre-existing and out of scope: `_scalars` (`fmcsa.py:278-292`) stores QCMobile's `ein` for every looked-up carrier.** This PR keeps the real EIN out of the fixture, but the adapter persists the live one in `source_records`. It's public FMCSA data, so this may be fine. **Fix:** either exclude it on purpose, or note in the README that the full QCMobile scalar payload is stored.

## The six checks
1. **Old spelling:** none left by accident. The remaining mentions all explain that the docs say `allowToOperate`, or assert that it's absent. `alembic/` and `.claude/references/` contain neither spelling. The old name lives only in the manifests stored in the database (v3 and v4).
2. **The fixture is coherent.** It describes a plausible broker (census type B, broker authority A, bond required, 0 power units) and matches the census fixture row for 1000001. `MockFmcsa._found` reloads it on every call, so mutating `dotNumber` is safe.
3. **The refused-key case reaches the `"webkey" in content.lower()` branch.** It adds little coverage on top of the missing-key case, but it pins the live body.
4. **The bool test is meaningful.** If bools stopped being skipped, `False` (an `int`) would be stored and the test would fail. The `cast` follows the pattern `conftest._found` already uses.
5. **The doc claims match the code.** That covers the not-found and bad-key paths, `str | int` values, and the config comment.
6. **Nothing in T7, T8 or the seeds** uses the old field name.

## What's done well
- It's a pure alignment change and keeps to "store as returned, never rename".
- The fixture is built on the live key set with invented values, so no real EIN was committed.
- Bool-skipping stays covered after the live data stopped exercising it.
- The live 404 body is pinned, the stale `config.py` comment is fixed, and `todo.md` was appended to.

## Recommendation
Approved, and already merged. L1 and L2 are one-line doc fixes; fold them into the next sourcing PR. L3 needs a decision: keep the full QCMobile payload, or deny-list `ein`.
