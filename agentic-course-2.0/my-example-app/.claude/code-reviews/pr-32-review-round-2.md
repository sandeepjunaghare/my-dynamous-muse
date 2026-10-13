# PR #32 review, round 2: the fixes for round 1

**Verdict: approve once the three Mediums below are fixed or explicitly deferred.** All nine round-1 findings are
closed. There are no Critical or High issues, no regressions, and validation passes. The `code-reviewer` agent did
the deep pass in a clean context, because this session wrote the fixes. Every Medium and Low below was then checked
by running the code on the PR head.

## Validation (head `7ed9d8b`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 181 files formatted |
| mypy strict | ✅ 183 files, no issues |
| pyright strict | ✅ 0 errors |
| pytest (DB tier, port 5436) | ✅ 1,282 passed, 2 skipped (paid live eval) |
| pytest (no DB) | ✅ 921 passed, 363 skipped |
| alembic check (test DB) | ✅ no drift; no migration |

## Round-1 findings: closed

- **H1:** no exception escapes `HomepageChecker.check`.
  - `asyncio.timeout` around `stream` surfaces as `TimeoutError` after the response closes.
  - `url.join` and IDN decoding raise `InvalidURL` or `UnicodeError`, which is a `ValueError`, and both are caught.
- **H2:** the redirect fence holds.
  - The host comparison is exact: lowercased, with userinfo and port split off. Tricks like `acme.test@evil` and `%2e` fail it.
  - `stream` never reads the body.
  - The 10-second total bound is correct.
- **H3, M1, M2:** the suite-number and one-shared-word cases are fixed.
  - `search_query` and `match_place` now share one precondition.
  - A live match beats a closed one in either order.
- **M3:** `PlaceCheck`'s fields are pinned, and `import app.sourcing.sources.google_places as x` is caught.
- **L1–L3:** fixed.
  - The CLI preflight runs after a read-only manifest lookup, before any network call or write.
  - `--allow-degraded` still passes.

**Regressions:** none. Behaviour is unchanged for:
- a `www` redirect;
- a `301` with no `Location`;
- a relative `Location`;
- a `200` that isn't HTML;
- the counts in `verify_business`.

## Issues

### Medium

**M1. `email_domain` still accepts short-form IP addresses.** `app/sourcing/website.py:68-73`
- **Confirmed:** `a@127.1`, `a@0x7f.1` and `a@169.254.43518` (which is 169.254.169.254) all come back as domains.
  - `ipaddress.ip_address` only parses canonical dotted quads.
  - httpx treats these as hostnames, and the OS resolver turns them into loopback and link-local addresses.
- **Impact:**
  - TLS verification fails for an IP reached by such a name, so it's likely that no HTTP request is ever sent.
  - A TCP connection is still opened.
  - The docstring ("An IP address is no domain") and the README overclaim.
- **Fix:** refuse a domain whose last label doesn't start with a letter. No real top-level domain starts with a digit.

**M2. The D13 import guard misses the package form of the import.** `tests/test_structure.py:278-283`
- **Confirmed:** `_imports_from` returns `False` for `from app.sourcing.sources import google_places`.
- The repo already imports this way, for example `verify_business.py` with `http`.
- So a second module could import the Places adapter and the guard would stay green.
- **Fix:**
  - Also count an `ImportFrom` of `app.sourcing.sources` that has a `google_places` alias.
  - Count relative imports too.
  - Mutation-check both.

**M3. The docs overclaim what option (a) guarantees.** `google_places.py:81-90`, `app/sourcing/README.md`
- **Confirmed:** `AJ LOGISTICS LLC` reduces to the one word `LOGISTICS`, so it still matches "Zenith Logistics" in the same building.
- A longer name that shares two trade words with a neighbour matches too.
- This is a known limit of the chosen option, and the option stands.
- **The problem is the wording:** the docs say a shared trade word "cannot verify a neighbour".
- **Fix:** say that holds only when the census name has two or more distinguishing words that aren't shared with the neighbour.

### Low

- **L1. Ports aren't checked.** `website.py:111-114`
  - `https://acme.test:6379/` passes the host check.
  - Under the documented case where a domain's own DNS points at a private address, up to 4 hops can reach arbitrary ports. The docstring says "its first hop".
  - **Fix:** refuse a port other than none or 443 as `other_host`.
- **L2. A punycode email domain is always refused.** `website.py:108,113`
  - **Confirmed:** `httpx.URL("https://xn--bcher-kva.de/").host` returns `bücher.de`, which never equals the allowed host.
  - **Fix:** compare `raw_host`.
- **L3. The query can use a name the matcher can't compare.** `google_places.py:315`
  - When the DBA has no distinguishing words, for example `KC`, the query uses it even though the legal name has some.
  - The call is paid and likely misses.
  - **Fix:** build the query from the first of DBA and legal name that has distinguishing words.
- **L4. Unconfirmed: requiring the address to start with the number is stricter than before.** `google_places.py:337`
  - If Google ever puts a building name before the street ("Northpark Center, 8687 N Central Expy…"), the match becomes a silent `places_not_found`.
  - How often that happens is unknown. Worth a look during the Level 4 live run.

## Done well

- Every hop is checked before it's requested, and only its status and headers are read.
- One shared precondition makes "paid for a certain miss" impossible for the cases it covers.
- Each round-1 test failed first, and the widened guards were mutation-checked.
- The preflight keeps `run()`'s own refusal as a backstop, and the `Stage` hook question is recorded for T10.

## Recommendation

Fix M1–M3 in this PR. Each is a few lines plus a test, and M3 is wording only. L1–L3 are cheap enough to fold in
with them. L4 needs live data, so check it during the Level 4 run and log it rather than change it blind. After
that the PR is ready for a human to approve and merge, once freight v9 is active.
