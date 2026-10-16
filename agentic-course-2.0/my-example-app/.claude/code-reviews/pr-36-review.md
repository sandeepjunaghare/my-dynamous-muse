# PR #36 review: verify by own-domain homepage and record Places misses (T6 follow-up)

**Verdict: fix M1 and M2 before merging.** There are no Critical or High issues, and validation passes.
GitHub won't let the author approve their own PR, so this verdict is posted as a comment.
- **M2** reopens PR #34 M3, which this PR says it closes. The owner tie can be satisfied by the person's own
  surname.
- **M1** can take a real business off the routes, and tell the judgment node it is a mail drop, because a
  UPS Store is in the same building.

M3 and M4 are fence-widening and visibility fixes. They can follow in a small PR or be logged. L1–L5 are polish.

The deep review was done by the `code-reviewer` agent in a clean context. Each finding was then checked
against the code at `40edf45`, and each failure scenario is reachable as described.

## Validation (head `40edf45`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 211 files formatted |
| mypy strict | ✅ 214 files, no issues |
| pyright strict | ✅ 0 errors, 0 warnings |
| pytest (DB tier, Postgres 16 on port 5434) | ✅ 1,696 passed, 4 skipped (all paid `live` evals) |
| alembic upgrade head / check | ✅ no new operations; head `0010_promotion` |

The deviations documented in `.claude/reports/t6-website-verification-report.md` and in the PR body are
intentional and are not flagged here. They are:
- `has_mail_drop_here()` is public.
- `PROMOTE_COUNTS` gained `refused_unverified`.
- `other_business` requires a distinguishing word.
- `_at_address()` was extracted.
- Two fixtures were updated for the tie.

The plan's Level 4 Places rerun is not done; it waits for the founder.

## Issues

### Medium

**M1. `mail_drop` fires on any mail-drop business in the same building, whatever its suite.**
- Where: `app/sourcing/sources/google_places.py:401-403` uses `_at_address` (`:335-343`). That test checks only
  the street number and the ZIP. The matcher deliberately ignores suites.
- Failure: a real tenant at "2100 Olympic Dr Ste 300" is searched. Google returns "The UPS Store, 2100 Olympic
  Dr Ste 120", or a Regus `business_center` in the same tower.
  - `classify_miss` returns `mail_drop`, and it wins on precedence.
  - Routing drops the candidate (`has_mail_drop_here`).
  - The classify prompt tells the node the address is a mail drop.
  - Strip centres and office towers, which is where small DFW brokers sit, are the common case.
- OQ-2 decided "at the address" for which results count. It did not weigh this consequence for `mail_drop`,
  the one kind that changes routing. The test `test_each_mail_drop_type_is_a_mail_drop` encodes the behaviour:
  a UPS Store at Ste 300 against a census address with no suite.
- Fix: count a result toward `mail_drop` only when its unit agrees with the census unit, or neither has one. A
  result at a *different* unit is evidence about a neighbour, not this door. Add a "UPS Store at a different
  suite" test.

**M2. The owner page tie can be satisfied by the surname itself, so PR #34 M3 is only partly closed.**
- Where: `app/sourcing/owner/judgment.py:262-275` (`_tied`), called at `:308`.
  - Off-site, `_tied` asks whether `name_words(quote)` shares the legal or DBA name's distinguishing words.
  - The quote must already contain the person's surname (`:306`).
  - The model chooses the quote.
- Failure A: legal name `RODRIGUEZ TRUCKING LLC`, or simply `RODRIGUEZ LLC`, which reduces to `{RODRIGUEZ}`.
  A directory quote "Maria Rodriguez, owner" for an unrelated Maria Rodriguez shares that word. `_names`
  passes, the tie passes, and an unrelated person is stored as the owner. Businesses named after the owner's
  surname are common in this ICP.
- Failure B: `AJ FREIGHT LLC` reduces to `{FREIGHT}` (AJ is too short). Any off-site quote containing "freight"
  ties. In the Places matcher the address anchors this known one-word limit (`schemas.py:206-209`). In the
  owner gate nothing does.
- Test gap: `test_one_shared_word_off_site_is_not_enough` uses only a two-word name.
- Fix:
  - Subtract `name_words(principal.name)` from the quote's words before comparing, and pass the principal
    into `_tied`.
  - Make an off-site tie fail closed when the business name has fewer than `MIN_SHARED_WORDS` distinguishing
    words. On-site still ties by host.
  - Add tests for a one-word name and for a surname equal to a business word.

**M3. Verification now unlocks the paid steps and promotion, but the free-mail fence is thin.**
- Where: `app/sourcing/website.py:37-45` has 26 `FREE_MAIL_DOMAINS`. The shared-domain threshold is 3 pool
  businesses (`:48`). The predicate is at `app/sourcing/schemas.py:604-620`.
- Failure: a broker emailing from `@earthlink.net`, `@windstream.net`, `@netzero.net`, `@juno.com`,
  `@frontier.com` or `@yahoo.co.uk`, with fewer than 3 such addresses in the pool.
  - The ISP's homepage answers 2xx HTML, so it is stored `web_lookup`.
  - `is_verified()` is true.
  - The candidate gets the judgment call (~$0.08), the owner call, routing and promotion.
  - Before this PR a wrong website cost nothing. Now it is the verification.
- Fix: widen the list with US ISP mail domains and the common country variants of the big providers. Add a test
  for a single-use ISP domain. Parked and registrar pages are a separate, documented B2 assumption.

**M4. Routing treats "door never checked" the same as "checked, not a mail drop".**
- Where: `app/sourcing/schemas.py:645-658`, used by `app/routing/service.py`.
- Failure: a website-verified candidate with no verdict is routable. A missing verdict also results from:
  - `places_not_checkable` (no street number or ZIP)
  - a tripped breaker
  - a failed lookup
  - a missing key with `--allow-degraded`

  In a degraded run, every website-verified candidate routes without its door ever being looked at.
- OQ-3 decided "route unless a mail drop", so this is mostly by design, and the impact is small: the breaker
  cap is above the batch size, and degraded mode is opt-in. The *never checked* sub-case is not discussed,
  and nothing makes it visible.
- Fix: at minimum, count it (for example `routing_door_unchecked`) so T10's Friday report shows it.

### Low

- **L1. IDN and `www.` asymmetry in `has_own_domain_website`** (`app/sourcing/schemas.py:616-620`).
  - `email_domain` keeps a Unicode domain, while the stored origin is punycode (`website.py`).
  - `www.` is stripped from the website side only.
  - Either way the candidate fails closed and stays unverified, but silently. `judgment._host` is a third
    copy of the host normalisation, so one shared helper would fix all three.
- **L2. The owner tie trusts the host of an `llm_inference` website** (`app/sourcing/owner/judgment.py:271`).
  - `classify_rollup` may fill `website` with a parent or franchisor homepage, and any page on that host
    then ties.
  - The plan says "any website", so this is intentional, but B2 refuses that same field as circular for
    verification.
  - Consider restricting the host test to `web_lookup` websites.
- **L3. `verify_website_only` is under-counted on a no-op retry** (`app/tools/verify_business.py:249-251`).
  - When the website is already stored and Places is skipped, `update` is empty.
  - The `continue` fires before the count at `:270`.
- **L4. Dynamic count key** (`app/tools/verify_business.py:408`).
  - `counts[f"places_{miss.kind.value}"]` raises `KeyError` mid-batch if `MissKind` grows without a
    `COUNT_KEYS` entry, because `counts` comes from `dict.fromkeys`.
  - Add a test that every `MissKind` has its key.
- **L5. Stale docstrings** (`app/promotion/exceptions.py:133-150`).
  - `RefusalReason` says declaration order is check order. The enum lists `dead_score` before
    `no_principal`, but eligibility checks it last (`eligibility.py:17-20`, `55-63`).
  - `PromotionRefusedError` doesn't mention `unverified`.

## Checked and fine

- **No leftover consumers.** Nothing left in `app/` treats `business_check is not None` as "verified". Every
  skip, T9 gate and routing decision goes through `is_verified()` or `has_routable_address()`.
- **Host matching:**
  - Subdomains other than `www.` don't match.
  - `hostname` lowercases.
  - Ports aren't in the stored origin.
  - Redirects are fenced before they are followed.
  - The stored origin is built from the final URL's host.
  - An `llm_inference` website never verifies.
- **Staleness:** checks and verdicts are compared by value against the current address. A verdict from an old
  address can't skip Places, block routing or reach the prompt.
- **D13:** only the kind, place ID and census address are stored. Logs carry the kind and place ID only. The
  prompt line is built from fixed phrases. The structure pin and the validator allow-list cover both fields.
- **Promotion:** `unverified` is checked after `disqualified` and before the owner. `PROMOTE_COUNTS` and the
  tally key agree.

## What's good

- One `is_verified()` predicate and one definition of routable, so each consumer change was a single line.
- `places_verdict` is a separate field from `business_check`, as OQ-1 argued. A miss can't silently pass any
  of the four old "verified" checks.
- A shared `_at_address` between the matcher and `classify_miss`, with the matcher's tests unchanged.
- Value-based staleness everywhere, and the empty-name-set guard in `_tied`, with a test.
- Owner omissions log a fixed reason only, and a test pins it.
- Strong fixtures, plus the ECHO and FOXTROT end-to-end cases covering both verdict paths through all five
  stages.

## Recommendation

**Request changes (as a comment):** fix M1 and M2. Fix M3 or log it. Log M4 and L1–L5, or fold them in.
Then re-run `/piv-validate`. The next step is `piv-fix-review-findings` on this file.
