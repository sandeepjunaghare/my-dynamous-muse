# PR #38 review, round 2: feat(promotion): let unverified survivors reach review, flagged

**Recommendation: Approve.** The round-1 High and L1–L4 are fixed and tested. Validation is green. Four
new Lows remain, all stale wording that still describes the old "verification required" gate. None is a
code defect, so none blocks the merge.

Reviewed at `54e348e`, in a fresh context (the `code-reviewer` agent). Every Low below was checked
against the file before posting.

## Round-1 follow-up

| Finding | Status | Where |
|---|---|---|
| H1: contradiction refused website-verified candidates | Fixed | `app/promotion/eligibility.py:63` is now `not fields.is_verified() and fields.has_contradicting_verdict()`. Tested at `tests/promotion/test_eligibility.py:138` (mail_drop and other_business) and through the service at `tests/promotion/test_promote.py:232` |
| L1: stale counts in `ensure_properties` | Fixed | seven properties, ten placements |
| L2: stale counts in README and test docstring | Fixed | `app/promotion/README.md:68`, `tests/promotion/test_properties.py:1` |
| L3: `Verification.website` not exercised through the service | Fixed | `test_promote.py:232` asserts `lpe_verification == "website"` |
| L4: `promoted_unverified` on dry run and recovery | Fixed | `test_promote.py:588` (dry run), `:459` (recovery, also checks the CRM property) |
| L5: CLI wording `verified none` | Skipped (optional) | not re-raised |

## Issues

### Critical / High / Medium

None.

### Low

**L1: `app/sourcing/README.md` still describes the old gate.**
- `:199` says an unverified candidate cannot be promoted, citing OQ-5. That has been false since 2026-10-16.
- `:116` says "the paid stages and T9 ask `is_verified()`". T9 now uses `is_verified()` only to exempt a
  candidate from the contradiction check. It refuses on `has_contradicting_verdict()`.
- Fix: say "an unverified candidate is not paid for; it reaches review only with a census owner", and
  name `has_contradicting_verdict()` for T9.

**L2: the `CandidateFields.has_contradicting_verdict` docstring contradicts the H1 fix**
(`app/sourcing/schemas.py:649-650`).
- It says "T9's gate refuses on this, not on `is_verified()`". The gate is now
  `not is_verified() and this`.
- Fix: "T9's gate refuses an *unverified* candidate on this (founder, 2026-10-16); a verified one is exempt
  (website-verification OQ-3)."

**L3: the `RecordMeta` carve-out descriptions omit `verification`.**
- `app/promotion/exceptions.py:116` and `app/shared/provenance.py:186` both list the closed set as
  "vertical, review status, route label, owner". The set now has five fields.
- Fix: add "verification" to both lists.

**L4: the T9 "Required for promotion" bullet still lists `business_check`**
(`docs/tickets/local-prospect-engine.md:424`).
- The required set no longer includes verification. PR #36 began removing it and this PR completes that.
- Fix: add a dated "Superseded 2026-10-16" note, the same pattern as the T6 note at `:269`.

## Validation

Full tier, with the test DB `lpe-test-pg` on 5434.

| Check | Result |
|---|---|
| `ruff check` | pass |
| `ruff format --check` | pass (211 files) |
| `mypy` (strict) | pass (214 source files) |
| `pyright` (strict) | 0 errors, 0 warnings |
| `pytest` | **1,752 passed, 4 skipped** (the paid `live` evals); 2 warnings come from a third-party library |
| `alembic check` | no drift |

## What's good

- **The eligibility matrix is correct in every case.**
  - Places- or website-verified candidates promote whatever the verdict.
  - Unverified candidates with `no_listing`, no verdict, never checked, or a stale verdict promote,
    flagged `none`.
  - An unverified candidate with a current `mail_drop` or `other_business` is refused `contradicted`.
  - With no owner and no contradiction, the refusal is `no_principal`.
  - `disqualified` is checked first.
- **The gate and `has_routable_address` now agree**, so "a website verifies the business, not the door" holds
  end to end.
- **`lpe_verification` has one derivation.** It is derived once in `company_write` through
  `verification_of`, so create, dry run and recovery cannot diverge. It is company-only and pinned for both
  object types.
- **`promoted_unverified` is counted only in `add_created`**, so duplicates and failures can't inflate it.
- **`RecordMeta` stays closed** (`extra="forbid"`, frozen), and the field set is pinned by a test. The
  write-gate is untouched: `to_property_payload` still runs on both payloads before the create.
- **The verdict-staleness test uses a real different address**, not a mock.
- **Dry-run parity still holds** with the new counter: `dry.counts == real.counts`.
- **The `lpe_verification` property description** tells the founder what "Not verified" means before they
  accept a record.
- **No new paid spend.** `classify_rollup` and `resolve_owner` still skip unverified candidates.
- **No leftover reader** of `refused_unverified` or `RefusalReason.unverified` anywhere in `app/`, `tests/`,
  `CLAUDE.md` or the READMEs. What remains is dated history.
