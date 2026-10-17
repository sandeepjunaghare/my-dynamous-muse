# PR #38 review — feat(promotion): let unverified survivors reach review, flagged

**Recommendation: Request changes.** One High: the new `contradicted` refusal also catches
website-verified candidates, which this PR newly refuses. Everything else is Low.

## Summary

The PR carries out the founder's 2026-10-16 decision as specified:
- An unverified survivor (no Places listing, no own domain) now reaches `pending_review`, flagged
  `lpe_verification = none`.
- Only a current `mail_drop` or `other_business` verdict refuses.
- The judgment nodes keep skipping unverified candidates.

The rename `unverified` → `contradicted` is complete:
- No old name remains in `app/`, `tests/` or `alembic/`.
- The ledger never stores a refusal reason, so no persisted value can break on read.

`lpe_verification` is company-only, derived once in `company_write`, and provisioned idempotently. Recovery
rebuilds it from the candidate.

## Issues

### High

**H1 — The contradiction gate now refuses website-verified candidates that were promotable before**
(`app/promotion/eligibility.py`, the `has_contradicting_verdict()` branch).

- **Before this PR:** the gate was `not fields.is_verified()`. A candidate verified by its own-domain
  homepage was promoted even with a current `mail_drop` or `other_business` Places verdict for its
  address.
- **Now:** `has_contradicting_verdict()` is checked regardless of verification, so those candidates are
  refused as `contradicted`.
- **Why this conflicts:**
  - The decision was about candidates that are *unverified*, and nobody asked to narrow the rule for
    verified ones.
  - It also conflicts with PR #36's settled OQ-3. `has_routable_address` deliberately keeps
    `other_business` routable for website-verified candidates, because "a website verifies the business,
    not the door". As written, the router would put such a candidate on a route, but the gate never lets
    it reach HubSpot.

**Failure scenario:** a carrier with its own domain shares a building with a listed business. Places
returns `other_business`, and this PR refuses a prospect the previous release promoted (and routed).

**Fix:** contradiction refuses only what nothing positive verifies:
```python
elif not fields.is_verified() and fields.has_contradicting_verdict():
    reason = RefusalReason.contradicted
```
Also:
- Add a test: `website_verified(...)` plus a current `mail_drop` verdict, and the same with
  `other_business`, are both eligible.
- Update the eligibility docstring, the `RefusalReason.contradicted` docstring and the README bullet to
  say "an unverified candidate with a contradicting verdict".

If the founder actually wants website-verified mail drops refused, that is a new decision and belongs in
the ticket. It should not arrive as a side effect.

### Medium

None.

### Low

- **L1 — Stale counts in `ensure_properties`** (`app/promotion/properties.py:192`, `:195`): says "six
  properties" and "nine placements"; it is now seven and ten.
- **L2 — Stale counts elsewhere:**
  - `app/promotion/README.md:68` says "nine `property_present` events"; it should be ten.
  - `tests/promotion/test_properties.py:1` says "six custom properties"; it should be seven.
- **L3 — `Verification.website` is never exercised through the service.** Only `verification_of` covers
  it (`tests/promotion/test_mapping.py`). Add a `test_promote.py` case that promotes a website-verified
  candidate and asserts `lpe_verification == "website"` and `promoted_unverified == 0`.
- **L4 — `promoted_unverified` is asserted only on a real create.** The dry-run path (`service.py`, the
  `dry_run` branch) and the recovery path both go through `_Tally.add_created`, but neither has a test.
  Add a dry-run assertion and one recovery case.
- **L5 — CLI wording** (`app/promotion/cli.py`, `_created_line`): `verified none` reads oddly.
  `verification none` is clearer. Optional.

## Validation

Full tier, test DB `lpe-test-pg` on 5434:

| Check | Result |
|---|---|
| ruff check | ✅ All checks passed |
| ruff format | ✅ 211 files already formatted |
| mypy | ✅ no issues, 214 files |
| pyright | ✅ 0 errors |
| pytest | ✅ 1747 passed, 4 skipped (paid live evals) |
| alembic check | ✅ no drift |

## What's done well

- Reason ordering is documented in eligibility, in `RefusalReason` and in the README, and is tested,
  including stale verdicts and ordering against owner and suppression.
- `RecordMeta` stays closed (`extra="forbid"`, frozen). Its field-set pin is updated, and the
  company-only rendering is pinned by a test.
- `CompanyWrite.verification` is derived once, so the HubSpot value and the run report cannot disagree.
- `PROMOTE_COUNTS` keeps the zero-filled key contract. `promoted_unverified` is counted only in
  `add_created`, so ledger skips, duplicates and failures never inflate it, and re-runs stay idempotent.
- The property description tells the founder in plain words what "Not verified" means at review time.
- `classify_rollup` and `resolve_owner` are unchanged in behaviour, so the PR adds no paid spend.
- The superseded ticket text is marked as superseded rather than rewritten, and the dated run result keeps
  its original counter name.
