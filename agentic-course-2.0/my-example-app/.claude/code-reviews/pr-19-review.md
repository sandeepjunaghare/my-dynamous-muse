# PR #19 review: T8 routing slice, DFW route clustering

**PR:** https://github.com/sandeepjunaghare/my-dynamous-muse/pull/19 · `feat/t8-routing` → `main` · 30 files, +2543/−3
**Reviewed:** 2026-10-10. The `code-reviewer` agent ran a fresh-context deep pass. The author's session verified each finding against the code before it was recorded here.
**Recommendation:** **Request changes.** No Critical or High issues. But two Medium findings (M1, M2) break behaviour that the PR's own README and docstrings promise: "one failed geocode drops one candidate; every geocode failing is an error". Both fixes are small and local.

Intentional decisions in the plan (`.claude/plans/t8-route-clustering.md`, OPEN QUESTIONS and AMENDMENTS) were **not** flagged. That covers verified-only eligibility, the routing-owned table, `0007` revising `0005` until merge, ZIP labels and sequential geocoding. No implementation report exists for this branch. The plan's amendments served instead.

## Validation

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 125 files already formatted |
| `mypy .` (strict) | No issues, 124 files |
| `pyright` (strict) | 0 errors |
| `pytest` (database tier, throwaway Postgres on 5436) | **734 passed**, 0 skipped |
| `alembic check` | No new upgrade operations |

## Critical
None.

## High
None.

## Medium

**M1. A total geocode failure that isn't "unavailable" commits an empty result and erases the run's routes.**
- Location: `app/routing/service.py:97`.
- The guard is `misses.unavailable == len(eligible)`.
- Scenario: every eligible geocode fails, but some or all fail with an unreadable body (`GeocoderResponseShapeError`), for example after a Census response-shape change. The guard is then false. The service clusters nothing, runs `replace_run_assignments(run_id, (), {})`, commits, and reports `routing_clusters=0` as success. The previous good routes for that run are deleted.
- This contradicts `app/routing/README.md` ("If it fails for every address, the stage raises") and the service docstring.
- **Fix:** guard on `misses.unavailable + misses.unreadable == len(eligible)` (when `eligible` is non-empty). Raise the last routing error seen. Keep a `last_error: RoutingError | None` rather than only `last_unavailable`.

**M2. One unexpected exception on a single geocode aborts the whole stage.**
- Locations: `app/routing/geocoder.py` `_get` (catches only `httpx.TransportError`) and `geocode` (builds `GeoPoint` and `ProvenancedValue` outside the shape-error `try`).
- Scenario 1: `httpx.DecodingError`, for example a truncated gzip body, is a `RequestError` but not a `TransportError`. This was verified. It escapes unretried, bypasses the service's two `except` clauses, and fails the stage.
- Scenario 2: a coordinate outside `GeoPoint`'s bounds raises `pydantic.ValidationError`, which escapes the same way.
- Timeouts are fine: `httpx.TimeoutException` *is* a `TransportError` (verified).
- **Fix:** catch `httpx.RequestError` in `_get`. Move the point construction inside the `try` that maps to `GeocoderResponseShapeError`.

**M3. Tests do not pin the all-failed rule or the failure counters.** This is why M1 went unnoticed.
- Location: `tests/routing/test_service.py`, `test_the_geocoder_down_for_everyone_is_an_error` and `test_misses_are_left_out_and_counted`.
- The only all-failed test uses 503s, which is the one branch that works.
- **Fix:** add tests for all-unreadable bodies, for a mix of unavailable and unreadable, and for an unreadable body counting as `routing_geocode_failed` in the service. Add one test that a failed re-run raises and leaves the run's earlier `route_assignment` rows intact. The raise happens before the delete, so that should hold once M1 is fixed.

## Low

- **L1** `app/routing/service.py:141-142`: the "unreachable" `address is None` branch skips without counting. If `has_verified_address` ever changes, the counts stop reconciling. Count it, or narrow the type another way.
- **L2** `app/routing/models.py:39-48`: nothing enforces that `candidate_id` belongs to `run_id`. A composite FK would need `unique(run_id, id)` on `candidate`, which is T4's table. **Suggest:** document it as service-enforced in the model docstring rather than altering `candidate` in a parallel wave.
- **L3** `tests/routing/test_service.py`: injected `census.geocoder()` clients are never closed (harmless today). Close them in a fixture.
- **L4** `tests/tools/test_cluster_routes.py:39`: `lambda: census.geocoder()` breaks silently if the service ever passes arguments to `CensusGeocoder()`. Use `lambda *a, **k: …`.
- **L5** `tests/routing/test_geocoder.py`: no test covers 429 being retried, or a transport error followed by success. Parametrize the retry test, and add a `DecodingError` case alongside M2.
- **L6** `app/routing/service.py:168-172`: `routing.service.geocode_missed` at info is up to about 150 lines a run, and the counts already carry it. Move it to debug.

## What's good

- **The clustering is pure and dependency-free, with a correct proof.**
  - The reviewer independently confirmed the cap invariant (`n ≤ k·max_doors` at every split) and that no group can be empty, since `n ≥ k` gives `left ≥ k_left` and `right ≥ k_right`.
  - The property-style tests sweep n from 1 to 60 against three caps, partition checks included.
- **The determinism contract is real.**
  - Input is sorted into one canonical order and every later sort breaks ties on registry id, so float sums and rounded centroids come out the same in any input order.
  - Shuffled-input equality is tested.
- **D13 is enforced in the structure, not by convention.**
  - Census-only coordinates, with the citation host asserted in both the geocoder and service tests.
  - x→longitude is pinned by a fixture where x and y are not interchangeable, and confirmed against one live call.
- **The model and migration match by hand** (constraint names and types), are registered in `env.py`, and `alembic check` is clean.
- **"No live network" is a proven property.** `MockCensus` raises on any unscripted request, and the "never geocoded" assertion reads the recorded request log.
- **The transaction rules are followed.** The repository flushes and the service commits. Delete-then-insert is atomic in one transaction. An owned geocoder is closed and an injected one is left to its owner. No address appears in any log line.

## Next step

Run `piv-fix-review-findings` on this report: fix M1, M2 and M3, plus the cheap Lows (L1, L4, L5, L6, and L2 as a docstring). Then re-run `/piv-validate` and push.
