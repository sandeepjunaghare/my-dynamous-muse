# PR #13 review: Places check as its own field (`business_check`)

**Recommendation: approve after fixing M1.** No Critical or High issues. The ownership merge works for the new
and moved fields, the rewritten tests keep the intent of the old ones, and `has_verified_address()` matches T8.
The Medium finding is a real bypass of the D13 guard, which the author reproduced. Fix it before T6 writes
`business_check`.

The review was done in a fresh context by the `code-reviewer` agent, reading every changed file in full. The
agent had no shell, so the author ran validation on the PR head (`defd830`) and reproduced the findings below.

## Validation

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean |
| `mypy .` · `pyright` | no issues · 0 errors (strict, zero suppressions) |
| `pytest`, with a database (Postgres 16) | **587 passed** |
| `pytest`, without one | 400 passed, 187 skipped |
| `alembic check` | no new operations (JSONB, no migration) |
| Mutations | phone back to `verify_business` fails 3 tests; guard disabled fails 5 |

## Issues

### Medium

**M1. The D13 guard runs only when a model is constructed, so `model_copy` bypasses it, and a bad row then
breaks every read of its run.** `app/sourcing/schemas.py` (`_no_places_content`), `app/sourcing/repository.py`
(`upsert_candidate`)
- `fields.model_copy(update={"phone": places_phone})` skips validators. Pydantic v2 does that by design, and the
  code base already uses `model_copy` in `_normalise_registry_id`.
- `upsert_candidate` dumps without re-validating, so the Places content is stored.
- Loading that row raises `ValidationError`, so `list_candidates` fails for the whole run.
- **Reproduced by the author.** The bypass constructs, and the reload fails.
- The README's "can never reach HubSpot" overstates the guard.
- **Fix:** re-validate in `upsert_candidate` before writing. Soften the README.

### Low

**L1. The guard's host matching has gaps.** `_is_google_maps`
- **Reproduced as passing the guard:** `https://maps.google.com./x` (trailing dot), `maps.google.com/x` (no
  scheme), `https://www.google.com/Maps/x` (case), `https://goo.gl/maps/abc`.
- The userinfo trick (`example.com@maps.google.com`) **is** caught.
- `source_url` comes from our own adapters, so this is a tripwire for our own bugs, not an attack surface.
- **Fix:** strip the trailing dot; parse a scheme-less URL; match `/maps` case-insensitively; add
  `goo.gl/maps`. Add a test per variant.

**L2. `has_verified_address()` does not tie the check to the address it checked.**
- A registry retry can replace the address after the check, and the candidate still reads as verified. Option
  A accepts this in principle.
- The suggested mitigation (`business_check.retrieved_at >= address.retrieved_at`) would un-verify a
  candidate on **every** registry retry, because a retry refreshes `retrieved_at` even when the address is
  unchanged. *Author's view: defer to T6, which owns the check and can compare like with like.*

**L3. Test gaps.** None of these is exercised through the SQL merge:
- a `verify_business` retry overwriting `business_check`;
- a non-owner unable to overwrite `business_check`;
- `verify_business` unable to overwrite `address` (only `phone` is tested).

The guard also has no bypass-variant or `model_copy` tests.

**L4. Documentation drift** in the sourcing README:
- the `unprovenanced_fields` example still reads `("phone", "website")`;
- "T8 (reads `fields.address`)" should name `has_verified_address()`;
- the guard's host list in the README omits `maps.app.goo.gl`.

The seeded fire manifest's `places` description contradicts D13. It is already logged for the manifest revision.

## Answers to the review questions
- **`unprovenanced_fields()`:** the only app caller is a log line in `upsert_candidate`. That adds log noise
  only.
- **Existing stored rows:** none cite Places, because T5 and T6 are not built yet. The load-path hazard is
  M1's.

## What's good
- The ownership merge (`fill || existing || owned`) is correct for every moved and new field.
- Nothing else keys on the old owners.
- `PlaceCheck` is frozen, so it passes the immutability walk in `ProvenancedValue`.
- Google *search* is told apart from Google *Maps*, with a test for it.

---

## Fixes (round 1)

The human approved fixing M1, L1, L3 and L4, and deferring L2. Each fix has a test, run against a real
Postgres.

| Finding | Outcome | Test |
|---|---|---|
| **M1** | **Fixed.** `upsert_candidate` re-validates with `CandidateFields.model_validate(...)` before it writes, so a `model_copy` cannot carry Places content past the guard. The README no longer overstates it | `test_places_content_slipped_in_by_model_copy_is_refused_at_the_write` (failed first; nothing is stored) |
| **L1** | **Fixed.** `_is_google_maps` parses a scheme-less URL, ignores a trailing dot, matches `/maps` case-insensitively and covers `goo.gl/maps` | 9 new variants refused (4 failed first) + 4 non-Maps Google URLs allowed |
| **L2** | **Deferred to T6**, logged on the T6 ticket. The suggested `retrieved_at` rule would un-verify on every registry re-run. T6 should compare address values | — |
| **L3** | **Added.** The merge through SQL: verification re-decides its check; another stage cannot replace it; verification cannot replace the census address. The code was already right, so these passed first | `TestOwnershipOfTheCheck` (3) |
| **L4** | **Fixed.** README: the `unprovenanced_fields` example, T8's rule, and the full host list | — |

**Validation after fixes:** ruff, mypy, pyright clean; **604 passed with the database**, 413 passed / 191
skipped without; `alembic check` clean.
