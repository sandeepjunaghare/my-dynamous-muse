# Feature: T6 follow-up — website verification, the Places verdict, and the owner page tie

The following plan should be complete, but validate documentation and codebase patterns and task sanity before
you start implementing.

Pay special attention to naming of existing utils, types and models. `ProvenancedValue`, `RetrievalMethod` come
from `app.shared.provenance`; `PageRead`, `normalize_url` from `app.shared.page_reads`; `CandidateFields`,
`PlaceCheck`, `PlacesEvidence`, `PostalAddress`, `Principal`, `has_entity_designator`, `is_google_maps`,
`principal_or_none` from `app.sourcing.schemas`; `PipelineStage`, `FIELD_OWNERS` from `app.sourcing.stages`;
`email_domain` from `app.sourcing.website`; `StageContext`, `StageResult` from `app.tools.registry`;
`RefusalReason`, `PromotionRefusedError` from `app.promotion.exceptions`. Every exception derives from
`app.core.exceptions.LocalProspectEngineError`.

**Where this runs:** branch `feat/t6-website-verification`, cut from `main` at `820bc71` (sync `main` with
origin first; memory: `sync-main-before-committing`). Test database: any throwaway Postgres 16 container
(`docker ps` first; reuse `lpe-test-pg` on 5434 if it is up, never 5433, which is Langfuse's). **No migration**:
the new field is JSONB inside `candidate.fields`. Every `uv` and git write command needs the sandbox disabled
(memory: `uv-commands-need-sandbox-disabled`, `git-writes-need-sandbox-disabled`).

---

## OPEN QUESTIONS — decide before `/piv-implement`

The founder decided (b) and (c) on 2026-10-15 (ticket T6, *Decided 2026-10-15*). These four are what that
decision left open: the ticket lists the first three by name, and the fourth is PR #34's M3, which was carried
in. Each has a recommendation, and the tasks that change with the answer are tagged `[OQ-n]`. The plan is
written **to the recommendations**, so "go with the recommendations" is a complete answer.

### OQ-1. Where does the verdict live?

| Option | What | Against |
|---|---|---|
| **a (rec.)** | **A new field** `places_verdict: ProvenancedValue[PlacesVerdict] \| None`, owned by `verify_business`. `PlacesVerdict(kind, place_id, checked_address)`, cited to the place it names | One more `CandidateFields` field, one more `FIELD_OWNERS` row, and the D13 validator's allow-list grows from one field to two |
| b | Widen `PlaceCheck` with an `outcome` (`verified` · `mail_drop` · …) | `business_check is not None` means "verified" in four places today (`classify_rollup.py:59`, `resolve_owner.py:71`, `eligibility.py:32`, `has_verified_address`). Every one of them would silently start passing misses. T6's OQ-2 rejected exactly this shape for that reason |

**Recommendation: a.** A verified check and a miss are different facts, and they should be different fields.
Either way the D13 structure guard changes (`tests/test_structure.py:391-396`): under (a) a second pin is added
next to the first, which is left untouched. **Depends:** tasks 1, 2, 3, 13.

### OQ-2. Which answers become a verdict, and which `types` count as a mail drop?

Only results **at the census address** count: the address starts with the census street number and holds the
5-digit ZIP, which is the matcher's own test. A verdict is recorded only on a miss. A match still stores
`business_check` exactly as it does today. Precedence when several results are at the address is
`mail_drop` › `no_listing` › `other_business`.

| Kind | When | Rec. |
|---|---|---|
| `mail_drop` | A result at the address whose `types` include **`post_office`, `business_center` or `shipping_service`**. All three are Table A types (checked 2026-10-15: post_office *Government*, business_center *Business*, shipping_service *Services*) | ✅ record. `courier_service` and `coworking_space` are left out: a courier's depot and a coworking space are both real doors |
| `no_listing` | A result at the address whose `types` are all address types: **`street_address`, `premise`, `subpremise`** | ✅ record, **`subpremise` included**: a suite with nothing listed is still "no listing at the address" (AEROWAY). This kind does not change routing (OQ-3), so including it costs little |
| `other_business` | A result at the address that is a business which **shares no distinguishing word** with the legal or DBA name | ✅ record, with the no-shared-word rule. That rule is what keeps Spearpoint ("Spearpoint Logistics" at its own suite, one shared word) from being called another business. A one-word overlap stays a plain miss |
| — | No results, or nothing at the address | No verdict, counted `places_not_found` as today. There is no place ID to cite, and an empty answer is not evidence of anything |

The type lists are generic across verticals, so they live in code beside `CLOSED_PERMANENTLY`, the same way
`FREE_MAIL_DOMAINS` does. Options against the recommendation: drop `other_business` (it is the weakest of the
three, and the founder's decision named it), or add `courier_service`. **Depends:** tasks 4, 5.

### OQ-3. Is a website-verified business routed?

T8 decided on 2026-10-10 that "eligibility is a verified address" (`has_verified_address`, Places only). A
website verifies the **business**. It does not verify the **door**.

| Option | What | Against |
|---|---|---|
| **a (rec.)** | Route a candidate that is Places-verified, **or** website-verified with **no `mail_drop` verdict for its current address**. `no_listing` and `other_business` stay routable | A census address with nothing listed can still be a wasted stop. It is a stop on a route the founder is driving anyway, and judgment usually removes the residential shells first |
| b | Keep today's rule: route Places-verified only. Website-verified candidates reach review unrouted (call or email first) | Under the live numbers (1 of 18 Places-verified), routes stay almost empty, and door knocking is the cadence's second touch |
| c | Route website-verified only when there is no verdict at all | `no_listing` is the common answer for a small broker's office suite, so this is close to (b) |

**Recommendation: a.** **Depends:** tasks 2 and 14 (under b, `has_routable_address` collapses to
`has_verified_address`, and task 14 is dropped).

### OQ-4. Tying the owner's cited page to *this* business (PR #34 review M3)

Today `admit_owner` (`app/sourcing/owner/judgment.py:242-284`) checks the surname in the quote but nothing about
the business. Under (b), website-only candidates reach the owner node. They have no Places listing, so they lean
hardest on web pages, which raises the stakes.

| Option | What | Against |
|---|---|---|
| **a (rec.)** | Admit when the cited page is **on the candidate's own website host** (`example.com` or `www.example.com`). Otherwise the **quote must also carry the business's distinguishing name words**, using the Places matcher's rule (all of a short name, two of a longer one, legal name or DBA) | Drops a valid owner quoted from a directory that never names the company in the quoted passage. That is the safe direction (fails closed) |
| b | The name-word rule only, own site included | Drops the common own-site case: an About page that says "Dana founded the company…" never repeats the company name |
| c | Check the quote against the fetched text (T7 deferred #23) | Needs `ReadTracker` to keep page content, which it does not today (`app/shared/agent_reads.py:101`). It closes a different hole (an invented quote). Keep it a separate ticket |

**Recommendation: a.** It needs the name-word test outside the Places adapter, and the D13 guard bans importing
`google_places` anywhere but `verify_business` (`tests/test_structure.py:362-389`). So the test **moves** to
`app/sourcing/schemas.py` beside `has_entity_designator`, and the adapter imports it from there (task 6).
**Depends:** tasks 6, 15, 16.

---

## Derived decisions — flag at plan review if any is wrong

These follow from the ticket, the docs or the code. They are stated so a reviewer can object, not because they
are open.

- **B1. "Verified" becomes one predicate:** `CandidateFields.is_verified()` = `has_verified_address()` **or**
  `has_own_domain_website()`. Every paid-step skip and T9's gate use it, never `business_check is None`
  directly. Using `has_verified_address()` rather than `business_check is not None` is slightly stricter: a
  check of an address that has since changed no longer counts. That matches PR #13 L2.
- **B2. An own-domain website** is `website` cited `web_lookup` (verify_business's fetch, not the
  `llm_inference` homepage `classify_rollup` may fill), whose host without `www.` equals
  `email_domain(email)`. Values are compared, never trusted from a stage name. A free-mail address yields no
  domain. A shared domain was already refused at fetch time, so no website exists for it.
- **B3. Places still runs for website-verified candidates.** It is what verifies the door, and the verdict is
  what sends a mail drop off the routes. Cost is ~$0 under the free tier.
- **B4. A verdict for the current address is not paid for twice.** `verify_business` skips Places
  (`places_skipped`) when a verdict's `checked_address` equals the current address, exactly as it does for a
  verified check. This holds within a run, like `business_check`. Rows are run-scoped, so a handed-back batch
  asks again (PR #32 L3, unchanged).
- **B5. The verdict reaches `classify_rollup` as context, never as a citation.** The prompt gains one line
  (`Places verdict (context only, never cite): mail drop`) built from the stored kind only, with no Places name
  or type. The gate already refuses Google Maps citations, and evidence never backs a fired rule (PR #18 M1), so
  a `self_employed_shell` firing still has to cite a page the node read. The verdict is a lead for the node, not
  proof. A free rule on the verdict would be a manifest change, so it is out of scope.
- **B6. T9 refuses an unverified candidate as `RefusalReason.unverified`**, checked after `disqualified` and
  before missing fields. An unverified candidate never reaches the owner node, so without this it would be
  counted `no_principal` and hide the real gap. `business_check` leaves `REQUIRED_FIELDS`. Reasons are tallied
  per run (`app/promotion/service.py:285`), never persisted, so there is no migration.
- **B7. The counts T10 reads keep their names.** `judgment_unverified_skipped` and `owner_unverified_skipped`
  now mean "neither Places- nor website-verified". New counts: `places_mail_drop`, `places_no_listing`,
  `places_other_business` (each also counted in `places_not_found`), `verify_website_only`, and
  `routing_mail_drop` (routing's own prefix).

---

## Feature Description

Places verifies almost nothing in the live freight runs: 1 of 18 survivors, because small DFW brokers mostly have
no storefront on Google. Under OQ-5 almost nothing is therefore promotable. This ticket adds a second way to be
verified: a fetched homepage on the business's own email domain, which `verify_business` already stores. It
records Places' "nothing listed here" answers as our own verdict (the place ID and a kind, never Places content),
which takes a mail drop off the routes and gives the judgment node a lead. It also closes PR #34's M3, so the
newly reachable owner node cannot pin a same-named company's person on this business.

## User Story

As the founder reviewing the weekly list
I want businesses with their own working website to reach review even when Google has no listing for them,
and mail drops kept off my routes
So that the run yields promotable prospects instead of an empty list, and I never drive to a post office box.

## Problem Statement

- **Verification is Places-only** (`classify_rollup.py:59`, `resolve_owner.py:71`, `eligibility.py:32`,
  `routing/service.py:112`). The matcher is right (five correct misses re-read on 2026-10-15), but the
  population has no listings.
- **A miss stores nothing** (T6 OQ-2 a), so a post office at the census address is indistinguishable from "no
  match". Routing can't skip it, and judgment never hears about it.
- **The owner gate checks the surname, not the business** (PR #34 M3).

## Solution Statement

1. `CandidateFields` gains `places_verdict` and three predicates: `has_own_domain_website()`, `is_verified()` and
   `has_routable_address()`.
2. `google_places` gains `classify_miss(fields, results) -> MissVerdict | None`, which is pure and deterministic.
   `verify_business` stores the verdict on a miss.
3. `classify_rollup`, `resolve_owner` and T9's gate switch to `is_verified()`. Routing switches to
   `has_routable_address()`.
4. The classify prompt gains the verdict line.
5. `admit_owner` takes the candidate and enforces OQ-4's page tie. The name-word test moves to `schemas.py`.

## Out of Scope / Non-Goals

- **The Spearpoint false miss** (a T6 *Decide* item, still open). It isn't needed here: Spearpoint has a homepage
  on its own domain, so it becomes website-verified, and OQ-2's no-shared-word rule keeps it from being called
  `other_business`. Loosening the matcher stays a separate decision.
- **Candidates with no score** (a T6 *Decide* item) and re-authoring freight's `automatable` signals.
- **Showing the verification method or the verdict in HubSpot.** `business_check` is never mapped
  (`app/promotion/mapping.py:10`), and the verdict isn't either. If the founder wants to see "verified by website"
  at review, that is a `RecordMeta` property for a follow-up.
- **Quote-against-fetched-text checking** (T7 deferred #23; OQ-4 c).
- **A free disqualifier rule on the verdict.** That is a manifest change with a dry-run and activation.
- **Carrying verdicts across runs.** They are run-scoped like `business_check`.
- **A new manifest version.** Freight v9 stays active. Nothing here reads manifest data.
- **Not changing:** the Places matcher (`match_place`), the field mask, the breaker, the homepage fetch and its
  fences, the stage order, and the D13 rule that nothing but the place ID and our verdict is kept.

## Feature Metadata

**Feature Type**: Enhancement
**Estimated Complexity**: Medium (~600–900 lines, about half tests; four slices touched, no migration)
**Primary Systems Affected**: `app/sourcing/` (schemas, Places adapter, owner gate), `app/tools/`
(verify_business, classify_rollup, resolve_owner), `app/qualification/prompts.py`, `app/routing/service.py`,
`app/promotion/` (eligibility, exceptions)
**Dependencies**: none new

## Related Work

**Implements**: the T6 follow-up ticket, *Decided 2026-10-15 (founder): (b) and (c)* plus *Carried into that
ticket (PR #34 review M3)*, in `docs/tickets/local-prospect-engine.md` · **Epic**:
`docs/local-prospect-engine.architecture.md` (D8, D13 inherited unchanged)

**Back-references**:

- `.claude/plans/t6-enrichment.md` — OQ-2 (misses counted only; reopened here for verdicts) and OQ-5 (skip
  unverified; its meaning widened here). Its *rejected* row (a marker inside `business_check`) is why OQ-1 a.
- `.claude/plans/t6-enrichment-pr-b.md` — `admit_owner`, A10's rules, and the five-stage E2E test extended here.
- `.claude/plans/t8-route-clustering.md` — routing eligibility as decided on 2026-10-10, changed by OQ-3.
- `.claude/plans/t9-promotion.md` — `REQUIRED_FIELDS` (decided 2026-10-11), changed by B6.
- `.claude/code-reviews/pr-34-review.md` M3.

**Forward-references**: T10 reads the new counts (B7) in the Friday report.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ THESE BEFORE IMPLEMENTING

- `app/sourcing/schemas.py` (108-131 `PlaceCheck`; 150-180 `has_entity_designator`; 351-365 `PlacesEvidence`;
  367-400 `CandidateFields` fields; 456-470 `_no_places_content`; 494-507 `has_verified_address`) — where the new
  model, field and predicates go, and which patterns they copy.
- `app/sourcing/sources/google_places.py` (57-80 constants; 286-383 `_checkable`, `search_query`,
  `_name_tokens`, `_same_name`, `match_place`) — `classify_miss` reuses `_checkable` and the at-address test.
  `_name_tokens` and `_same_name` move out.
- `app/tools/verify_business.py` (whole file, 372 lines) — `_places` (270-358) returns the verdict path, and
  `run` (160-252) writes it. `COUNT_KEYS` at 75-87.
- `app/sourcing/stages.py` (51-79 `FIELD_OWNERS`) — one row added. `tests/sourcing/test_stages.py` fails until
  it is.
- `app/sourcing/website.py` (66-77 `email_domain`) — B2's comparison.
- `app/tools/classify_rollup.py` (53-61) and `app/tools/resolve_owner.py` (60-74) — the skip.
- `app/qualification/prompts.py` (64-100 `build_user_prompt`) — the verdict line goes next to the Places
  evidence block.
- `app/sourcing/owner/judgment.py` (196-284: `_index`, `_surname`, `_names`, `_omit`, `admit_owner`) — OQ-4.
- `app/promotion/eligibility.py` (whole, 75 lines) and `app/promotion/exceptions.py` (133-145 `RefusalReason`).
- `app/routing/service.py` (1-30 docstring diagram; 95-130 eligibility; 138-145 counts).
- `tests/test_structure.py` (355-424 `TestPlacesIsACheckNeverASource`) — the guards that must keep passing, plus
  one new pin.
- `tests/tools/test_verify_business.py` (53-133 helpers `_body`, `_candidate`, `_found`, `_stage`, `_setup`,
  `_stored`, `_run`; 141-265 `TestTheCheck`) — the pattern for every new stage test.
- `tests/sourcing/builders.py` (26-34 URLs; 63-95 `place_checked`, `a_candidate`) — add `a_verdict()`.
- `tests/sourcing/sources/test_google_places.py` — the `match_place` tests `classify_miss` mirrors.
- `tests/sourcing/owner/test_judgment.py` (50-120 helpers and the name-check params; 150-170 omission params) —
  `admit_owner`'s new argument.
- `tests/promotion/test_eligibility.py`, `tests/promotion/test_exceptions.py:30` (enumerates `RefusalReason` in
  order).
- `tests/sourcing/test_end_to_end.py` — the five-stage run. A website-only case is added.
- `tests/sourcing/fixtures/places_no_match.json` — the fixture shape for the three new fixtures.

### New Files to Create

- `tests/sourcing/fixtures/places_mail_drop.json` — a `post_office` at the census street number and ZIP.
- `tests/sourcing/fixtures/places_no_listing.json` — one `subpremise` result at the census address.
- `tests/sourcing/fixtures/places_other_business.json` — "Napco Bag & Film" at the census suite.

### Relevant Documentation

- [Place types (New), Table A and Table B](https://developers.google.com/maps/documentation/places/web-service/place-types)
  — `post_office`, `business_center`, `shipping_service` are Table A. `street_address`, `premise`, `subpremise`
  are Table B (address types returned on address results). Checked 2026-10-15.
- [Places API policies: caching, place IDs](https://developers.google.com/maps/documentation/places/web-service/policies#pre-fetching,-caching,-or-storage-of-content)
  — a place ID may be stored indefinitely. This is the basis for keeping the verdict's place ID (D13).
- `.claude/references/adding-a-vertical.md` — confirms nothing here is vertical data.

### Patterns to Follow

**A stored Places artefact** (copy `PlaceCheck`, `schemas.py:108-131`): frozen, no Places text, our own
`checked_address`, and `place_id` stripped and non-blank:

```python
class PlaceCheck(BaseModel):
    model_config = ConfigDict(frozen=True)
    place_id: str = Field(min_length=1)
    checked_address: PostalAddress
```

**Value comparison, never `retrieved_at`** (`has_verified_address`, `schemas.py:494-507`):

```python
return (
    self.address is not None
    and self.business_check is not None
    and self.business_check.value.checked_address == self.address.value
)
```

**Citing a Places artefact** (`verify_business.py:343-349`):

```python
ProvenancedValue(
    value=PlaceCheck(place_id=match.id, checked_address=fields.address.value),
    source_url=place_url(match.id),
    retrieved_at=self._clock(),
    retrieval_method=RetrievalMethod.web_lookup,
)
```

**Counts**: every count is pre-seeded at zero from `COUNT_KEYS` (`dict.fromkeys(COUNT_KEYS, 0)`) and carries
the stage's prefix. The runner refuses a name two stages share.

**Logging**: `domain.component.action_state`, IDs only, never a Places name or a person's name. For example:
`logger.info("sourcing.places.miss_recorded", registry_id=..., kind=verdict.kind.value, place_id=...)`.

**Owner gate omissions**: `_omit("fixed reason")` logs the fixed reason only (PR #34 M2). The new omission
follows that pattern: `_omit("the page is not tied to this business")`.

**Enums**: `StrEnum` with values equal to names (`RefusalReason`, `PipelineStage`).

**Docstrings**: dense prose that cites the decision id (D13, OQ-n, PR #n Mn), as the files above do. New
decisions in this plan are cited as `website-verification OQ-n`.

---

## IMPLEMENTATION PLAN

### Phase 1: The data model (`app/sourcing/schemas.py`, `stages.py`)

`PlacesVerdict`, `MissKind`, the `places_verdict` field and its owner, the D13 allow-list, the three predicates,
and the moved name-word test.

### Phase 2: The verdict and the website in `verify_business`

**Depends on:** Phase 1.
`classify_miss` in the adapter; `_places` returns a verdict on a miss; `run` writes it; the skip rule and the
counts.

### Phase 3: The consumers

**Depends on:** Phase 1. **Independent of:** Phase 2 (each consumer reads only the model).
`classify_rollup` and `resolve_owner` skips, the classify prompt line, routing, T9's gate, and the owner page tie.

### Phase 4: End-to-end, docs, validation

**Depends on:** Phases 2 and 3.

---

## STEP-BY-STEP TASKS

Execute in order. Run each task's VALIDATE before moving on.

### 1. ADD `MissKind`, `PlacesVerdict` to `app/sourcing/schemas.py` `[OQ-1, OQ-2]`

- **IMPLEMENT**: directly after `PlaceCheck`:
  - `class MissKind(StrEnum)`: `mail_drop`, `no_listing`, `other_business`. Each member gets a docstring with its
    founder meaning: a door that isn't there / nothing listed at the address / another business at the address.
  - `class PlacesVerdict(BaseModel)`: frozen, fields `kind: MissKind`, `place_id: str` (min 1, stripped, with the
    same `_not_blank` validator as `PlaceCheck`) and `checked_address: PostalAddress`. Its docstring must say
    *our* verdict, the place ID of the result it rests on, and no Places name, address or type.
- **PATTERN**: `PlaceCheck`, `schemas.py:108-131`.
- **IMPORTS**: `from enum import StrEnum` (check whether it is already imported).
- **GOTCHA**: no `types` field, no `display_name`. Task 13 pins the shape.
- **VALIDATE**: `uv run pyright app/sourcing/schemas.py`
- **SATISFIES**: AC 2

### 2. ADD the field and predicates to `CandidateFields`

- **IMPLEMENT**:
  - `places_verdict: ProvenancedValue[PlacesVerdict] | None = None`, declared right after `business_check`. Its
    docstring: what a Places miss told us (website-verification OQ-1), recorded only on a miss, absent when
    verified or when nothing was at the address.
  - `_no_places_content`: allow `{"business_check", "places_verdict"}` as a module-level
    `_PLACES_ARTEFACTS: Final = frozenset(...)`, not as two `if`s. Update the error message to name both.
  - `has_own_domain_website(self) -> bool` (B2): `website` and `email` present,
    `website.retrieval_method is RetrievalMethod.web_lookup`,
    `domain := email_domain(self.email.value)` is not `None`, and
    `(urlsplit(self.website.value).hostname or "").removeprefix("www.") == domain`.
  - `is_verified(self) -> bool` (B1): `self.has_verified_address() or self.has_own_domain_website()`.
  - `has_routable_address(self) -> bool` `[OQ-3]`: `has_verified_address()`, or `address is not None and
    has_own_domain_website() and not self._mail_drop_here()`. Here `_mail_drop_here` is true when
    `places_verdict` has kind `mail_drop` and its `checked_address == address.value`.
  - `has_current_verdict(self) -> bool` (B4): the verdict's `checked_address` equals the current address.
- **PATTERN**: `has_verified_address`, `schemas.py:494-507`.
- **IMPORTS**: `from urllib.parse import urlsplit`; `from app.sourcing.website import email_domain`.
- **GOTCHA**: check the import graph. `website.py` must not import `schemas.py` (today it imports only
  `core.logging` and `shared.provenance`), or this is a cycle. If a cycle appears, move `email_domain` and
  `FREE_MAIL_DOMAINS` into `schemas.py` and re-export them from `website.py`. Don't duplicate them.
- **GOTCHA**: `unprovenanced_fields()` will now list `places_verdict` for almost every candidate. Grep for any
  caller that treats that list as "everything missing" (`app/promotion/service.py` tallies only
  `refused.fields`, which come from `REQUIRED_FIELDS`, so it should be fine). Confirm.
- **VALIDATE**: `uv run pytest tests/sourcing/test_schemas.py -q`
- **SATISFIES**: AC 1, 2, 5

### 3. UPDATE `app/sourcing/stages.py` `FIELD_OWNERS`

- **IMPLEMENT**: `"places_verdict": PipelineStage.verify_business` with a comment: our verdict on a Places miss,
  the place ID and a kind only (D13).
- **VALIDATE**: `uv run pytest tests/sourcing/test_stages.py -q`
- **SATISFIES**: AC 2

### 4. MOVE the name-word test to `schemas.py` `[OQ-4]`

- **IMPLEMENT**: in `schemas.py`, next to `has_entity_designator`, add the public `name_words(name: str) ->
  frozenset[str]` (today's `_name_tokens`, body unchanged, with `_NAME_STOP_WORDS`, `_MIN_TOKEN_LENGTH` and
  `_LETTERS`) and `shares_name(ours, theirs) -> bool` (today's `_same_name`, with `MIN_SHARED_WORDS`). Move the
  `_MIN_SHARED_WORDS` docstring with it. In `google_places.py`, delete the private copies and import these.
- **PATTERN**: a pure move, no behavior change. `tests/sourcing/sources/test_google_places.py` must pass unchanged.
- **GOTCHA**: `google_places.py` already imports `has_entity_designator` from `schemas`, so the dependency
  direction is unchanged.
- **VALIDATE**: `uv run pytest tests/sourcing/sources/test_google_places.py -q && uv run pytest tests/test_structure.py -q`
- **SATISFIES**: AC 7

### 5. ADD `classify_miss` to `app/sourcing/sources/google_places.py` `[OQ-2]`

- **IMPLEMENT**:
  - Constants: `MAIL_DROP_TYPES: Final = frozenset({"post_office", "business_center", "shipping_service"})` and
    `ADDRESS_TYPES: Final = frozenset({"street_address", "premise", "subpremise"})`, each with a docstring citing
    the Table A/B source and website-verification OQ-2.
  - `class MissVerdict(NamedTuple)`: `kind: MissKind`, `place_id: str`. This is in-memory, like `PlaceResult`.
  - `classify_miss(fields, results) -> MissVerdict | None`: pure, no model. It is called **only after
    `match_place` returned `None`**, and says so in its docstring. It uses `_checkable(fields)` and returns `None`
    when that is `None`. "At the address" means `_contains_number(formatted, zip5)` and
    `_starts_with_number(formatted, number)`, the matcher's own tests. Over the at-address results, in Google's
    order:
    - the first with `types & MAIL_DROP_TYPES` gives `mail_drop`;
    - else the first with non-empty `types <= ADDRESS_TYPES` gives `no_listing`;
    - else the first with `types` not all address types and `name_words(display_name)` disjoint from every one of
      `checkable.names` gives `other_business`;
    - else `None`.
- **PATTERN**: `match_place`, `google_places.py:363-383`.
- **GOTCHA**: a `PERMANENTLY_CLOSED` post office is still a post office at the address. Don't filter on status.
  A result with no `formatted_address` is never at the address.
- **GOTCHA**: `types` on a business result usually include `establishment` and `point_of_interest`. Those are
  not in `ADDRESS_TYPES`, so a business is never `no_listing`.
- **VALIDATE**: `uv run pytest tests/sourcing/sources/test_google_places.py -q`
- **SATISFIES**: AC 3

### 6. CREATE the three fixtures

- **IMPLEMENT**: copy `places_no_match.json`'s shape. Each fixture must sit at the address `_candidate()` in
  `tests/tools/test_verify_business.py:67` uses (read it first).
  - `places_mail_drop.json`: `"types": ["post_office", "establishment", "point_of_interest"]`, display name
    "United States Postal Service".
  - `places_no_listing.json`: `"types": ["subpremise"]`, no `displayName`.
  - `places_other_business.json`: "Napco Bag & Film", `["store", "establishment"]`.
- **VALIDATE**: `python -m json.tool tests/sourcing/fixtures/places_mail_drop.json > /dev/null` (and for the
  other two)
- **SATISFIES**: AC 3

### 7. ADD `classify_miss` unit tests in `tests/sourcing/sources/test_google_places.py`

- **IMPLEMENT**: a `TestClassifyMiss` class, parametrized where it can be:
  - each mail-drop type gives `mail_drop`;
  - `street_address`, `premise` and `subpremise` each give `no_listing`;
  - a disjoint business at the address gives `other_business`;
  - **Spearpoint**: "Spearpoint Logistics" vs `SPEARPOINT FREIGHT SERVICES LLC` gives `None`;
  - a result at another street number gives `None`;
  - another ZIP gives `None`;
  - an empty list gives `None`;
  - an uncheckable candidate gives `None`;
  - precedence: a business listed before a post office at the same address still gives `mail_drop`;
  - `courier_service` alone at the address gives `other_business` or `None` by name, never `mail_drop`.
- **VALIDATE**: `uv run pytest tests/sourcing/sources/test_google_places.py -q`
- **SATISFIES**: AC 3

### 8. UPDATE `app/tools/verify_business.py` — record the verdict `[OQ-1, OQ-2]`

- **IMPLEMENT**:
  - `_places` returns `Checked | ProvenancedValue[PlacesVerdict] | None`. The cleanest typed form is a small
    frozen dataclass `_Outcome(check: Checked | None, verdict: ProvenancedValue[PlacesVerdict] | None)`. Avoid a
    bare union.
  - On `match is None`: increment `places_not_found` as today, then `miss = classify_miss(fields, results)`. If
    it is not `None`, build `ProvenancedValue(PlacesVerdict(kind, place_id, checked_address=fields.address.value),
    source_url=place_url(miss.place_id), retrieved_at=self._clock(), retrieval_method=web_lookup)`, increment
    `places_{kind}`, and log `sourcing.places.miss_recorded` (registry_id, kind, place_id; **never** the name).
  - The skip at line 283 becomes `fields.has_verified_address() or fields.has_current_verdict()` (B4).
  - `run`: put `update["places_verdict"] = ...` when present. Leave the `context.places` evidence for verified
    checks only (unchanged).
  - After writing, count `verify_website_only` when the stored fields are `has_own_domain_website()` and not
    `has_verified_address()`. Compute it on the copy you wrote, not on the stale `fields`.
  - `COUNT_KEYS` += `places_mail_drop`, `places_no_listing`, `places_other_business`, `verify_website_only`.
  - Update the module docstring: point 2 gains "a miss at the census address records our verdict", plus a
    paragraph on website verification (website-verification OQ-1/OQ-2, B3).
- **PATTERN**: the existing match path, lines 330-358.
- **GOTCHA**: a permanently closed **match** still stores nothing and gets **no** verdict (A4 unchanged).
  `classify_miss` runs only when `match_place` returned `None`.
- **VALIDATE**: `uv run pytest tests/tools/test_verify_business.py -q`
- **SATISFIES**: AC 3, 4

### 9. ADD stage tests to `tests/tools/test_verify_business.py`

- **IMPLEMENT**: a `TestTheVerdict` class using `_setup`, `_stage`, `_stored` and `_run`:
  - each fixture stores a `places_verdict` of the right kind, cited to `place_url(<id>)`, with
    `checked_address` equal to the census address. Assert `stored.business_check is None` and assert that
    `model_dump_json()` contains neither the fixture's display name nor its `formattedAddress` (D13 at the row);
  - `places_no_match.json` (wrong street number) stores no verdict;
  - a retry with a current verdict makes **no** Places request (copy `test_a_retry_makes_no_places_call`);
  - a verdict for an old address does not stop a new check;
  - the counts.
- Add to `TestTheWebsite`: with a working homepage on the email domain and `places_no_match.json`, the stored
  candidate `is_verified()` and `verify_website_only == 1`.
- **VALIDATE**: `uv run pytest tests/tools/test_verify_business.py -q`
- **SATISFIES**: AC 3, 4

### 10. UPDATE `app/tools/classify_rollup.py` and `app/tools/resolve_owner.py` — the skip

- **IMPLEMENT**: `if fields.business_check is None:` becomes `if not fields.is_verified():` at
  `classify_rollup.py:59` and `resolve_owner.py:71`. Update both module docstrings (OQ-5 now means "neither
  Places- nor website-verified", website-verification B1). Keep the count names (B7).
- **GOTCHA**: `context.places.get(...)` stays as it is. A website-only candidate has no evidence, and the prompt
  says `places=None`.
- **VALIDATE**: `uv run pytest tests/qualification/test_stage.py tests/tools/test_resolve_owner.py -q`
- **SATISFIES**: AC 4

### 11. ADD tests for the widened skip

- **IMPLEMENT**: in each of the two stage test files, a website-only candidate (an own-domain `website` cited
  `web_lookup` plus a business `email`, no `business_check`) **is** judged or resolved. A candidate whose website
  is on another domain, or is cited `llm_inference`, is still skipped as unverified.
- **VALIDATE**: same as task 10.
- **SATISFIES**: AC 4

### 12. UPDATE `app/qualification/prompts.py` — the verdict line (B5)

- **IMPLEMENT**: in `build_user_prompt`, when `candidate.has_current_verdict()`, append `""`, then
  `"Places verdict (context only, never cite):"`, then one fixed phrase per kind from a module-level
  `Mapping[MissKind, str]`:
  - `mail_drop`: "the census address is a mail drop (a post office or shipping store), not an office";
  - `no_listing`: "nothing is listed at the census address";
  - `other_business`: "another business is listed at the census address".
  Mention in the system prompt (`SYSTEM_PROMPT`, the rule on Places evidence) that the verdict is a lead and is
  never cited.
- **GOTCHA**: never interpolate the place ID, a type string or a name. The phrase is ours.
- **VALIDATE**: `uv run pytest tests/qualification -q -k prompt`, then add a test asserting each phrase appears
  and that no `ChIJ` place ID does.
- **SATISFIES**: AC 6

### 13. UPDATE `tests/test_structure.py` — pin the verdict `[OQ-1]`

- **IMPLEMENT**: next to `test_a_stored_check_holds_the_place_id_and_our_own_address_only`, add
  `test_a_stored_verdict_holds_a_kind_the_place_id_and_our_own_address_only`:
  `set(PlacesVerdict.model_fields) == {"kind", "place_id", "checked_address"}`. Leave the existing pin and the
  importer set (`{"app/tools/verify_business.py"}`) unchanged: `classify_miss` lives in the adapter.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q`
- **SATISFIES**: AC 2

### 14. UPDATE `app/routing/service.py` `[OQ-3]`

- **IMPLEMENT**: eligibility at line 112 uses `has_routable_address()`. Count a candidate that is
  website-verified but has a current `mail_drop` verdict as `routing_mail_drop`, separately from
  `routing_unverified_address`, and add it to the counts dict (138-145). Update the docstring diagram (lines 3-12)
  and the "Eligibility is…" paragraph: website-verification OQ-3 amends the T8 decision of 2026-10-10.
- **VALIDATE**: `uv run pytest tests/routing tests/tools/test_cluster_routes.py -q`, then add tests:
  website-verified with no verdict is routed; with `no_listing` it is routed; with `mail_drop` it is counted
  `routing_mail_drop` and never geocoded; with a `mail_drop` verdict for an **old** address it is routed.
- **SATISFIES**: AC 5

### 15. UPDATE `app/sourcing/owner/judgment.py` — the page tie `[OQ-4]`

- **IMPLEMENT**:
  - Signature `admit_owner(answer, fields: CandidateFields, *, reads)`.
  - After the agent check, add `_tied(read.url, citation.quote, fields)`. It is true when the read's host
    without `www.` equals `fields.website`'s host without `www.` (any `website`; the founder's own site is the
    business's page whoever found it), or when `shares_name(names, name_words(quote))` holds for the legal or DBA
    name's words.
  - Else `_omit("the cited page is not tied to this business")`.
  - Update the module docstring bullet "A name is held to what was read" with this rule and cite PR #34 M3.
  - Update `resolve_owner.py` to pass `fields`.
- **GOTCHA**: a name with no distinguishing words (`AJ LLC`) gives an empty set. `shares_name(empty, …)` must be
  **false**, not vacuously true. Check `min(len(ours), MIN_SHARED_WORDS)`: with `ours` empty it is `0 >= 0`, which
  is true, so guard explicitly (`ours and shares_name(...)`).
- **GOTCHA**: the owner prompt (`app/sourcing/owner/prompts.py`) should ask for a quote that names both the
  person and the business, unless the page is the business's own site. Otherwise the model will quote the
  shortest passage and fail the gate.
- **VALIDATE**: `uv run pytest tests/sourcing/owner -q`
- **SATISFIES**: AC 7

### 16. UPDATE `tests/sourcing/owner/test_judgment.py`

- **IMPLEMENT**:
  - Pass a `fields` to every `admit_owner` call (builder: `a_candidate()` with a `website`).
  - New cases: the M3 scenario ("Dana Reyes, President" on `other-acme.test` with no name words) is omitted with
    the new reason; the same quote on the candidate's own host is admitted; an off-site quote carrying the DBA's
    words is admitted; a name with no distinguishing words is never tied off-site.
  - The omission-reasons parametrization gains the new reason. The "no name ever reaches a log" test still holds.
- **VALIDATE**: `uv run pytest tests/sourcing/owner tests/tools/test_resolve_owner.py -q`
- **SATISFIES**: AC 7

### 17. UPDATE `app/promotion/exceptions.py` and `eligibility.py` (B6)

- **IMPLEMENT**:
  - `RefusalReason.unverified = "unverified"`, placed after `disqualified` in declaration order.
  - In `eligibility.py`, drop `"business_check"` from `REQUIRED_FIELDS`. Check order: `suppressed` gives
    `disqualified`; `not fields.is_verified()` gives `unverified`; then missing and `no_principal`; then
    `dead_score`. The `fields=` on the error still names the missing required fields.
  - Update the module docstring's required-set paragraph: verification is Places **or** an own-domain website,
    website-verification B6.
- **GOTCHA**: `tests/promotion/test_exceptions.py:30` asserts the enum's order. Update it.
  `app/promotion/service.py:285` tallies `refused_unverified` with no change.
- **VALIDATE**: `uv run pytest tests/promotion -q`
- **SATISFIES**: AC 4

### 18. ADD eligibility tests in `tests/promotion/test_eligibility.py`

- **IMPLEMENT**:
  - website-verified and owned is promotable;
  - neither Places- nor website-verified is `unverified` (not `no_principal`, even with no owner);
  - suppressed and unverified is `disqualified`;
  - Places-verified behaves as before.
- **VALIDATE**: `uv run pytest tests/promotion/test_eligibility.py -q`
- **SATISFIES**: AC 4

### 19. UPDATE `tests/sourcing/test_end_to_end.py`

- **IMPLEMENT**: one more five-stage case. The candidate has an own-domain homepage, Places returns
  `places_no_listing.json`, and the stubbed nodes answer. Expected:
  - `places_verdict.kind == no_listing`;
  - `classify_rollup` and `resolve_owner` both ran (`classified == 1`, `owner_calls == 1`, or `owner_from_registry`
    if the census named one);
  - `cluster_routes` routed it.
  A second candidate with `places_mail_drop.json` is judged but **not** routed (`routing_mail_drop == 1`).
- **PATTERN**: the existing E2E case in the same file.
- **VALIDATE**: `uv run pytest tests/sourcing/test_end_to_end.py -q` (database tier: export `TEST_DATABASE_URL`)
- **SATISFIES**: AC 3–6

### 20. UPDATE docs

- **IMPLEMENT**:
  - `docs/tickets/local-prospect-engine.md`: under T6, a **Built** paragraph for this follow-up (what was decided
    for each OQ, the new counts for T10). In T10's AC, add the counts to the hand-over line and note the T9 gate
    change.
  - `app/sourcing/README.md` and `app/routing/README.md`: the verdict and routing eligibility.
  - `CLAUDE.md`, *Architecture map*: amend the `verify_business.py` and `google_places.py` lines in one clause
    each. Keep it lean; don't add a paragraph.
  - `_tasks/todo.md`: tick this plan's list and append the *Review*.
- **VALIDATE**: `/rules-check-drift` (advisory)
- **SATISFIES**: AC 8

### 21. VALIDATE everything

- **VALIDATE**: `/piv-validate` (ruff check + format, mypy, pyright, pytest with the DB tier, alembic check: no
  new operations expected).
- **SATISFIES**: AC 9

---

## TESTING STRATEGY

### Unit Tests

- `classify_miss`: pure, table-driven (task 7).
- `CandidateFields` predicates: `has_own_domain_website` (www vs bare host, another domain, `llm_inference`
  website, free-mail email, no email), `is_verified`, `has_routable_address`, `has_current_verdict`, and the D13
  validator accepting a Google-cited `places_verdict` while still refusing a Google-cited `website`
  (`tests/sourcing/test_schemas.py`, next to 196-252).
- `admit_owner` page tie (task 16). The prompt verdict line (task 12). Eligibility (task 18).

### Integration Tests (database tier)

- `verify_business` against the fixtures through `httpx.MockTransport` (task 9).
- The stage skips (task 11), routing (task 14), and the five-stage E2E (task 19).

### Edge Cases

- A `mail_drop` verdict on a Places-**verified** candidate is impossible: a verdict is written only on a miss.
  Assert in task 9 that a match leaves `places_verdict` absent.
- The census address changes between runs: the old verdict no longer skips Places or blocks routing.
- A website on `www.` vs the email's bare domain is the same business. A website on `acme.com` with email at
  `acmefreight.com` is not own-domain.
- The owner page tie with an empty name-word set fails closed.
- Spearpoint: no `other_business`, and website-verified if its homepage answers.
- A post office listed as `CLOSED_PERMANENTLY` is still a mail drop.
- A Places search with no results, or one that failed, stores no verdict.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style

`uv run ruff check . && uv run ruff format --check .`
`uv run mypy . && uv run pyright`

### Level 2: Unit Tests

`uv run pytest tests/sourcing tests/qualification tests/promotion tests/routing tests/test_structure.py -q`

### Level 3: Integration Tests

`docker ps` to find the throwaway Postgres, then
`export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:<port>/postgres && uv run pytest -q`
`uv run alembic check` (no new operations expected).

### Level 4: Manual Validation — **paid, ask the founder first**

1. Rerun Places read-only over run `9ca51e17`'s five misses (the 2026-10-15 rerun script pattern; nothing stored)
   and print what `classify_miss` returns for each. Expected: DBT is `other_business`, Optimal is
   `other_business`, Isla is `no_listing`, Ark is `no_listing`, and Unishippers 1759 is `None` (the listing is 70
   miles away, so nothing is at the address).
2. With approval: `uv run lpe sourcing run --vertical freight --batch-size 10` on dev (~$0.10–1.00), then
   `uv run lpe promotion promote --run <id> --dry-run`. Read `verify_website_only`,
   `places_{mail_drop,no_listing,other_business}`, `judgment_unverified_skipped` and `refused_unverified`.
   **No real `promote`**: HubSpot writes only through `lpe`, and only when the founder says so.

---

## ACCEPTANCE CRITERIA

- [ ] AC 1: `CandidateFields.is_verified()` is true for a Places-verified candidate or an own-domain homepage cited
      `web_lookup`, and false otherwise (B1, B2).
- [ ] AC 2: `places_verdict` exists, is owned by `verify_business`, holds exactly `kind`, `place_id` and
      `checked_address`, and is the only field besides `business_check` that may cite Google Maps (structure
      pin plus validator).
- [ ] AC 3: a Places miss at the census address stores the right verdict per OQ-2, with no Places name, address
      or type anywhere in the stored row. A miss with nothing at the address stores none.
- [ ] AC 4: `classify_rollup`, `resolve_owner` and T9's gate admit website-verified candidates. T9 refuses the rest
      as `unverified`.
- [ ] AC 5: routing follows OQ-3, and a mail drop at the current address is never geocoded.
- [ ] AC 6: the classify prompt carries the verdict as context only, with no place ID or Places text.
- [ ] AC 7: `admit_owner` refuses a page that isn't tied to the business (PR #34 M3). The own site and a quote
      naming the business still pass.
- [ ] AC 8: the ticket, the READMEs and CLAUDE.md say what changed. T10's hand-over lists the new counts.
- [ ] AC 9: ruff, mypy, pyright and pytest are all green, and the alembic check shows no drift.

---

## COMPLETION CHECKLIST

- [ ] OQ-1…OQ-4 answered (or "go with the recommendations") before task 1
- [ ] All tasks completed in order, each VALIDATE run
- [ ] `/piv-validate` PASS
- [ ] The Level 4 read-only rerun is done or explicitly skipped by the founder
- [ ] `_tasks/todo.md` review appended

---

## OPEN QUESTIONS / ASSUMPTIONS

- The four OQs above are the questions. Everything else is in *Derived decisions*.
- **Assumption:** the email-domain homepage is evidence that the business exists and operates. It is not evidence
  of the census address. That is why OQ-3 routes on it only conditionally, and why Places still runs (B3).
- **Assumption:** the verdict is useful to the judgment node as a lead (B5), but does not by itself fire
  `self_employed_shell`. If the founder wants a mail drop to disqualify outright, that is a manifest rule and a
  separate ticket.
- **Assumption:** the founder doesn't need to see "verified by website" in HubSpot for this ticket (out of scope).
  If the founder does want it, it is a `RecordMeta` property, which is cheap but touches T9's mapping.

## NOTES (open canvas)

**Why not let the classify node's own homepage count as verification?** `classify_rollup` may fill `website`
(`llm_inference`) only for a candidate that is already verified. Counting it would make verification circular
for the next stage. B2's `web_lookup` requirement plus the host comparison keeps verification to the free,
deterministic fetch.

**Expected effect on the live numbers.** In run `9ca51e17`, two of five survivors (DBT, Ark) had a homepage on
their own email domain. Over the 2026-10-11 census measurement, ~23% of the pool sits on a non-shared business
domain, and only some of those answer a 2xx HTML homepage. So expect roughly 10–20% of survivors to become
website-verified, against ~6% Places-verified today. That is the order of magnitude, not a promise. The Level 4
step measures it.

**Cost.** Each newly verified survivor costs one judgment call (~$0.08) and, if the census names no officer, one
owner call (~$0.10). At a batch of 150 that is ≤ ~$5 extra a run, under the existing caps
(`max_judgment_calls_per_run`, `max_owner_calls_per_run`).

**Data flow after this ticket:**

```
census row ─▶ search_registry (email, owner?)
          ─▶ verify_business: homepage(email domain) ──▶ website (web_lookup)
                              Places ── match ──▶ business_check
                                     └─ miss at address ──▶ places_verdict (kind + place ID)
          ─▶ classify_rollup   if is_verified()  (verdict line in prompt)
          ─▶ resolve_owner     if is_verified()  (page tied to the business)
          ─▶ cluster_routes    if has_routable_address()
          ─▶ promote           if is_verified() and owner …
```

## AMENDMENTS

- **2026-10-15 — OQ-1…OQ-4 decided (founder): go with the recommendations.** OQ-1 a (a new `places_verdict`
  field), OQ-2 as recommended (`mail_drop` / `no_listing` incl. `subpremise` / `other_business` with the
  no-shared-word rule), OQ-3 a (route website-verified unless a current `mail_drop`), OQ-4 a (own host, else the
  quote carries the business's name words).
