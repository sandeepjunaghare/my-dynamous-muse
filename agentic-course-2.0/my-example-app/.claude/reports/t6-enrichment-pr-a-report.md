# Implementation Report — T6 PR A: `verify_business`, the stage reorder, the census owner and email

**Plan**: `.claude/plans/t6-enrichment.md` (PR A per *AMENDMENTS* 2026-10-12, item 2)   **Branch**: `feat/t6-enrichment`   **Status**: COMPLETE (PR A); PR B not started

## Summary
Stage 2, `verify_business`, now checks every surviving candidate against Google Places (one Pro-fields-only
Text Search, matched deterministically) and stores only the place ID and the census address it checked. What
Places saw rides an in-memory, once-per-run channel (`StageContext.places`) for `classify_rollup`. The website
comes from a free homepage fetch of the census email's domain. The stages run in D8's order
(`classify_rollup` before `resolve_owner`). The census adapter fills `owner` and `email`. `RunCost.charge` couples
the cap check with the record. A manifest with no declared Places source, or a run with no key, refuses unless
`--allow-degraded` is passed.

## Tasks completed
- 1 `RunCost.charge` → `app/core/cost.py` (UPDATE)
- 2 `principal_or_none`, `has_entity_designator`, `PlaceCheck.checked_address`, value-compared `has_verified_address`,
  `HeadcountBand`, `CandidateFields.email` / `.headcount_band`, `PlacesEvidence` moved here → `app/sourcing/schemas.py`;
  imports in `app/qualification/{schemas,prompts,judgment}.py` (UPDATE)
- 3 reorder and owner rows → `app/sourcing/stages.py`; docstring → `app/tools/classify_rollup.py` (UPDATE)
- 4 settings → `app/core/config.py`, `.env.example` (UPDATE; the key was already on main, not re-declared)
- 5 census `owner` + `email`, `has_principal` → `app/sourcing/sources/fmcsa.py` (UPDATE)
- 6 `post()` on `_send` → `app/sourcing/sources/http.py` (UPDATE)
- 7–8 client, binding, matcher → `app/sourcing/sources/google_places.py` (CREATE)
- 9 → `app/sourcing/website.py` (CREATE); `shared_email_domains` → `app/sourcing/{pool,service}.py` (UPDATE)
- 10 channel → `app/tools/registry.py`, `app/sourcing/pipeline.py` (UPDATE)
- 11 → `app/tools/verify_business.py` (CREATE)
- 20 → `app/sourcing/cli.py` (UPDATE)
- 21 → `tests/test_structure.py` (UPDATE)
- 23 (PR A part) → `CLAUDE.md`, `app/sourcing/README.md`, `.claude/references/adding-a-vertical.md`,
  `docs/tickets/local-prospect-engine.md`, `docs/local-prospect-engine.architecture.md`, `_tasks/todo.md`

## Tests added
- `tests/core/test_cost.py` (`TestCharge`), `tests/core/test_config.py` (`TestEnrichmentSettings`)
- `tests/sourcing/test_schemas.py`: `principal_or_none`, `HeadcountBand` (round trip with `high=None`), the checked
  address and the changed-address truth table, D13 on `email` / `headcount_band`
- `tests/sourcing/test_stages.py`: the D8 order, owners of `email` and `headcount_band`, the registry may only fill `owner`
- `tests/sourcing/sources/test_fmcsa.py` (`TestCensusOwnerAndEmail`), `tests/sourcing/sources/test_http.py` (new)
- `tests/sourcing/sources/test_google_places.py` (new): request shape, key never in URL or log, parsing, bad key /
  403 / other 400 / 5xx / non-JSON, binding (fire docs URL, v9 API URL, first wins, never a registry adapter),
  blank key, Enterprise deny-list, the matcher table
- `tests/sourcing/test_website.py` (new), `tests/sourcing/test_places_channel.py` (new, DB)
- `tests/tools/test_verify_business.py` (new, DB, 20 tests): stored row holds no Places content, channel, disqualified
  never sent, retry pays nothing, closed vs temporarily closed, breaker, refusals (no source, no/blank key, bad key
  with and without the flag), website rules, refused write, run cost
- `tests/sourcing/test_cli.py` (`TestPlaces`)
- Fixtures: `tests/sourcing/fixtures/places_*.json`; mocks `MockPlaces`, `MockWeb`, `routed` in `tests/sourcing/conftest.py`

## Validation results
- ruff check, ruff format --check, mypy (183 files), pyright: all clean; zero suppressions
- pytest with the DB on 5436: **1262 passed, 2 skipped** (baseline 1127); offline: 901 passed, 363 skipped
- `alembic upgrade head && alembic check`: no new operations; head `0009_candidate_run_scoped_fks` (no migration)
- Mutation checks, each caught then reverted: mask gains `websiteUri`; display name copied into the stored check;
  `removed` filter dropped; `search_registry` owns `owner`; `has_verified_address` ignores the address; fresh dict
  per stage; breaker caught without stopping

## Deviations from the plan
- **Channel test location:** `tests/sourcing/test_places_channel.py`, not `tests/tools/`, because it needs the
  sourcing suite's `session_factory` fixture.
- **Blank-key test location:** with the Places adapter tests, not `tests/core/test_config.py`, so a core test does
  not import a sourcing adapter.
- **`tests/sourcing/builders._source` renamed `a_source`** (public), so other suites can declare a Places source
  without a private import.
- **Matcher, census street with no leading number:** the plan says the number is "required only when the census
  street starts with digits" and also that "a PO-box-only street can never match". These conflict. This PR follows
  the first rule and matches such a street on ZIP and name. Flagged for a decision.
- **Extra log event `sourcing.places.match_closed`** for a permanently closed match (place ID only), alongside the
  planned `match_found` / `match_missing`.
- **`http._send` logs the URL with any query string stripped**, not only the separate `params`. This is slightly
  stricter than before.
- **The end-to-end five-stage run** in *Testing strategy* is left to PR B, since it needs both judgment nodes.

## Issues encountered
- The sandbox's writable `.` is the main checkout, so every scripted edit and `uv` call in the worktree ran with the
  sandbox off; heredocs fail inside the sandbox.
- **Behaviour change to note:** freight v8 (ACTIVE on dev) declares no Places source, so `lpe sourcing run --vertical
  freight` now fails at `verify_business` (and hands its batch back) until freight v9 is activated, or
  `--allow-degraded` is passed.
