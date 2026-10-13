# PR #32 review: `verify_business` and the D8 stage order (T6 PR A)

**Verdict: request changes.** There are three High issues and no Critical ones. Validation passes.

The deep review was done by the `code-reviewer` agent in a clean context. Every High and Medium below was then checked
against the code. H1 was checked against the installed httpx. H2 was checked by running `email_domain`. H3 was reproduced
by running `match_place` on the two cases below.

The PR's design is sound. D13 holds everywhere it was checked, and cost is charged before every call. The three Highs
are each small, local fixes.

## Validation (head `25918da`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 181 files formatted |
| mypy strict | ✅ 183 files, no issues |
| pyright strict | ✅ 0 errors |
| pytest (DB tier, port 5436) | ✅ 1,263 passed, 2 skipped (paid live eval) |
| pytest (no DB) | ✅ 902 passed, 363 skipped |
| alembic check (test DB) | ✅ no drift; no migration in this PR |

The deviations documented in `.claude/reports/t6-enrichment-pr-a-report.md` count as intentional and are not flagged.
Tasks 12–19 and 22 belong to PR B.

## Issues

### High

**H1. `HomepageChecker.check` can raise, although its docstring says it never does, and that ends the whole run.**
`app/sourcing/website.py:75-92`
- **The gaps:**
  - Only `httpx.TimeoutException` and `httpx.HTTPError` are caught. In the installed httpx, `httpx.InvalidURL` subclasses
    `Exception` directly (checked).
  - A redirect to `https://x.com:abc/` makes `get` raise `InvalidURL`.
  - `urljoin(url, "//[bad")` raises `ValueError` (checked).
  - `httpx.URL(url)` at line 92 sits outside the `try`.
  - `verify_business._website` has no guard around the call.
- **Failure:** one prospect's website sends a malformed redirect. The stage raises at candidate N, and the run fails after N
  Places calls were charged. The batch goes back to the pool, and the next run pays for those calls again.
- **Fix:**
  - Catch `(httpx.HTTPError, httpx.InvalidURL, ValueError)`.
  - Move the `httpx.URL(url)` parse inside the `try`.
  - Add tests for a malformed `Location` and a domain that won't parse.

**H2. The website fetch follows redirects to any host before checking the host, and accepts an IP address as an email
domain.** `app/sourcing/website.py:49-84`
- **The gaps:**
  - The `allowed_hosts` check runs after the redirect loop, so every hop is fetched first, including `http://` targets.
  - `email_domain("a@169.254.169.254")` returns `169.254.169.254` (checked).
  - The census email is filed by the registrant, so whoever controls that domain controls where the engine sends a GET.
- **Failure:** blind SSRF from the operator's Mac or the planned VPS. For example, a redirect to a cloud-metadata endpoint or
  a LAN admin page. The response is thrown away, so this is lower impact. But the fix is a few lines, and it should land
  before the VPS move.
- **Second problem:** `client.get` buffers the whole body, and httpx's 5-second timeout applies to each read, not the whole
  request. A hostile page that drips bytes slowly can hold the stage open far longer than 5 seconds.
- **Fix:**
  - Inside the loop, before each request, refuse a hop whose scheme isn't `https` or whose host isn't in `allowed_hosts`.
  - In `email_domain`, refuse a domain that `ipaddress.ip_address` parses.
  - Fetch with `client.stream(...)` and read only the status and headers.
  - Bound the whole check with `asyncio.timeout(...)`.

**H3. `match_place` verifies the wrong business that shares a building, and accepts a suite number as the street
number.** `app/sourcing/sources/google_places.py:296-334`
- **Reproduced on the PR head:**
  - **Same building:** census `ACME LOGISTICS LLC, 4100 Spring Valley Rd, 75244` matches Places
    `Zenith Logistics, 4100 Spring Valley Rd Suite 500, …, 75244`. The only shared word is `LOGISTICS`.
  - **Suite number:** census `100 Main St, 75024` matches Places `Acme Logistics, 5000 Legacy Dr, Suite 100, …, 75024`.
    `_contains_number` finds `100` anywhere in the address.
- **Why High:**
  - "Owned ≠ verified" is the reason this stage exists, and freight brokers cluster in shared office buildings.
  - A wrong match is silent. The wrong business's place ID is stored as `business_check` and passes T9's promotion gate.
  - That business's name and types reach `classify_rollup` as evidence in PR B.
  - The docstring says "a shared office building is told apart by the name". That holds only when the shared word is rare.
- **Fix:**
  - Anchor the street number at the start of `formattedAddress`: `^\s*{number}(?!\d)`.
  - Make the name test stricter **without vertical words in code**. The stop-word docstring is right that `LOGISTICS` in a
    list would be vertical-specific code. Options:
    - (a) Require every distinguishing census word, or at least two, to appear in the Places name.
    - (b) Discount words many pool names share, computed from the pool the way `shared_email_domains` is.
  - (a) is simpler. (b) is data-driven. Either needs tests for both cases above.

### Medium

**M1. `search_query` and `match_place` don't agree on when a candidate can be checked, so some candidates pay for a call
that can never match.** `google_places.py:272-285` vs `:311-324`
- **The gap:** `match_place` also returns `None` in two cases `search_query` doesn't check:
  - a postal code with no leading 5 digits;
  - a name with no word of 3+ letters, for example `KC LLC` or `AJ INC`.
- **Failure:** those candidates are charged, then counted `places_not_found`. This is the waste the PO-box commit fixed for
  numberless streets.
- **Fix:** one precondition helper used by both functions: a 5-digit ZIP, a street number, and at least one name word.

**M2. A permanently closed listing earlier in Google's results hides a live listing later on.**
`google_places.py:326-334`, `verify_business.py:306`
- **Failure:** a closed former tenant with the same name word, number and ZIP is listed before the live business. The
  candidate is counted `places_closed`, gets no `business_check`, and drops out of routing and promotion.
- **Fix:** prefer the first match that isn't closed, and fall back to a closed one only if every match is closed.

**M3. The D13 structure guard doesn't protect `PlaceCheck` itself.** `tests/test_structure.py:343-346`
- **The gap:** the guard bans `PlacesEvidence` and `PlaceResult` from appearing as types in `CandidateFields`. It doesn't stop
  someone adding `display_name` to `PlaceCheck`, which is stored. Its import scan also covers only `app/tools/`.
- **Fix:**
  - Assert `set(PlaceCheck.model_fields) == {"place_id", "checked_address"}`.
  - Widen the import scan to all of `app/`, except `app/tools/verify_business.py` and the adapter itself.

### Low

- **L1. Stale CLI help.** `app/sourcing/cli.py:49-52` still says "(free sources only)". A default run now makes up to 150
  Places calls.
- **L2. The run refuses late when the manifest has no Places source.** `verify_business.py:143-147`
  - It only refuses after `search_registry` has pulled the census and run QCMobile over the batch.
  - The refusal is documented and the batch is handed back, so nothing is lost but time.
  - Checking the manifest before the pull would fail fast.
- **L3. "A retry pays nothing" holds only within one run.** `verify_business.py:260`
  - A failed run hands its batch back to the pool.
  - The next run's candidate rows start without a `business_check`, so the Places checks are paid for again.
  - The docstring should say so.

## Checked, and holds

- **D13:** no Places value other than the place ID reaches `candidate`, `source_records`, any table or a log.
  - The API key travels in a header.
  - Logged URLs have their query strings removed.
  - Writes are validated again, so `model_copy(update=...)` can't bypass the guard.
- **Cost:** `RunCost.charge` checks the cap and records the call in one step, before every `search_text`.
  - The cap trips the stage to degraded and skips the remaining Places checks. It doesn't crash.
- **Verified-address tie:** values are compared, not `retrieved_at`, so a changed census address un-verifies the check.
  - No stored `business_check` exists, so making `checked_address` required breaks no stored rows.
  - `email` defaults to `None`.
- **Census owner:** `has_principal` and the adapter both use `principal_or_none`, so the two can't drift. A refused officer
  is caught per record, and its name isn't logged.
- **Reorder:** `FIELD_OWNERS` is consistent. The upsert means `search_registry` only fills `owner`. No stage runs twice or
  is skipped.
- **Refusal paths:** a key that fails mid-batch fails the run unless `--allow-degraded` is passed. With the flag, Places is
  dropped for the rest of the batch. The flag reaches both stages.
- **House rules:** no `Any`, no suppressions, and log event names follow the pattern.

## Done well

1. `RunCost.charge` checks and records in one step, so a paid call that goes uncounted is ruled out by the code itself.
2. The Places "bad key" quirk was confirmed from a live call. It comes back as HTTP 400 with `API_KEY_INVALID` and is told
   apart from other 400s.
3. D13 is enforced in several places:
   - a guard that allows only Pro-tier fields;
   - a single call site;
   - the validator, with its one named `business_check` exemption;
   - a second validation on write;
   - mutation checks recorded in the report.
4. `principal_or_none` is the only test of "a citable person", shared by the backlog ordering and the adapter.
5. The degraded paths are honest. One refused write or one failed lookup doesn't sink the stage, and a refused paid batch
   goes back to the pool.

## Recommendation

**Fix H1–H3 before merging. Fix M1–M3 in this PR as well**, because each is small and has a clear test. The Lows can go
in with them or be deferred into PR B. Next step: `/piv-fix-review-findings .claude/code-reviews/pr-32-review.md`.

## Resolution (2026-10-12)

All nine findings were fixed in this PR, on the owner's call. H3 took option (a), the stricter name overlap. Details
and tests are in `_tasks/todo.md`, under *PR #32 review fixes*. After the fixes: 1,282 passed, 2 skipped. ruff,
format, mypy, pyright and `alembic check` are clean.
