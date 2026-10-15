# Implementation Report — T6 follow-up: website verification, the Places verdict, the owner page tie

**Plan**: `.claude/plans/t6-website-verification.md`   **Branch**: `feat/t6-website-verification`   **Status**: COMPLETE
(the plan's Level 4 paid/read-only Places rerun is not done; it waits for the founder)

## Summary

A candidate can now be verified two ways. Places can find it at its current address, or it can answer a
homepage on its own email domain (`CandidateFields.is_verified()`). `classify_rollup`, `resolve_owner` and
T9's gate all use that one test. When Places misses but finds something at the census address, the miss now
records **our** verdict in a new `places_verdict` field: `mail_drop`, `no_listing` or `other_business`, plus
the place ID and nothing else (D13). Routing skips a website-verified mail drop. The classify prompt gets the
verdict as an uncitable lead, in our own words. `admit_owner` now requires the cited page to be tied to this
business, which closes PR #34 M3. No migration; the head stays `0010_promotion`.

All four OQs were decided as recommended: a new field, the three kinds with the no-shared-word rule, route
unless a mail drop, and own host or name words in the quote.

## Tasks completed

- 1–2 `MissKind`, `PlacesVerdict`, `places_verdict`, the `_PLACES_ARTEFACTS` D13 allow-list,
  `has_own_domain_website` / `is_verified` / `has_current_verdict` / `has_mail_drop_here` /
  `has_routable_address` → `app/sourcing/schemas.py` (UPDATE)
- 3 `FIELD_OWNERS["places_verdict"]` → `app/sourcing/stages.py` (UPDATE)
- 4 `name_words` / `shares_name` / `MIN_SHARED_WORDS` moved, behaviour unchanged → `app/sourcing/schemas.py`,
  `app/sourcing/sources/google_places.py` (UPDATE)
- 5 `MAIL_DROP_TYPES`, `ADDRESS_TYPES`, `MissVerdict`, `classify_miss`, `_at_address` →
  `app/sourcing/sources/google_places.py` (UPDATE)
- 6 fixtures → `tests/sourcing/fixtures/places_{mail_drop,no_listing,other_business}.json` (CREATE)
- 8 `_Outcome`, the verdict path, the B4 skip, `verify_website_only`, four new counts →
  `app/tools/verify_business.py` (UPDATE)
- 10 skip on `is_verified()` → `app/tools/classify_rollup.py`, `app/tools/resolve_owner.py` (UPDATE)
- 12 `VERDICT_PHRASES`, the verdict line, the system-prompt rule → `app/qualification/prompts.py` (UPDATE)
- 13 verdict shape pin → `tests/test_structure.py` (UPDATE)
- 14 `has_routable_address`, `routing_mail_drop` → `app/routing/service.py` (UPDATE)
- 15 `_tied`, `admit_owner(answer, fields, *, reads)`, the owner prompt's quote rule →
  `app/sourcing/owner/{judgment,prompts}.py` (UPDATE)
- 17 `RefusalReason.unverified`, `business_check` out of `REQUIRED_FIELDS`, `refused_unverified` →
  `app/promotion/{exceptions,eligibility,schemas}.py` (UPDATE)
- 20 docs → `docs/tickets/local-prospect-engine.md` (T6 *Built* paragraph, T10 hand-over),
  `app/{sourcing,routing,promotion}/README.md`, `CLAUDE.md` (two map lines), `_tasks/todo.md`

## Tests added

- `tests/sourcing/test_schemas.py`: `TestPlacesVerdict` (shape, D13 allow-list, staleness), `TestWebsiteVerification`
  (www and bare host, another domain, `llm_inference`, free-mail, no email, routability under each verdict and
  an old one), `TestNameWords`.
- `tests/sourcing/sources/test_google_places.py`: `TestClassifyMiss`. It covers each mail-drop type, each
  address type, another business, Spearpoint (one shared word, so no verdict), another number, another ZIP, no
  address, no results, uncheckable, precedence both ways, a closed post office, a courier, a business listing
  never being `no_listing`, and each fixture through the real client.
- `tests/tools/test_verify_business.py`: `TestTheVerdict` checks that each fixture stores the right kind and no
  Places name, type or address. It also covers no verdict off the address, none on a match, a retry not paying
  twice, and an old address re-checking. `TestTheWebsite` gains a website-only verified case and the
  `verify_website_only` count.
- `tests/qualification/test_stage.py`, `tests/tools/test_resolve_owner.py`: a website-verified candidate is
  judged or resolved, and another domain or an `llm_inference` site is still skipped.
- `tests/qualification/test_judgment.py`: the verdict phrase per kind, no `ChIJ`, an old address left out, and
  the system-prompt rule.
- `tests/sourcing/owner/test_judgment.py`: `TestTheBusinessTie` covers the M3 scenario being omitted, the own
  site (www and bare) admitted, the DBA or legal name off-site admitted, one shared word refused, no website,
  and an empty name set failing closed. No name ever reaches a log.
- `tests/routing/test_service.py`: `TestWebsiteVerified` checks that no verdict, `no_listing` or
  `other_business` is routed, a mail drop is counted and never geocoded, and an old mail drop doesn't block.
- `tests/promotion/test_eligibility.py`: unverified, unverified before the owner, suppressed before unverified,
  website-verified promotable, website-verified with no owner as `no_principal`, and Places alone is enough.
- `tests/sourcing/test_end_to_end.py`: a second five-stage run (ECHO `no_listing`, FOXTROT `mail_drop`, both
  website-verified).

## Validation results

- `ruff check .`, `ruff format --check .`: clean (211 files)
- `mypy .`: no issues in 214 files · `pyright`: 0 errors
- `pytest` with the DB tier (`lpe-test-pg` on 5434): **1,696 passed, 4 skipped** (paid `live`), up from 1,607
- `alembic check`: no new upgrade operations

## Deviations from the plan

- **`has_mail_drop_here()` is public**, not the private `_mail_drop_here`. Routing needs it to count
  `routing_mail_drop` separately from `routing_unverified_address`.
- **`PROMOTE_COUNTS` gained `refused_unverified`.** The plan said `service.py` tallies the new reason with no
  change, but the report's count keys are a fixed, pre-seeded list. Without the key, the first `unverified`
  refusal would raise `KeyError`.
- **`other_business` requires the listing to have at least one distinguishing word.** The plan's "disjoint
  from every name" is vacuously true for an empty set. A business result with no usable name is no evidence that
  someone else is at the address, so it stays a plain miss.
- **`_at_address()` was extracted** in the Places adapter and is shared by `match_place` and `classify_miss`.
  This is a pure refactor, so "at the address" has one definition. The matcher's tests pass unchanged.
- **Two existing fixtures changed because of OQ-4, not the rule.**
  - In the E2E, BRAVO's owner quote became "Bryan Griggs, President of Bravo Logistics". It is off-site and
    BRAVO has no website, so the old quote would now be omitted.
  - The `resolve_owner` stage tests give their verified candidates the `www.acme.test` site that their cited
    page is on.
- **`app/promotion/README.md` was updated too.** It states the required set, which changed.
- **Test builders** `a_verdict`, `own_site`, `website_verified`, `OWN_DOMAIN` were added to
  `tests/sourcing/builders.py`.

## Issues encountered

- Two of the plan's derived consequences only surfaced in the code (`PROMOTE_COUNTS`, the owner fixtures). Both
  are recorded above.
- Not run: the plan's **Level 4** step (a read-only Places rerun over run `9ca51e17`'s five misses, then
  optionally a paid batch of 10). Both need the founder's go-ahead and make paid calls.
