# Feature: T9 — Promotion slice: the write-gate, HubSpot create, the `promotion` ledger

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. `HubSpotClient`, `to_property_payload`,
`ProvenancedProperty`, `render_property_value`, `ObjectType`, `HubSpotObject`, `SearchRequest`,
`SearchFilter`, `SearchFilterGroup`, `SearchOperator`, `Association`, `AssociationTarget`,
`AssociationType`, `PropertyDefinition`, `ObjectWriteRequest` live in `app.promotion.client` /
`app.promotion.schemas`; `find_existing_company`, `normalize_phone` in `app.promotion.dedupe`;
`ProvenancedValue`, `RetrievalMethod`, `is_promotable` in `app.shared.provenance`; `CandidateFields`,
`CandidateResponse`, `Principal`, `PostalAddress`, `PriorityScore`, `ScoreBand`, `RunStatus` in
`app.sourcing.schemas`; `CadenceService` (its `enrol`) in `app.cadence.service`; `AlreadyEnrolledError` in
`app.cadence.exceptions`. Every exception derives from `app.core.exceptions.LocalProspectEngineError`.

---

## ⚠ OPEN QUESTIONS — the human decides before `/piv-implement` starts

> **DECIDED 2026-10-12: all eight go with the recommendation.** OQ1 D (not disqualified and not a cited dead
> score; unscored allowed) · OQ2 A (new COMPANY enum `lpe_review_status`: pending_review / accepted / rejected,
> a one-way door, accepted) · OQ3 A (the founder sets it in HubSpot; `lpe promotion sync` reads it back and
> enrols via `CadenceService.enrol`) · OQ4 A (first word → `firstname`, the rest → `lastname`, casing kept,
> ambiguous splits flagged and counted) · OQ5 A (skip, flag `duplicate` with the matched id) · OQ6 A (closed
> `RecordMeta` exemption for `lpe_vertical`, `lpe_review_status`, `lpe_route_cluster`, `hubspot_owner_id`) ·
> OQ7 CLI only, no `routes.py` · OQ8 B (`cluster_routes` excludes the run's disqualified candidates).

These are genuinely the user's calls (product behaviour, one-way doors in the portal, or a reinterpretation
of an epic-level rule). **None is silently answered below.** The tasks are written against the
**recommended** option of each; the `Depends on` line says which tasks change if a different option is
picked. Evidence was gathered read-only (HubSpot connector, the public census API) on 2026-10-11.

### OQ1 — What does "qualified" mean at promotion time?

The ticket: *"a candidate graduates only when it qualifies **and** a human accepts it."* T7 writes two
things a promotion can read: `disqualification` rows (any rule fired → suppressed by `(vertical,
registry_id)`), and `CandidateFields.priority` (a cited Intensity × Automatable score, E9: ≥16 live, <9
dead). There is **no "judged" marker**: a candidate whose judgment call failed (`judgment_failed`) or whose
signal answers were uncited (`not_scored`) looks identical to "judged, nothing fired, no score" (T7 report,
deviation 4).

| Option | Rule | For | Against |
|---|---|---|---|
| A | not suppressed (no `disqualification` for its `(vertical, registry_id)`) | literal reading; maximum yield | promotes cited-*dead* scores the founders' own model calls dead (E9) |
| B | A + `priority` present (when the manifest declares signals) | only scored prospects reach review | judgment-node citations are unproven live; yield could be ~0 and nothing promotes |
| C | B + band ≠ dead | strictest | same yield risk as B |
| **D (recommended)** | A + **not a cited-dead score** (priority absent is allowed; `priority.band is dead` is excluded) | uses every cited fact we have, refuses nothing on an absence (absent ≠ bad); founder judges the unknowns at review | an unjudged rollup (judgment failed) can reach review — the founder catches it, and it is counted |

**Recommendation: D**, with `dead_score` and `unscored` counted separately in the report so the yield is
visible. Follow-up recorded: a T7 "judged" marker would let D also exclude never-judged candidates.
**Depends on it:** Task 7 (`eligibility.py`), Task 15 tests.

### OQ2 — Where does `pending review` live in HubSpot (Sales Hub Starter)?

Read-only check of portal 244766495, 2026-10-11: **no "pending review" stage exists anywhere.**
`lifecyclestage` has only the eight stock values (subscriber … other). `hs_lead_status` (NEW, OPEN,
IN_PROGRESS, OPEN_DEAL, UNQUALIFIED, ATTEMPTED_TO_CONTACT, CONNECTED, BAD_TIMING) **is already in use by
the founders** — 10 contacts carry it (e.g. `CONNECTED` on Beth, `NEW` on Jorge Rodriguez); 0 companies.

| Option | For | Against |
|---|---|---|
| **A (recommended)** a new enumeration property **`lpe_review_status`** on **COMPANY**: `pending_review` · `accepted` · `rejected`, in the existing `lpe` group | ours alone; filterable/bulk-editable in the company list view (D14: the list view is the review surface); cannot collide with the founders' own use; options can be added later | **property internal names are permanent** (one-way door, like T3's five); one more thing provisioned |
| B reuse `hs_lead_status` (add `PENDING_REVIEW` option) | no new property | collides with live human use on 10 contacts; the machine would read the founders' own status changes as review decisions |
| C `lifecyclestage` | stock | custom stages are not a Starter feature (unverified); lifecycle only moves forward cleanly; no "rejected" |
| D a deal pipeline | stages built in | deals are reserved for the $999 assessment (GATE-A); a deal per prospect is a shadow pipeline |

**Recommendation: A**, on COMPANY only (the contact is one click away on the company record). Also on
CONTACT if the founder wants to review from the contact list — say so; it is a second permanent name.
**Depends on it:** Tasks 2, 3, 9, 11, 12 and the Level 4 provisioning run.

### OQ3 — What *is* "a human accepts", and how does acceptance flow back?

| Option | For | Against |
|---|---|---|
| **A (recommended)** the founder sets `lpe_review_status` to `accepted` / `rejected` in HubSpot (single or bulk edit in the list view); **`lpe promotion sync`** reads the decisions back and **enrols accepted contacts** in the cadence (first task created) | review stays in HubSpot, no second surface (architecture → *Review surface*); acceptance is a HubSpot fact we read, never mirror (D4's split) | needs a trigger: one more line in the existing 07:00 launchd job (human edit of the plist), or the founder runs it |
| B `lpe promotion accept <id>` / `reject <id>` on the CLI | explicit, no polling | a second review surface — the Apollo complaint the architecture cites |
| C accept *before* create (CLI lists candidates; only accepted ones are created) | nothing unaccepted ever reaches HubSpot | contradicts the decided "land at a `pending review` stage in HubSpot" |

**Recommendation: A.** `lpe promotion sync` is its own command, **not** folded into `lpe cadence sync`
(cadence depends only on `core/` and the HubSpot client — CLAUDE.md; making it import promotion inverts
that). The human adds `lpe promotion sync` before `lpe cadence sync` in
`~/Library/LaunchAgents/com.compumatrice.lpe.cadence-sync.plist` — a manual step, listed in Level 4.
Rejected prospects are recorded `rejected` in the ledger (M6's "founder accept/reject at review" reads
it), and their HubSpot records are left as they are — nothing deletes.
**Depends on it:** Tasks 11, 12, 13.

### OQ4 — Splitting `owner.name` into `firstname` / `lastname`, and what happens when it cannot be split

**Measured 2026-10-11** on 3,000 census `company_officer_1` values (TX, broker `carship`, officer present):
every sampled name is **given-name first** (`DAVE MUSGRAVES`, `MICHAEL J RICKETTS`); a comma appears only
before a suffix (`TERRANCE TERRELL WILLIS, SR.`). After dropping a trailing generational suffix
(JR/SR/II/III/IV): **73.5% are confident** (two tokens, or given + single-letter initial + surname),
**26.5% are ambiguous** (three or more full words: a middle name — `JAMES CASEY TEAGUE` —, a compound
surname — `ALONDRA GALLEGOS LARES`, `JUAN JOSE DE LEON` —, or a title glued on — `CARL P MCLENDON CEO`),
and 1 is a single word. 22 of 3,000 are mixed case; the rest are ALL CAPS.

| Option | Ambiguous names | For | Against |
|---|---|---|---|
| **A (recommended)** first token → `firstname`; **everything after it** → `lastname` (suffix kept, nothing dropped); ambiguous ones **flagged** in the CLI output and counted `owner_name_ambiguous`; a single word → `lastname` only | first token was the given name in every sampled case, so the field a caller says aloud is right; compound surnames survive intact; nothing is guessed away | middle names land in `lastname` (`CASEY TEAGUE`) and a glued title stays (`P MCLENDON CEO`) — visible, flagged, fixable by the founder at review |
| B first token / last token, middle dropped | tidy | silently destroys compound surnames — a guess that loses data |
| C ambiguous → not promoted (stays in the workbench, counted) | never writes an uncertain split | loses ~26% of qualified prospects for a cosmetic problem |
| D ambiguous → whole name in `lastname`, no `firstname` | no split asserted | the caller has no first name to use; ~26% of contacts look broken |

**Casing (sub-question): keep the name as the source stated it (ALL CAPS).** Title-casing mangles
`MCDONALD`, `DE LA CRUZ`, `MCLENDON`; the citation is to the stated string. **Recommendation: A, casing
unchanged.** Both name fields carry the owner's citation unchanged (a deterministic split of a cited
string). Title tokens are **not** stripped (a hard-coded title list is the sort of guess E18 warns about).
**Depends on it:** Task 6 (`names.py`) and its tests.

### OQ5 — Dedupe hit: attach, skip, or flag?

The portal holds ~3,118 contacts / ~1,040 companies, many auto-created from email domains; the 14 adopted
prospects (T13) and the 3 parked refusals are among them. Census-sourced candidates have **no email** and
the only phone is the business phone, so dedupe is company-level (domain, then phone — `dedupe.py`).

| Option | For | Against |
|---|---|---|
| **A (recommended) skip and flag** — create nothing, write nothing to the existing records; the ledger row is `duplicate` with the matched company id; `promote` prints the match's HubSpot URL for a person to look at | never edits a record a founder owns (the adopted/parked ones included); simplest; freight has had zero activity, so hits should be rare — measured on the first run | a real prospect already in the portal (e.g. an auto-created domain company) is not enriched or enrolled automatically |
| B attach — reuse the existing company, create our contact under it (unless a name match exists among its contacts), stamp the existing record's provenance as `manual_hubspot_entry` (source = the record URL, `retrieved_at` = its `createdate`), never overwrite a populated property | enriches what is already there | writes onto founder-owned records; touches adopted/parked companies (whose contact may already be in the cadence, or parked for good); needs contact-level name matching |
| C update the existing records with our cited values | richest | overwrites hand-entered data — the portal editing the founders |

**Recommendation: A.** Consequence, stated plainly: under A **T9 never writes a field to an adopted
record**, so the ticket's `manual_hubspot_entry` criterion has nothing to act on yet. It is recorded in the
README as the rule for the first such write (whoever builds B), and **Task 8b (the `manual_hubspot_entry`
provenance builder) is built only if B is chosen**. In-run duplicates (two candidates of one batch sharing a
phone or domain, which HubSpot's lagging search cannot see) are always caught by the ledger.
**Depends on it:** Tasks 8 (b), 10.

### OQ6 — How do engine-metadata properties pass the write-gate?

Every value sent through `to_property_payload` must be a `ProvenancedValue` with an http(s) source. Four
properties promotion must write are **not claims about the prospect** but facts about our process:
`lpe_vertical` (which manifest), `lpe_review_status` (workflow state), `lpe_route_cluster` (our routing's
label, e.g. `A · 75238` — no source states it) and `hubspot_owner_id` (who works it). There is no honest
URL to cite for any of them.

| Option | For | Against |
|---|---|---|
| **A (recommended)** a **narrow, typed carve-out**: a frozen `RecordMeta` model (vertical, review status enum, route label, owner id) rendered by its own function and merged into the body **after** the prospect fields pass the gate — the same reasoning as `TaskCreate` being ungated | honest; the carve-out is a closed set of four typed fields, not a `dict[str, str]`, so it cannot become a side door | widens the gate's documented carve-out from "task creation" to "task creation and engine metadata" — an epic-level wording change (CLAUDE.md, `hubspot-integration.md`, `provenance.py` docstring) |
| B cite them anyway (vertical → the registry source URL, route → the Census geocode URL, review status → ?) | no rule change | a citation that does not support the value — the small lie the primitive exists to prevent; review status has nothing to cite at all |
| C don't write them | gate untouched | no review status means no review stage (contradicts OQ2/OQ3); no route on the record |

**Recommendation: A.** `lpe_priority_score` is **not** metadata — it is a judgment about the business and
goes through the gate with its `llm_inference` citation. `lpe_source_url` / `lpe_sourced_at` are also gated:
they are **derived from a designated record-defining citation** — the company's from `registry_id`, the
contact's from `owner` — which settles T3's open question ("caller or derived?") in favour of *derived*: the
meta-provenance cannot disagree with the fields it describes. (That one is recorded as decided here, since
T3 delegated it to T9's planner; flag it if you disagree.)
**Depends on it:** Tasks 2, 5, 9, and the docs task.

### OQ7 — CLI now, or a route?

**Recommendation: CLI only** — `lpe promotion promote --run <id> [--dry-run]` and
`lpe promotion sync [--dry-run]`, mirroring `lpe sourcing run` / `lpe cadence sync`. T10's `POST /runs`
calls `PromotionService` directly. The ticket's file list names `routes.py`; it is dropped (a route with no
caller is scaffolding; the CLI is the trigger launchd needs). Alternative: add `POST /promotion/sync` now
for symmetry with `POST /cadence/sync` (~40 lines + tests). **Depends on it:** Task 13.

### OQ8 — Routes that include disqualified doors, before T10

`route_assignment` (T8) holds every verified address, disqualified or not. Pipeline order already runs
`classify_rollup` (stage 4) **before** `cluster_routes` (stage 5), and the free predicates disqualify inside
stage 1 — so by stage 5 a run's disqualifications are all recorded. T8 skipped them only because T7 did not
exist yet.

| Option | For | Against |
|---|---|---|
| A promotion reads route rows only for the candidates it promotes (all qualified) | no change outside the slice | routes stay lopsided (a 12-door route with 3 survivors); the label on a company names a route full of doors nobody will knock |
| **B (recommended)** `cluster_routes` excludes the run's disqualified candidates before clustering (`app/tools/cluster_routes.py` reads `QualificationService.disqualified_candidate_ids` and passes them to `RoutingService.cluster_run(..., exclude=...)`); promotion then writes the route label of each promoted company | routes are built from survivors, balanced by construction; `routing/` still never imports `qualification/` (the stage module bridges, as `search_registry` does); ~20 lines + 3 tests | touches T8's stage — the ticket leaves "re-route" to T10. This is not a re-route: it is the eligibility rule T8 deferred. T10 keeps the question of re-running routes if qualification ever changes after stage 5 |
| C omit `lpe_route_cluster` until T10 | zero risk | the founder plans routes by hand again (E4) |

**Recommendation: B.** Unscored / unknown-score candidates are still routed (they are promotable under
OQ1-D); cited-dead ones are routed too unless the human wants B to also read OQ1's rule — say so.
**Depends on it:** Task 14.

---

## Feature Description

T9 is the slice where a sourced, qualified candidate becomes something a founder works. It has two halves:

1. **`lpe promotion promote --run <id>`** takes one finished sourcing run, selects its candidates that are
   *qualified* (OQ1) and carry every *required* cited field (`registry_id`, `legal_name`, `address`,
   `phone`, `business_check`, `owner` — decided 2026-10-11), dedupes them against the portal (domain, then
   phone) **and against itself** (the ledger), and creates a **Company + Contact** in HubSpot at
   `lpe_review_status = pending_review`, with the `lpe_*` provenance properties. Every prospect field goes
   through the write-gate; nothing is written for a candidate until both payloads have passed it (no partial
   writes). The `promotion` ledger records candidate → HubSpot ids and makes a re-run promote nothing twice.
2. **`lpe promotion sync`** reads the founder's review decisions back from HubSpot. An **accepted** company's
   contact is handed to T11 (`CadenceService.enrol`), which creates the first cadence task — and promotion's
   job ends there. A **rejected** one is recorded and left alone.

## User Story

As a founder with ~4 hours a week for new business
I want each week's qualified, owner-named prospects to appear in HubSpot waiting for my yes/no, and the
first call scheduled the moment I say yes
So that the list-building evening (E4) and the re-typing into HubSpot (E6, E14) disappear, and nothing that
reaches HubSpot carries a field nobody can cite (E18).

## Problem Statement

Sourcing, verification, qualification and routing all write to Supabase (the workbench); nothing reaches the
human surface. The HubSpot gateway (T3) exists with no promotion caller, the cadence (T11) has a `enrol` seam
nobody calls for new prospects, and the write-gate has never been exercised on a real candidate. Three
specific hazards:

- **E18 at the person field.** The owner arrives as one cited string (`company_officer_1`). Splitting it is a
  write-time transformation, and a careless split is the failure the primitive exists to prevent.
- **Duplicates.** The portal holds ~3,118 contacts / ~1,040 companies. Search lags writes by "a few moments",
  so two candidates of one batch that resolve to one company both miss dedupe (`dedupe.py` docstring). A
  create is never retried on a 5xx/timeout (T3, D20), so an interrupted create is ambiguous.
- **Failed runs.** A failed run's batch is handed back to the pool and re-batched as *new* `candidate` rows,
  so promoting from a failed run would promote the same business twice under two candidate ids.

## Solution Statement

- **A new `promotion` table** (`0010_promotion`), one row per `(vertical, registry_id)` ever promoted or
  attempted — a *state machine*, not a log: `pending_create → pending_review → enrolling → enrolled`, with
  terminal `rejected`, `duplicate`, `withdrawn`. The unique key is what makes a re-run (or a second run of
  the same business) promote nothing twice. Interrupted creates are recovered the way T11 recovers tasks:
  **intent committed first**, then the create, then the id.
- **Pure, tested-first core** — `names.py` (OQ4 split), `eligibility.py` (OQ1 + required set, naming every
  missing field via `CandidateFields.unprovenanced_fields()`), `mapping.py` (candidate → company/contact
  property mappings, with the derived `lpe_source_url`/`lpe_sourced_at`). No I/O, no clock.
- **Gateway extensions, not duplicates** — `create_contact(..., company_id=)` with an inline primary
  association; a typed `RecordMeta` carve-out (OQ6); `lpe_review_status` provisioned by the existing
  `ensure_properties` (an enumeration, so `PropertyDefinition` gains `options`).
- **`PromotionService`** orchestrates: reads `SourcingService`, `QualificationService`, `RoutingService`;
  writes through `HubSpotClient`; hands accepted contacts to `CadenceService.enrol`.
- **`cluster_routes` excludes disqualified candidates** (OQ8-B).

## Out of Scope / Non-Goals

- **No `POST /runs`, no pipeline wiring** — T10 calls `PromotionService` after the stages. Promotion is
  *not* a `PipelineStage` (the five stages are fixed; it runs on a finished run).
- **No routes** (OQ7), no frontend, no auth/RLS, no Celery, no new scheduler (launchd edit is the human's).
- **No sending, no email copy, no generated outreach** (Spike 4; E16 opener unresolved).
- **No writes to existing HubSpot records** under OQ5-A — no enrichment of duplicates, no stamping of
  adopted records.
- **No deals**, no `lifecyclestage` / `hs_lead_status` writes (founder-owned, OQ2).
- **No contact email or contact phone.** The census phone is the *business* phone; writing it on the person
  would claim it is theirs. The company carries it; the contact is associated.
- **Not touching T6's files**: `app/tools/verify_business.py`, `app/tools/resolve_owner.py`,
  `app/sourcing/sources/`, `app/sourcing/schemas.py` / `stages.py` field definitions.
- **Headcount band** (T6 may add it to `CandidateFields`): mapped after rebasing onto T6 — follow-up, shape
  not designed here.
- **Not changing** cadence behaviour: `enrol` is called exactly as documented ("T9 hands over a freshly
  promoted prospect with the defaults").
- **Not re-batching** a candidate without a principal: "stays in the pool and is counted" is read as *stays
  in the workbench (its `candidate` row), counted `no_principal`*; the pool never re-batches a completed
  run's entry (T5). Flag if the intent was a re-queue.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: High
**Primary Systems Affected**: `app/promotion/` (new service/ledger/CLI + gateway extensions), `alembic/`,
`app/tools/cluster_routes.py` + `app/routing/service.py` (OQ8-B), `app/cli.py`, `app/core/config.py`
**Dependencies**: no new libraries. HubSpot portal 244766495 (one new property, OQ2 — human-gated).

## Related Work

**Implements**: T9 — `docs/tickets/local-prospect-engine.md` § *T9* · **Epic**:
`docs/local-prospect-engine.architecture.md` + `docs/local-prospect-engine.prd.md`

**Back-references**:
- `.claude/plans/t3-hubspot-gateway.md` — the gateway, the gate, the retry table; T3's open question on
  `lpe_sourced_at` is settled here (OQ6: derived).
- `.claude/plans/t11-cadence.md` — `enrol` is the hand-off seam; the intent-before-create pattern copied.
- `.claude/plans/t13-cadence-adoption.md` — moved `manual_hubspot_entry` to T9 (see OQ5).
- `.claude/plans/t7-qualification.md` — suppression key `(vertical, registry_id)`, `priority`.
- `.claude/plans/t8-route-clustering.md` — `route_assignment`; the qualified-only hand-over (OQ8).
- `_tasks/todo.md` § *Seam PR before T6 ∥ T9* — `Principal`, the required set, the designator guard.

**Forward-references**:
- T6 (merging first) — fills `owner`, `business_check`, `website`; may add `0011` and a headcount band.
- T10 — `POST /runs` calls `PromotionService.promote_run`; Friday report reads the ledger (M6 accept/reject).

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `app/promotion/client.py` (whole; esp. 111-179 the gate, 239-395 `_request` + retry rules, 487-533
  `_write_object` / creates, 535-578 `create_task` associations, 580-668 `batch_read` / associations walk)
  — Why: every HubSpot call goes through here; extend `_write_object`, never add a second client.
- `app/promotion/schemas.py` (whole) — request models are `extra="forbid"`, response `extra="ignore"`,
  split aliases; `ObjectWriteRequest`, `Association*`, `PropertyDefinition` are what you extend.
- `app/promotion/dedupe.py` (whole) — `find_existing_company`, `normalize_phone`; the lag caveat (13-17).
- `app/promotion/properties.py` (whole) — `PROVISIONING_PLAN`, read-then-create, never PATCH.
- `app/promotion/exceptions.py` — hierarchy; `status_code` vs `status_code_received` (58-80).
- `app/promotion/README.md` — the gate, retry table, "what dedupe cannot do" (T9's ledger).
- `app/shared/provenance.py` (107-191) — `ProvenancedValue` (frozen, http(s) URL, tz-aware) and
  `is_promotable` (docstring to update under OQ6).
- `app/sourcing/schemas.py` (87-174 `PostalAddress`, `PlaceCheck`, `Principal`; 176-210 `PriorityScore`;
  289-420 `CandidateFields`, `unprovenanced_fields`) — the input. **Read-only for T9** (T6 owns edits).
- `app/sourcing/models.py` (114-157) — `candidate`, `uq_candidate_run_id_id` (composite FK target).
- `app/sourcing/service.py` (73-114) — `get_run`, `list_candidates` (ordered by registry id).
- `app/qualification/service.py` (189-199) — `disqualified_candidate_ids(run_id)`,
  `suppressed_registry_ids(vertical, ids)`.
- `app/qualification/models.py` (70-82) — the composite-FK + CHECK pattern to mirror.
- `app/routing/service.py` (75-140) and `app/routing/repository.py` (61-74) — `cluster_run`,
  `list_assignments`; `app/tools/cluster_routes.py` (whole) — the stage you extend (OQ8).
- `app/tools/search_registry.py` — how a stage module bridges `sourcing` ↔ `qualification` without either
  importing the other (the pattern for OQ8-B).
- `app/cadence/service.py` (155-266) — `CadenceService(session, hubspot=, clock=)`, `enrol(...)`: **commits
  the session it was given**, safe to retry, raises `AlreadyEnrolledError(orphan_task_id=)`.
- `app/cadence/repository.py` (20-80) — the advisory-lock pattern (not needed here — see NOTES).
- `app/cadence/sync.py` (265-290) — `find_task_by_key`: *never search for recovery, walk associations* —
  the reason company recovery has an age guard (Task 10).
- `app/cadence/cli.py` and `app/sourcing/cli.py` — `register(parser)` / `dispatch(args)`, stdout is the
  review surface, `dispose_engine()` in `finally`, exit 1 on any per-item failure.
- `app/cli.py` (52-113) — slice registration; one place errors become exit codes.
- `alembic/versions/0008_qualification.py`, `0009_candidate_run_scoped_fks.py` — hand-written migration
  style; `alembic/env.py` (22-45) — `_REGISTERED_MODELS` append point.
- `app/core/config.py` (40-75) — where `hubspot_*` settings live; `.env.example` (69-…).
- `tests/promotion/conftest.py` (whole) — `MockPortal`, `json_responder`, `make_client`, `cited`.
- `tests/cadence/conftest.py` (60-420) — `FakeHubSpot` (stateful task/association fake), `FakeClock`,
  `raced_by_another_run` — extended for promotion's fake CRM.
- `tests/sourcing/builders.py` — `a_candidate`, `sourced`, `place_checked`, `an_active_manifest`.
- `tests/conftest.py` (37-60, 110-170) — `requires_db`, `migrated_database`, `db_session`.
- `tests/core/test_logging.py` (86-104) — event-name regex `^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9]+_[a-z0-9]+$`
  (the last segment has **exactly one** underscore).
- `tests/test_structure.py` — no `Any`, no `# type: ignore`, no empty package, one migration head, no
  `if vertical ==`, Agent SDK import allow-list (promotion must not import the SDK).

### New Files to Create

- `app/promotion/models.py` — `Promotion` ORM model (the ledger)
- `app/promotion/repository.py` — ledger queries; flushes, never commits
- `app/promotion/names.py` — `split_owner_name` (pure)
- `app/promotion/eligibility.py` — required set, qualified rule, `PromotionRefusedError` raising (pure)
- `app/promotion/mapping.py` — candidate → `CompanyWrite` / `ContactWrite` (pure)
- `app/promotion/service.py` — `PromotionService.promote_run`, `.sync_reviews`
- `app/promotion/cli.py` — `lpe promotion promote | sync`
- `alembic/versions/0010_promotion.py`
- `tests/promotion/crm.py` — `FakeCrm`: companies/contacts on top of `FakeHubSpot`
- `tests/promotion/test_names.py`, `test_eligibility.py`, `test_mapping.py` (pure)
- `tests/promotion/test_ledger.py`, `test_promote.py`, `test_review_sync.py` (DB tier, `requires_db`)
- `tests/promotion/test_cli.py`
- `tests/routing/…` / `tests/tools/…` additions for OQ8-B (existing files extended)

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [HubSpot CRM — create a record with associations](https://developers.hubspot.com/docs/api-reference/crm-companies-v3/guide#create-companies)
  — inline `associations: [{to: {id}, types: [{associationCategory: "HUBSPOT_DEFINED", associationTypeId}]}]`
  on a create body. Why: contact→company in the same request (the recovery anchor).
- [HubSpot default association type ids](https://developers.hubspot.com/docs/guides/api/crm/associations/associations-v4#contact-to-object)
  — contact→company **primary = 1**, unlabeled = 279. Why: Task 4. Verify `1` read-only at Level 4.
- [HubSpot properties API — enumeration options](https://developers.hubspot.com/docs/api-reference/crm-properties-v3/guide#create-a-property)
  — `type: "enumeration"`, `fieldType: "select"`, `options: [{label, value, displayOrder}]`. Why: Task 3.
- [HubSpot CRM search — filter on custom properties](https://developers.hubspot.com/docs/api-reference/search/guide#filter-search-results)
  — EQ on a custom string property; index lag. Why: Task 10's company recovery.
- `app/promotion/README.md` → *Throttle and retry* — creates are never retried on 5xx/timeout/423.

### Patterns to Follow

**Naming:** snake_case modules, `PascalCase` classes, `StrEnum` for closed sets, constraints named
`ck_<table>_<what>`, `uq_<table>_<cols>`, `fk_<table>_<what>`.

**The repository flushes; the service commits** (every slice):
```python
# app/qualification/repository.py:46 — insert … on_conflict_do_nothing, then re-select with populate_existing
await self._session.execute(insert(Disqualification).values(...).on_conflict_do_nothing(constraint="uq_…"))
result = await self._session.execute(select(...), execution_options={"populate_existing": True})
```

**Compare-and-set state transitions** (`app/sourcing/repository.py:78-101`):
```python
update(SourcingRun).where(SourcingRun.id == run.id, SourcingRun.status == RunStatus.running.value)
    .values(...).returning(SourcingRun)
# None back → someone else moved it; theirs stands.
```

**Intent before an ambiguous create** (`app/cadence/service.py` module docstring, 7-21): commit a pending
row carrying a deterministic key *before* the create; on the next run, look for the key before creating.

**Errors:** typed, deriving `LocalProspectEngineError` with `default_code` / `status_code` ClassVars;
per-item failures in a batch are caught, logged, counted and reported — one bad candidate never sinks a
batch (`app/tools/classify_rollup.py:68-110`); the CLI exits 1 when any item failed (T11 report).

**Logging:** `logger = get_logger(__name__)`; events `promotion.<component>.<action>_<state>`, e.g.
`promotion.service.company_created`, `promotion.service.candidate_refused`,
`promotion.service.duplicate_found`, `promotion.service.review_read`, `promotion.service.prospect_enrolled`,
`promotion.ledger.row_claimed`, `promotion.gate.write_refused`. **Exactly one underscore in the last
segment** — `candidate_skipped` ✓, `owner_name_ambiguous` ✗ (use `promotion.names.split_ambiguous`).

**CHECK constraints for every value named in prose** (T2 lesson) — the ledger `status` is a CHECK, and the
shape rules per status are CHECKs too.

**Typing:** no `Any`, no `cast` beyond the `_is_cited` precedent, no suppressions. A `*args/**kwargs`
lambda is "partially unknown" under Pyright strict — write a typed `def` (T8 lesson).

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — gateway extensions and settings

Schemas (`ReviewStatus`, `PropertyOption`, `RecordMeta`, associations on `ObjectWriteRequest`), client
(`create_contact(company_id=)`, meta merge, `lpe_review_status` read helper), properties (the enumeration),
config (`hubspot_portal_id`, `hubspot_ui_domain`). Each tested against `MockPortal` before Phase 2.

### Phase 2: Pure core — names, eligibility, mapping

**Depends on:** Phase 1 (`RecordMeta`). **Independent of:** Phase 3 (can be built in parallel).
Tested exhaustively with no database and no portal — the T7/T8 lesson: the pure core first, to exact
expectations.

### Phase 3: The ledger — model, migration, repository

**Independent of:** Phase 2. Hand-written `0010_promotion`, model mirror, `env.py` registration, repository
with claim/CAS methods. DB-tier tests of every constraint.

### Phase 4: Service, CLI, routing exclusion

**Depends on:** Phases 1-3. `promote_run`, `sync_reviews`, `lpe promotion`, OQ8-B in `cluster_routes`.

### Phase 5: Docs and validation

README, tickets note, CLAUDE.md map/commands, `hubspot-integration.md`, todo; full `/piv-validate` with the
database tier; mutation checks; Level 4 (human-gated) list.

---

## STEP-BY-STEP TASKS

IMPORTANT: Execute every task in order, top to bottom. Each task is atomic and independently testable. Run
the **whole** suite once per phase, not just `tests/promotion` (T7 lesson: the logging guard only fires at
the full-suite run).

### Task 1 — UPDATE `app/core/config.py` and `.env.example`

- **IMPLEMENT**: `hubspot_portal_id: str | None = None` and `hubspot_ui_domain: str = "app-na2.hubspot.com"`
  beside the other `hubspot_*` settings, each with a one-line comment (used to print a record's URL in the
  CLI, and — under OQ5-B only — as the `manual_hubspot_entry` source). Add both to `.env.example`
  (`HUBSPOT_PORTAL_ID=244766495` commented as the live portal; T3 confirmed `app-na2.hubspot.com`).
  Add a helper in `app/promotion/mapping.py` later (Task 8), not here.
- **PATTERN**: `app/core/config.py:63-67`.
- **GOTCHA**: `extra="forbid"` on `.env` keys — add to `.env.example` in the same commit. Blank counts as
  unset (D18 precedent): the URL helper returns `None` and the CLI prints the bare id.
- **VALIDATE**: `uv run pytest tests/core -q`
- **SATISFIES**: AC 9 (reviewable output)

### Task 2 — UPDATE `app/promotion/schemas.py`

- **IMPLEMENT**:
  - `class ReviewStatus(StrEnum)`: `pending_review`, `accepted`, `rejected` (values are the HubSpot option
    values, lower snake case). Docstring: the founder moves it; we write only `pending_review`, once.
  - `class PropertyOption(BaseModel)` (request config): `label: str`, `value: str`,
    `display_order: int = Field(serialization_alias="displayOrder")`.
  - `PropertyDefinition.options: list[PropertyOption] | None = None` (excluded when `None` by the existing
    `exclude_none` dump — string properties stay byte-identical, so the T3 tests still pass).
  - `ObjectWriteRequest.associations: list[Association] | None = None` — **`None`, not `[]`**, so a plain
    company create's body is unchanged (`exclude_none`).
  - `class RecordMeta(BaseModel)` (frozen, request config) — **OQ6 carve-out**: `vertical: str`,
    `review_status: ReviewStatus | None = None`, `route_cluster: str | None = None`,
    `owner_id: str | None = None`; method `to_properties(object_type) -> dict[str, str]` rendering
    `lpe_vertical`, `lpe_review_status` (**companies only**), `lpe_route_cluster` (**companies only**),
    `hubspot_owner_id`. Docstring states exactly why these four are ungated and that the set is closed.
- **PATTERN**: `TaskCreate` (`schemas.py:257-301`) — plain values, documented carve-out.
- **IMPORTS**: `ObjectType` (same module).
- **GOTCHA**: a property name rendered for the wrong object type is silently ignored by HubSpot only if it
  exists — `lpe_route_cluster` does **not** exist on CONTACT and would 400. Test the per-type rendering.
- **VALIDATE**: `uv run pytest tests/promotion -q && uv run mypy app/promotion && uv run pyright app/promotion`
- **SATISFIES**: AC 2, AC 3

### Task 3 — UPDATE `app/promotion/properties.py`

- **IMPLEMENT**: `REVIEW_STATUS = PropertyDefinition(name="lpe_review_status", label="Review status",
  type="enumeration", field_type="select", group_name="lpe", options=[pending_review "Pending review" 0,
  accepted "Accepted" 1, rejected "Rejected" 2], description=...)`; append
  `(ObjectType.companies, REVIEW_STATUS)` to `PROVISIONING_PLAN`. Module docstring: "five" → "six"
  properties, nine placements; update the README table. `_definition` gains an optional `options` param
  or `REVIEW_STATUS` is built directly — keep it readable.
- **PATTERN**: `properties.py:40-95`.
- **GOTCHA**: drift detection compares `type` only; an option a human added in HubSpot is not drift (do not
  PATCH options — read-then-create, never PATCH). Existing tests assert "eight `property_present`" — update
  to nine, and the `test_properties.py` placement list.
- **VALIDATE**: `uv run pytest tests/promotion/test_properties.py -q`
- **SATISFIES**: AC 3

### Task 4 — UPDATE `app/promotion/client.py`

- **IMPLEMENT**:
  - `_CONTACT_TO_COMPANY_PRIMARY = 1` beside `_TASK_TO_CONTACT`.
  - `_write_object(object_type, object_id, properties, *, meta: RecordMeta | None = None, associations:
    list[Association] | None = None)`: run `to_property_payload(properties)` **first** (the gate); then
    `meta.to_properties(object_type)`; refuse with `ValueError` if a meta key collides with a prospect key
    (a programming error: both halves claim one property); merge; build `ObjectWriteRequest`.
  - `create_company(properties, *, meta=None)`, `create_contact(properties, *, meta=None, company_id=None)`
    (inline association when `company_id` is given). `update_*` keep their signatures (+ optional `meta`).
  - Log `promotion.hubspot.object_created` with `object_type`, `object_id` after a create.
- **PATTERN**: `create_task` (`client.py:535-578`) for inline associations.
- **GOTCHA**: creates stay `retry_on_server_error=False` (D20). The gate must still run with **zero
  requests** on refusal — extend `test_gate.py`'s "zero requests on refusal" to `meta=` and associations.
  Meta must never be able to carry a prospect field: the closed `RecordMeta` model is the guarantee; add a
  test that `RecordMeta` has exactly the four fields (a structure-style assertion) so widening it is a
  visible diff.
- **VALIDATE**: `uv run pytest tests/promotion/test_client.py tests/promotion/test_gate.py -q`
- **SATISFIES**: AC 1, AC 2

### Task 5 — CREATE `app/promotion/exceptions.py` additions

- **IMPLEMENT**:
  - `PromotionRefusedError(LocalProspectEngineError)` — `default_code="promotion_refused"`,
    `status_code=422`; carries `candidate_id: str`, `fields: tuple[str, ...]` (every missing required field,
    declaration order), `reason: RefusalReason`. Raised by `eligibility.check_promotable`.
  - `class RefusalReason(StrEnum)` (in `eligibility.py`, imported here or vice versa — keep one home):
    `disqualified`, `dead_score`, `no_principal`, `missing_fields`.
  - `RunNotPromotableError` (409, `default_code="run_not_promotable"`): run is `running` or `failed`.
  - `PromotionStateError` (409): a ledger transition refused (lost race), carries `promotion_id`, `status`.
- **PATTERN**: `app/promotion/exceptions.py:58-80`; never shadow `status_code` with an instance attribute.
- **VALIDATE**: `uv run mypy app/promotion && uv run pyright app/promotion`
- **SATISFIES**: AC 1

### Task 6 — CREATE `app/promotion/names.py` (OQ4)

- **IMPLEMENT**: `@dataclass(frozen=True) class NameSplit: first: str | None; last: str; ambiguous: bool`
  and `def split_owner_name(name: str) -> NameSplit` (pure):
  1. Collapse whitespace (`" ".join(name.split())`); a comma becomes a space (comma only ever precedes a
     suffix in the census sample).
  2. Tokens. A **trailing** token in `{JR, JR., SR, SR., II, III, IV}` (case-insensitive) is a suffix and
     stays attached to the last name; a suffix token in first position is a name (`JR WICKER` → `JR` /
     `WICKER`).
  3. Core = tokens minus a trailing suffix. `len(core) == 1` → `first=None, last=<whole stated name>,
     ambiguous=True`. `len(core) == 2` → confident. `len(core) == 3` and the middle token matches
     `^[A-Za-z]\.?$` → confident (initial goes to the last name? **No** — to keep nothing dropped *and* the
     surname clean, the initial goes with the **first** name: `MICHAEL J` / `RICKETTS`). Otherwise →
     ambiguous: `first = core[0]`, `last = rest` (with the suffix).
  4. **Case is never changed.** Output strings are substrings of the input, re-joined with single spaces.
  Docstring cites the 2026-10-11 measurement (73.5% / 26.5% / 1 in 3,000) and OQ4.
- **PATTERN**: pure module like `app/routing/clustering.py`.
- **GOTCHA**: `Principal` already refused entity designators, so `LLC` never reaches here. Do not add a
  title-token list (OQ4: not stripped).
- **VALIDATE**: `uv run pytest tests/promotion/test_names.py -q`
- **SATISFIES**: AC 4

### Task 7 — CREATE `app/promotion/eligibility.py` (OQ1 + the required set)

- **IMPLEMENT**:
  - `REQUIRED_FIELDS: Final = ("registry_id", "legal_name", "address", "phone", "business_check", "owner")`
    with a docstring quoting the 2026-10-11 decision (cadence is keyed on a contact; owned ≠ verified).
    A module-level assertion test (Task 15) proves each name is a real `CandidateFields` field.
  - `def check_promotable(candidate: CandidateResponse, *, suppressed: bool, manifest_has_signals: bool)
    -> None` — raises `PromotionRefusedError` with the **first applicable reason** in this order:
    `disqualified` (suppressed) → `missing_fields` / `no_principal` (missing ⊆ `REQUIRED_FIELDS ∩
    candidate.fields.unprovenanced_fields()`; reason is `no_principal` when `owner` is the **only** missing
    field, else `missing_fields`; `fields` always lists every missing one) → `dead_score`
    (`priority is not None and priority.value.band is ScoreBand.dead`). `manifest_has_signals` is kept as a
    parameter so OQ1-B/C is a one-line change; under D it is unused except in the docstring — **if OQ1 = D,
    drop the parameter** rather than leave it dead.
  - Logs `promotion.gate.write_refused` (warning) with `candidate_id`, `registry_id`, `reason`, `fields`.
- **PATTERN**: `to_property_payload`'s "collect every offender, raise once" (`client.py:147-179`).
- **GOTCHA**: `unprovenanced_fields()` lists *all* absent fields (website, dba_name, priority…) — intersect
  with `REQUIRED_FIELDS`; never refuse on an optional field.
- **VALIDATE**: `uv run pytest tests/promotion/test_eligibility.py -q`
- **SATISFIES**: AC 1, AC 5

### Task 8 — CREATE `app/promotion/mapping.py`

- **IMPLEMENT** (pure):
  - `@dataclass(frozen=True) class CompanyWrite: properties: Mapping[str, ProvenancedProperty]; meta:
    RecordMeta; key: str` and `ContactWrite: properties; meta; name: NameSplit`.
  - `def company_write(fields: CandidateFields, *, vertical: str, route_label: str | None, owner_id: str |
    None) -> CompanyWrite`: `name` ← `legal_name`; `address`/`city`/`state`/`zip` ← the four
    `PostalAddress` parts, **each a new `ProvenancedValue` carrying the address citation unchanged**;
    `phone` ← `phone`; `domain` ← host of `website` (lowercased, `www.` stripped, citation unchanged) **only
    when `website` is present**; `lpe_priority_score` ← `priority.value.score` (citation unchanged) only when
    present; `lpe_source_url` ← `registry_id.source_url` and `lpe_sourced_at` ← `registry_id.retrieved_at`,
    both cited to the registry citation (OQ6 derived). `key = registry_id.source_url` (the recovery key,
    Task 10). Optional absent fields are **omitted**, never `None` (the gate refuses `None`).
  - `def contact_write(fields: CandidateFields, *, vertical: str, owner_id: str | None) -> ContactWrite`:
    `firstname` (only when the split has one) / `lastname` from `split_owner_name(owner.value.name)`, both
    cited with the owner citation; `jobtitle` ← `owner.value.title` when present; `lpe_source_url` /
    `lpe_sourced_at` ← the owner citation. No email, no phone (Out of scope).
  - `def record_url(object_type, object_id) -> str | None` from settings (portal id + UI domain;
    `0-2` companies, `0-1` contacts), `None` when the portal id is unset.
  - Both builders call `assert`-free narrowing: they take an already-checked candidate, so they raise
    `ValueError` (programming error) if a required field is `None` — eligibility runs first.
- **(8b — only if OQ5 = B)** `def manual_entry_provenance(record: HubSpotObject, object_type) ->
  ProvenancedValue[str]`-family builder: source = `record_url(...)`, `retrieved_at` = `record.created_at`,
  `retrieval_method = RetrievalMethod.manual_hubspot_entry`. **Skip under A.**
- **PATTERN**: `ProvenancedValue(value=..., source_url=c.source_url, retrieved_at=c.retrieved_at,
  retrieval_method=c.retrieval_method)` — never `model_copy(update=...)` for a value change (it skips
  validators; T4/PR #13 M1 lesson).
- **GOTCHA**: `ProvenancedValue[int]` for the score — `PriorityScore.score` is a property; build a new
  value. `datetime` renders as epoch ms in `render_property_value` — `lpe_sourced_at` is `datetime`/`date`
  fieldType, which accepts epoch ms. D13: no Google Maps citation can reach a mapped field, because every
  mapped field's citation comes from a `CandidateFields` field that already refuses one — **except
  `business_check`, which is never mapped** (assert in a test that no mapped property cites Google Maps).
- **VALIDATE**: `uv run pytest tests/promotion/test_mapping.py -q`
- **SATISFIES**: AC 2, AC 4

### Task 9 — CREATE `app/promotion/models.py` + `alembic/versions/0010_promotion.py` + `alembic/env.py`

- **IMPLEMENT** the `promotion` table:

  | column | type | notes |
  |---|---|---|
  | `id` | uuid pk | |
  | `vertical` | `String(64)` not null | |
  | `registry_id` | `String(128)` not null | `REGISTRY_ID_MAX_LENGTH` |
  | `run_id` | uuid not null, FK `sourcing_run.id` | |
  | `candidate_id` | uuid not null | composite FK `fk_promotion_run_candidate (run_id, candidate_id) → candidate(run_id, id)` (#20 pattern) |
  | `status` | `String(24)` not null | CHECK `ck_promotion_status` in (`pending_create`, `pending_review`, `enrolling`, `enrolled`, `rejected`, `duplicate`, `withdrawn`) |
  | `company_key` | `Text` not null | the company's `lpe_source_url` — finds an interrupted create; CHECK `btrim <> ''` |
  | `dedupe_phone` | `String(10)` null | `normalize_phone` of the candidate phone — in-run/cross-run dedupe the lagging search cannot do |
  | `dedupe_domain` | `String(253)` null | |
  | `hubspot_company_id` | `String(32)` null | unique `uq_promotion_hubspot_company_id` |
  | `hubspot_contact_id` | `String(32)` null | unique `uq_promotion_hubspot_contact_id` |
  | `matched_company_id` | `String(32)` null | the existing record a `duplicate` matched (HubSpot), or null when it matched another ledger row |
  | `duplicate_of_id` | uuid null, FK `promotion.id` | the ledger row an in-run duplicate matched |
  | `name_ambiguous` | `Boolean` not null default false | OQ4 flag, for the sync/report |
  | `created_at` / `updated_at` | timestamptz, server default `now()` | |
  | `decided_at` | timestamptz null | when sync read accepted/rejected |
  | `enrolled_at` | timestamptz null | |

  Constraints: `uq_promotion_vertical_registry_id (vertical, registry_id)` — **the idempotency key across
  runs**; CHECK `ck_promotion_ids_match_status`: `status in ('pending_review','enrolling','enrolled',
  'rejected','withdrawn') ⇒ hubspot_company_id is not null and hubspot_contact_id is not null`;
  CHECK `ck_promotion_duplicate_has_match`: `status = 'duplicate' ⇔ (matched_company_id is not null or
  duplicate_of_id is not null)`; CHECK `ck_promotion_enrolled_at`: `(status = 'enrolled') = (enrolled_at is
  not null)`; indexes `ix_promotion_status`, `ix_promotion_vertical_dedupe_phone (vertical, dedupe_phone)`,
  `ix_promotion_vertical_dedupe_domain`.
  Migration `0010_promotion`, `down_revision = "0009_candidate_run_scoped_fks"`, hand-written, declared
  identically in the model (T2 lesson: an index only in the migration is reported "removed" by
  `alembic check`). Register `promotion_models.Promotion` in `alembic/env.py` `_REGISTERED_MODELS`.
- **PATTERN**: `app/qualification/models.py:38-82`, `alembic/versions/0008_qualification.py`.
- **GOTCHA — merge with T6**: T6 merges first and may add `0011_*` off `0009`. At T9's merge, per
  `conventions.md` § merge: re-point this file's `down_revision` at T6's head **and rename it to the next
  free number** (`0012_promotion`, revision id included) so filenames stay ordered — safe because no
  environment has applied it. Then `alembic check`, the single-head guard, and **`alembic upgrade head` on
  dev Supabase as part of the merge** (todo 2026-10-10: a run failed because `0007` was missing on dev).
- **VALIDATE**: `uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1 &&
  uv run alembic upgrade head` (against the throwaway DB) · `uv run pytest tests/test_structure.py -q`
- **SATISFIES**: AC 6

### Task 10 — CREATE `app/promotion/repository.py`

- **IMPLEMENT** (`PromotionRepository(session)`, flush only):
  - `claim(candidate, *, vertical, run_id, company_key, dedupe_phone, dedupe_domain, name_ambiguous) ->
    Promotion | None` — `insert … on_conflict_do_nothing(constraint="uq_promotion_vertical_registry_id")
    returning`; `None` means this business already has a ledger row (any run, any status).
  - `find_by_dedupe_key(vertical, *, phone, domain, excluding_id) -> Promotion | None` — another non-duplicate
    row with the same normalized phone or domain.
  - `get_by_registry_id(vertical, registry_id)`, `list_by_status(statuses, *, vertical=None)`,
    `list_for_run(run_id)`.
  - `transition(row_id, *, from_status, to_status, **values) -> Promotion | None` — compare-and-set
    (`WHERE id = :id AND status = :from`), `returning`, `populate_existing`; sets `updated_at = now()`.
- **PATTERN**: `app/sourcing/repository.py:63-101` (CAS), `app/qualification/repository.py:27-79`.
- **GOTCHA**: to simulate "another process moved the row" in a test, use `update(...)` with
  `execution_options(synchronize_session=False)` (PR #12 lesson) — otherwise the session's copy refreshes
  and the race test passes on broken code.
- **VALIDATE**: `uv run pytest tests/promotion/test_ledger.py -q` (DB tier)
- **SATISFIES**: AC 6, AC 7

### Task 11 — CREATE `app/promotion/service.py` — `promote_run`

- **IMPLEMENT** `PromotionService(session, *, hubspot: HubSpotFactory = get_hubspot_client, clock: Clock =
  utc_now)` and `async def promote_run(run_id: UUID, *, dry_run: bool = False) -> PromotionReport`:
  1. `run = SourcingService.get_run(run_id)`; refuse `RunNotPromotableError` unless status ∈
     {`completed`, `degraded`} (a failed run's batch is re-batched as new candidate rows).
  2. `manifest = ManifestService.get(run.manifest_id)` (signals flag, vertical).
  3. `candidates = SourcingService.list_candidates(run_id)` (registry-id order — deterministic);
     `suppressed = QualificationService.suppressed_registry_ids(run.vertical, ids)`;
     `routes = {a.candidate_id: a.cluster_label for a in RoutingService.list_assignments(run_id)}`.
  4. Resume first: every ledger row of this vertical in `pending_create` (see step 6 recovery).
  5. Per candidate, in order:
     - `check_promotable(...)` → on `PromotionRefusedError`: count by reason (`no_principal` separately,
       `missing_fields` with a per-field tally), continue. **Nothing is written.**
     - `existing = repo.get_by_registry_id(...)` → if present: count `already_promoted`, continue.
     - Build `company_write` + `contact_write`, then **render both through the gate up front**
       (`to_property_payload(cw.properties)`, same for contact) so a contact that cannot pass never leaves an
       orphan company — *no partial writes*. (A gate refusal here is a bug, since eligibility passed; it is
       caught, counted `failed`, logged.)
     - `dry_run` → run the dedupe **reads** (step below), record what would happen in the report, write
       nothing (no ledger row, no HubSpot write), continue.
     - `row = repo.claim(...)`; commit. `None` → `already_promoted` (a concurrent promote won).
     - **Ledger dedupe** (the lag the portal cannot cover): `repo.find_by_dedupe_key(...)` hit → transition
       `pending_create → duplicate` with `duplicate_of_id`; commit; count `duplicate_in_ledger`; continue.
     - **Portal dedupe**: `find_existing_company(client, domain=…, phone=…)` hit → transition to `duplicate`
       with `matched_company_id`; commit; count `duplicate_in_hubspot`; report the record URL; continue
       (OQ5-A: nothing written to it).
     - `company = client.create_company(cw.properties, meta=cw.meta)`; transition (stays `pending_create`,
       sets `hubspot_company_id`) — commit **immediately** after the create returns.
     - `contact = client.create_contact(ct.properties, meta=ct.meta, company_id=company.id)`; transition
       `pending_create → pending_review` with `hubspot_contact_id`; commit; count `created`.
     - Any `HubSpotError` on a create → leave the row in `pending_create` (ambiguous — never retried in
       process), count `failed`, record the message, continue with the next candidate.
       `HubSpotAuthError` / `HubSpotRateLimitError(policy=DAILY)` → stop the pass (every later call fails
       the same way) and report.
  6. **Recovery of `pending_create` rows** (from a crash or ambiguous create on an earlier pass):
     - no `hubspot_company_id`: only if the row is older than `RECOVERY_MIN_AGE = timedelta(minutes=10)`
       (search lags writes; `find_task_by_key` avoids search for this reason, and companies have no
       association to walk) → search companies `lpe_source_url EQ company_key`; found → adopt its id; not
       found → create as above. Younger → count `recovery_deferred`, leave it.
     - with `hubspot_company_id`, no contact: walk `companies → contacts` associations (written in the same
       request as the contact — reliable, no lag), batch-read `lpe_source_url,firstname,lastname`; a contact
       whose `lpe_source_url` equals the owner citation URL **and** whose `lastname` equals the split's last
       → adopt; else create the contact.
  7. `PromotionReport` (frozen pydantic, in `schemas.py` of the slice — add a `PromotionReport`,
     `PromotedProspect`, `ReviewSyncReport` set to `app/promotion/schemas.py`): `run_id`, `dry_run`,
     `counts: dict[str, int]` (`considered`, `created`, `already_promoted`, `refused_disqualified`,
     `refused_dead_score`, `refused_no_principal`, `refused_missing_fields`, `duplicate_in_hubspot`,
     `duplicate_in_ledger`, `recovered`, `recovery_deferred`, `failed`, `name_ambiguous`), `created:
     list[PromotedProspect]` (registry id, legal name, company/contact ids + URLs, route label, ambiguous
     flag), `duplicates`, `missing_field_tally: dict[str, int]`, `failures`.
- **PATTERN**: `CadenceService` structure (factory for the client, injected clock); per-item
  try/except/continue from `classify_rollup.py`; `SyncReport` for the report shape.
- **IMPORTS**: `from app.cadence.service import CadenceService` only in `sync_reviews` (Task 12) — and see
  the circular-import GOTCHA.
- **GOTCHA — circular import**: `app/cadence/service.py` imports `app.promotion.client`, which executes
  `app/promotion/__init__.py`. **Do not export `PromotionService` from `app/promotion/__init__.py`** (or
  import cadence at module top in a module `__init__` pulls in) — import it by module path
  (`from app.promotion.service import PromotionService`) in the CLI and in T10.
- **GOTCHA**: commit after every HubSpot create — the ledger must never be behind HubSpot by more than the
  one in-flight call. Search is paced at 5/s by the client: ~2 searches per candidate (domain, phone) ×
  ~20 promotable a week ≈ 8 s. Budgeted.
- **VALIDATE**: `uv run pytest tests/promotion/test_promote.py -q` (DB tier)
- **SATISFIES**: AC 1, AC 2, AC 5, AC 6, AC 7, AC 8

### Task 12 — UPDATE `app/promotion/service.py` — `sync_reviews`

- **IMPLEMENT** `async def sync_reviews(*, dry_run: bool = False) -> ReviewSyncReport`:
  1. Rows in `enrolling` (an earlier sync claimed and crashed) are retried first — `enrol` is retry-safe.
  2. Rows in `pending_review`: `client.batch_read(companies, ids, ["lpe_review_status"])`.
     - company absent (deleted/archived) → `pending_review → withdrawn`; count `withdrawn`.
     - `accepted` → CAS `pending_review → enrolling` (commit; a lost CAS = another sync has it, skip) →
       `CadenceService(session, hubspot=self._hubspot, clock=self._clock).enrol(contact_id,
       company_id=company_id)` (defaults: cycle 1, call, due today, owner = `HUBSPOT_DEFAULT_OWNER_ID`) →
       `enrolling → enrolled` with `enrolled_at`, `decided_at`; count `enrolled`.
       `AlreadyEnrolledError` → `enrolled` too (the contact is in the cadence — the hand-off happened), and
       report `orphan_task_id` if set. Any `HubSpotError` → stay `enrolling`, count `failed`.
     - `rejected` → `pending_review → rejected`, `decided_at`; count `rejected`. Nothing written to HubSpot.
     - `pending_review`, blank, or an unknown value → count `waiting` (unknown also logs
       `promotion.service.status_unrecognised` — a human may have added an option).
  3. `dry_run`: same reads, no CAS, no enrol, no writes; the report says what would happen.
- **PATTERN**: `CadenceService.sync` decide-then-apply split (`service.py:318-…`) so the dry run is the real
  sync minus the writes.
- **GOTCHA**: `enrol` **commits the session it is handed**. Commit the `enrolling` claim before calling it,
  and do not hold other un-committed ledger changes across the call. `enrol` raises
  `AlreadyEnrolledError` *before* any HubSpot call when a row exists — cheap.
- **GOTCHA**: the enrol is the first task — **promotion's job ends here** (ticket hand-off). Do not create
  any task yourself.
- **VALIDATE**: `uv run pytest tests/promotion/test_review_sync.py -q` (DB tier)
- **SATISFIES**: AC 9, AC 10

### Task 13 — CREATE `app/promotion/cli.py`; UPDATE `app/cli.py`

- **IMPLEMENT**:
  - `lpe promotion promote --run <uuid> [--dry-run]`: prints `run <id>  <vertical>  promote` then one line
    per created prospect (`created  <legal name>  company <url|id>  contact <url|id>  route <label>`, and
    `  ⚠ name split uncertain: first=<x> last=<y> — check at review` when ambiguous), one line per duplicate
    (`duplicate <legal name> — already in HubSpot: <url>` / `— same business as <registry id> in this
    vertical`), then the counts table and the missing-field tally. Dry run prefixes `would `. Exit 1 if any
    `failed`.
  - `lpe promotion sync [--dry-run]`: per decided company one line (`enrolled <contact> — first call due
    <date>` / `rejected …` / `withdrawn …`), then counts; exit 1 if any failure.
  - Register in `app/cli.py` (`commands.add_parser("promotion", help="promote a run's qualified prospects to
    HubSpot; read review decisions back")`, dispatch branch).
- **PATTERN**: `app/sourcing/cli.py` (asyncio.run, `dispose_engine()` in `finally`), `app/cadence/cli.py`
  (dry-run wording, exit codes). Logs go to stderr (T2 deviation 3) — do not print JSON to stdout.
- **GOTCHA**: argparse `type=UUID` gives a usage error (exit 2) on a bad id — fine and consistent.
  Avoid non-ASCII in code that ruff RUF001 flags in strings? `⚠` in an f-string is allowed (RUF001 targets
  ambiguous confusables); if ruff complains, use `!`.
- **VALIDATE**: `uv run pytest tests/promotion/test_cli.py -q` · `uv run lpe promotion --help`
- **SATISFIES**: AC 8, AC 9

### Task 14 — UPDATE `app/tools/cluster_routes.py` and `app/routing/service.py` (OQ8-B)

- **IMPLEMENT**: `RoutingService.cluster_run(run_id, *, exclude: Collection[UUID] = frozenset())` — skip
  excluded candidates before the eligibility filter and count them `routing_disqualified`. The stage reads
  `QualificationService(context.session).disqualified_candidate_ids(context.run_id)` and passes it. Routing
  still never imports qualification. Update `app/routing/README.md` *Who gets routed* (remove the T9/T10
  follow-up box; state the new rule) and the count list.
- **PATTERN**: `refresh_pool_and_select(exclude=…)` in `app/sourcing/service.py:174-219`.
- **GOTCHA**: `routing_unverified_address` must be computed over the non-excluded candidates, or the counts
  double-count. Every key every time (README promise) — `routing_disqualified` always present.
- **VALIDATE**: `uv run pytest tests/routing tests/tools -q`
- **SATISFIES**: AC 11

### Task 15 — CREATE the tests (write each failing first; see TESTING STRATEGY for the full list)

- **IMPLEMENT**: `tests/promotion/crm.py` — `FakeCrm`, composing `tests.cadence.conftest.FakeHubSpot` (its
  task/association handlers are reused for `enrol`) and adding stateful companies/contacts: search
  (`domain`, `phone`, `lpe_source_url` EQ), create (records inline associations), batch read companies
  (`lpe_review_status`), `companies → contacts` association walk, `set_review_status(company_id, value)`
  (what the founder does), `delete_company`, `lose_create_responses` (create for real, answer 500 — the
  ambiguous case **after** HubSpot recorded it; the T11 lesson), `fail_creates` (500 without creating).
  Add a public `MockPortal.prepend(*routes)` in `tests/promotion/conftest.py` so `FakeCrm`'s routes go
  **before** the generic ones.
- **GOTCHA**: `MockPortal` matches path **fragments in order**: `/crm/objects/2026-09/companies` is a
  fragment of `/crm/objects/2026-09/companies/search` and of `/companies/batch/read` — register search and
  batch read **before** create. And `/batch/read` is FakeHubSpot's generic engagement route — companies'
  batch read must precede it.
- **VALIDATE**: `uv run pytest tests/promotion -q`
- **SATISFIES**: every AC

### Task 16 — UPDATE docs

- `app/promotion/README.md`: new sections *Promotion* (flow diagram, the ledger states, the required set,
  qualified rule, recovery, dedupe incl. the ledger, OQ5 rule for the first write to a pre-existing record
  incl. `manual_hubspot_entry`), *Review* (`lpe_review_status`, `lpe promotion sync`, launchd line),
  *Names* (OQ4 with the measurement), *Engine metadata* (OQ6 carve-out); update "five properties" → six;
  module `__init__.py` docstring ("T3 ships the gateway and no caller" is no longer true).
- `app/shared/provenance.py` `is_promotable` docstring + `app/promotion/exceptions.py`
  `UnprovenancedWriteError` docstring: the carve-out now also names engine metadata (OQ6-A).
- `.claude/references/hubspot-integration.md`: the review stage, acceptance read back, engine metadata.
- `CLAUDE.md`: architecture map (`promotion/` built; `0010` in the alembic line), *Commands*
  (`lpe promotion promote --run <id> [--dry-run]`, `lpe promotion sync [--dry-run]`), the write-gate ground
  rule's carve-out wording (OQ6).
- `docs/tickets/local-prospect-engine.md` § T9: *Built …* note (decisions taken from the OQs; `routes.py`
  dropped; OQ8 applied); T10: promotion is `PromotionService.promote_run` after the stages and the Friday
  report reads the ledger; T6/T9 follow-up: map the headcount band.
- `_tasks/todo.md`: implementation section (append).
- **VALIDATE**: `uv run pytest tests/test_structure.py -q` (README links nothing; docs-only)
- **SATISFIES**: AC 12

---

## TESTING STRATEGY

pytest, async tests as elsewhere; DB tier marked `requires_db` and run against the throwaway Postgres on
**port 5434** (5433 is Langfuse's here — todo 2026-10-11). Recorded/fake portals only: **no live HubSpot
call in any test** (`MockPortal` raises on an unrouted request).

### Unit Tests (pure, no DB)

- `test_names.py` — table-driven over real shapes from the sample: `DAVE MUSGRAVES` (confident),
  `CHANCE  BRITT` (double space), `MICHAEL J RICKETTS`, `JEFFREY J. MCCAIG` (initial → first),
  `RYAN BRENT SHANKLE` / `ALONDRA GALLEGOS LARES` / `JUAN JOSE DE LEON` / `CARL P MCLENDON CEO` (ambiguous:
  first token / rest, nothing dropped), `ROBERT J MURPHY JR` (suffix stays with last), `TERRANCE TERRELL
  WILLIS, SR.` (comma), `JR WICKER` (leading JR is a name), `MADONNA` (single → last only, ambiguous),
  `Ryan Roach` (case kept), property: `first + " " + last` re-joined == the whitespace-collapsed input minus
  the comma, for every case (nothing dropped, nothing invented).
- `test_eligibility.py` — each reason; order of reasons; `no_principal` only when owner is the sole gap;
  `fields` lists every missing required field in declaration order; an absent optional field (website,
  priority) never refuses; dead band refused, middle/live/absent allowed (OQ1-D); `REQUIRED_FIELDS ⊆
  CandidateFields.model_fields`.
- `test_mapping.py` — every mapped property carries exactly the citation of its source field; address split
  into four; domain from website (`https://www.Acme.com/x` → `acme.com`), omitted without one; no `None`
  values; `lpe_source_url`/`lpe_sourced_at` equal the registry (company) / owner (contact) citation;
  `RecordMeta.to_properties` omits `lpe_route_cluster` and `lpe_review_status` on contacts; no mapped
  property cites Google Maps; contact has no `phone`/`email`.
- `tests/promotion/test_client.py` additions — contact create carries association type `1` to the company;
  company create body has **no** `associations` key; meta merged after the gate; a gate refusal with meta
  sends zero requests; a meta/prospect key collision raises before any request; `RecordMeta` has exactly
  four fields.
- `test_properties.py` — `lpe_review_status` provisioned on companies only, `enumeration`/`select`, three
  options in order; second pass nine `property_present`, zero writes.

### Integration Tests (DB tier + `FakeCrm`)

- `test_ledger.py` — every CHECK refuses its bad shape (status outside the set; `pending_review` without
  ids; `duplicate` without a match; `enrolled` without `enrolled_at`); `uq_promotion_vertical_registry_id`;
  the composite FK refuses run A + run B's candidate; `claim` twice → second `None`; CAS loses to a
  concurrent transition (`synchronize_session=False`).
- `test_promote.py`:
  - happy path: a completed run with two eligible candidates → two companies + two contacts at
    `pending_review`, contact→company association, `lpe_*` values, route label from `route_assignment`,
    ledger `pending_review`; **re-running promotes nothing** (zero creates, `already_promoted = 2`).
  - a second *run* containing the same registry id → `already_promoted` (cross-run idempotency).
  - refused: suppressed, no owner (`refused_no_principal`), no `business_check`, dead score — zero
    requests to HubSpot for each, nothing in the ledger.
  - `running` and `failed` runs → `RunNotPromotableError`.
  - dedupe: domain hit and phone hit → `duplicate` with `matched_company_id`, **no create, no write to the
    match**; two candidates sharing a phone in one batch → one created, one `duplicate_in_ledger` (the
    lagging search modeled by FakeCrm returning nothing for both).
  - ambiguous create, company: `lose_create_responses=1` → row stays `pending_create`, `failed=1`; next pass
    with the clock < 10 min → `recovery_deferred`; at ≥10 min → found by `lpe_source_url`, adopted, **one**
    company total in FakeCrm.
  - ambiguous create, contact: company exists, contact create answers 500 after creating → next pass finds
    it through the association walk, one contact total.
  - unambiguous failure (`fail_creates`) → `pending_create`, next pass creates once.
  - `HubSpotAuthError` stops the pass; later candidates untouched.
  - dry run: same counts as the real pass would produce, zero writes to FakeCrm, zero ledger rows.
  - a mixed batch where the contact payload would fail the gate (forced by a hand-built candidate) → **no
    company created** (no partial writes).
  - "nothing happens" tests are paired with "and the next real thing does" (T13/#15 lesson): after a
    re-run promotes nothing, a newly eligible candidate in a new run is still promoted.
- `test_review_sync.py`:
  - accepted → enrolled: exactly one cadence task created via the cadence fake (subject `Cadence 1/3 ·
    call`), associated to contact **and** company, `cadence_state` row live, ledger `enrolled`.
  - rejected → ledger `rejected`, zero HubSpot writes; pending → `waiting`; deleted company → `withdrawn`;
    unknown value → `waiting` + warning.
  - re-running sync after enrol does nothing (no second task) — paired with: a newly accepted one is
    enrolled.
  - crash after the `enrolling` claim (inject an exception from `enrol`'s task create) → next sync enrols
    once (enrol's task-key reuse) — assert one task.
  - `AlreadyEnrolledError` path → `enrolled`, orphan task id reported (use `raced_by_another_run`).
  - dry run: same decisions, no enrol, no ledger change.
- `test_cli.py` — `promote --dry-run` lines; created/duplicate/ambiguous lines (assert **whole lines**, not
  bare ids — PR #14 lesson); exit 1 on a failure, 0 otherwise; `sync` lines; a `RunNotPromotableError` is
  one `error:` line, no traceback.
- Routing (OQ8-B): a disqualified candidate with a verified address is on no route and counted
  `routing_disqualified`; routes balance over the survivors; a run with no disqualifications is unchanged.

### Edge Cases

- Candidate whose `Principal` was refused upstream (designator) → owner absent → `no_principal` (never a
  company-only promotion).
- Website present but unparsable host → domain omitted (logged), still promotable.
- Phone that `normalize_phone` rejects (<10 digits) → no phone dedupe key; domain-only dedupe; still
  requires the cited `phone` field (it is required — the value exists, it just isn't a match key).
- A run whose routing was degraded → unrouted candidates promote without `lpe_route_cluster`.
- Two `promote` invocations at once → `claim` lets exactly one create.
- Accepted company whose contact was deleted by a human → enrol fails (`HubSpotResponseError`) → row stays
  `enrolling`, reported every sync until a person fixes it (documented).
- Manifest with no qualifying signals → `priority` always absent → nothing refused on score.

---

## VALIDATION COMMANDS

Run with the sandbox **disabled** for `uv` and git (memory: both fail silently in the sandbox).

### Level 1: Syntax & Style

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy . && uv run pyright
```

### Level 2: Unit Tests

```bash
uv run pytest tests/promotion/test_names.py tests/promotion/test_eligibility.py tests/promotion/test_mapping.py tests/promotion/test_client.py tests/promotion/test_gate.py tests/promotion/test_properties.py -q
```

### Level 3: Integration Tests (database tier — required before any push that changes output text)

```bash
docker run --rm -d --name lpe-test-pg-t9 -p 5434:5432 -e POSTGRES_PASSWORD=test postgres:16   # if not running
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/postgres
uv run pytest -q                 # whole suite; the skip count must be 2 (the paid live eval) — any more means tests did not look
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head && DATABASE_URL=$TEST_DATABASE_URL uv run alembic check
DATABASE_URL=$TEST_DATABASE_URL uv run alembic downgrade 0009_candidate_run_scoped_fks && DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head
```
Or `/piv-validate` (full tier). **Mutation check** (record results in the report): re-introduce each of
(a) promoting from a `failed` run, (b) dropping the ledger dedupe, (c) retrying a company create after a 500,
(d) passing `None` for an absent optional field, (e) title-casing names, (f) enrolling on `rejected`,
(g) skipping the recovery age guard, (h) routing disqualified candidates — each must fail a test.

### Level 4: Manual Validation — live portal, **human-gated** (no HubSpot write outside `lpe`)

1. **One-way door:** answer OQ2. Then `uv run python -c "…ensure_properties…"` (or the existing provisioning
   entry point) creates `lpe_review_status` on COMPANY. Second run: nine `property_present`, zero writes.
2. Read-only (HubSpot connector): confirm association type `1` is "Primary" contact→company in this portal.
3. `uv run lpe promotion promote --run <a completed run with T6 data> --dry-run` — reads only; check the
   would-create lines, duplicates and the missing-field tally.
4. On approval: `promote` for real on that run (expect ~a handful). Open each company in HubSpot: provenance
   properties, `pending_review`, contact associated, names as stated.
5. In HubSpot, set one company `accepted` and one `rejected`. `uv run lpe promotion sync --dry-run`, then
   `sync`. Check: one cadence task on the accepted contact (`Ref: lpe-cadence:` in the body), owner set, due
   23:59 Dallas; the rejected one untouched. `uv run lpe cadence sync --dry-run` closes nothing.
6. Human: add `lpe promotion sync` before `lpe cadence sync` in the 07:00 launchd plist (OQ3).
7. Dev Supabase: `alembic upgrade head` (to `0010`, or the renumbered head after T6).

### Level 5: Additional Validation

`/piv-review-pr` fresh-eyes review, with the explicit question "does each fake behave like HubSpot does —
especially when a create is recorded relative to its 5xx, and search lag?" (Wave 3 review lesson).

---

## ACCEPTANCE CRITERIA

- [ ] **AC 1** Any required field (`registry_id`, `legal_name`, `address`, `phone`, `business_check`,
      `owner`) lacking provenance → `PromotionRefusedError` naming every missing field + a
      `promotion.gate.write_refused` event; **zero** HubSpot requests and **no** ledger row for that
      candidate. A candidate without an owner is counted `no_principal` and never promoted as a Company alone.
- [ ] **AC 2** Every prospect property sent to HubSpot passes `to_property_payload` with its source field's
      citation; engine metadata passes only through the closed `RecordMeta` (per OQ6); task creation is
      untouched and ungated.
- [ ] **AC 3** Company + Contact created at `lpe_review_status = pending_review` (per OQ2), with
      `lpe_source_url`, `lpe_sourced_at`, `lpe_vertical`, `lpe_priority_score` (when cited),
      `lpe_route_cluster` (when routed); contact associated to company (primary).
- [ ] **AC 4** Owner name split per OQ4; the stated casing is kept; ambiguous splits are flagged and counted;
      nothing in the name is dropped or invented.
- [ ] **AC 5** Only candidates of `completed`/`degraded` runs that are qualified per OQ1 are promoted.
- [ ] **AC 6** The `promotion` ledger maps candidate → company/contact ids; re-running a run, or a later run
      holding the same registry id, promotes nothing twice; `alembic check` clean; one head.
- [ ] **AC 7** No partial writes: a contact that cannot pass the gate means no company is created; an
      interrupted create is recovered without a duplicate.
- [ ] **AC 8** Dedupe on domain and phone before any create (per OQ5), plus ledger dedupe for same-batch
      duplicates the lagging search cannot see.
- [ ] **AC 9** `lpe promotion promote` / `sync` with `--dry-run`, readable stdout, exit 1 on any failure,
      never a traceback.
- [ ] **AC 10** Hand-off to T11: an accepted prospect is enrolled once via `CadenceService.enrol`
      (first touch requested); promotion creates no task itself; a rejected one is recorded and untouched.
- [ ] **AC 11** Routes carry only qualified (non-disqualified) candidates (per OQ8).
- [ ] **AC 12** Docs updated (README, CLAUDE.md, hubspot-integration, tickets, todo).
- [ ] ruff, ruff format, mypy, pyright clean, zero suppressions; full pytest green with the DB tier
      (skips = 2); mutation checks recorded.

---

## COMPLETION CHECKLIST

- [ ] Open Questions answered by the human (OQ1-OQ8) and any changed task re-read against its answer
- [ ] All tasks completed in order; each task's validation passed immediately
- [ ] Full suite run once per phase (not only `tests/promotion`)
- [ ] Every new test seen failing first (or covered by the mutation check — say which)
- [ ] `/piv-validate` green on the full (database) tier
- [ ] Level 4 steps listed for the human; nothing written to the live portal by the implementer
- [ ] Report written (`.claude/reports/t9-promotion-report.md`)

---

## OPEN QUESTIONS / ASSUMPTIONS

**Open questions:** OQ1-OQ8, at the top. Nothing below is one of them.

**Assumptions (flag if wrong):**

1. **T6 has merged before implementation starts** (it merges first). Until real runs carry `owner` and
   `business_check`, nothing is promotable live — expected; the tests build candidates directly.
2. **"Stays in the pool and is counted"** means stays in the workbench (its `candidate` row) and is counted
   `refused_no_principal`; no re-queue into `sourcing_pool` (a completed run's batch is never re-batched).
3. **Contact gets no phone and no email** (the census phone is the business's). The founder dials from the
   associated company.
4. **`dba_name` is not written** (HubSpot has no standard DBA property; `name` is the legal name a registry
   can cite). Add a property later if review shows it is wanted.
5. **No promotion advisory lock.** The ledger's unique key plus compare-and-set transitions keep two
   concurrent invocations from double-creating or double-enrolling; one user, weekly run. Revisit with T10.
6. **`lpe_source_url` (company) = the registry citation's URL** (per-carrier census row URL), which doubles
   as the deterministic recovery key. If a future source cited one URL for many records, recovery by key
   would be ambiguous — the CHECK only guarantees non-blank. Freight's census URL is per-USDOT.
7. **Owner of the HubSpot records** = `HUBSPOT_DEFAULT_OWNER_ID` (written through `RecordMeta`, OQ6), so
   promoted companies land in the founder's "My companies" view.
8. **Freight v-current declares qualifying signals**; if not, OQ1-D still holds (score never refuses).

## NOTES (open canvas)

**Why the ledger is a state machine and not an append-only log.** The questions every caller asks are
"has this business been promoted?", "which ones are waiting for review?", "which accepted ones still need
their first touch?". Each is one indexed read on a status column. A log would need "latest event per key"
queries for each. The states mirror exactly the HubSpot round trips that can be interrupted.

**Why recovery uses search for companies but associations for contacts.** `find_task_by_key` documents the
rule: never recover by search — its index lags writes by the window an interrupted create leaves. Contacts
are created with an inline association to the company, written in the same request, so the association
walk sees them at once. Companies have nothing to hang off, so search by the `lpe_source_url` key is the
only handle; the 10-minute age guard puts the lookup safely past the lag. A weekly human-run command makes
the delay free.

**Why dedupe also runs against the ledger.** `dedupe.py` says plainly that two candidates of one run
resolving to one company both miss and both create. The census does hold multiple USDOT numbers for one
office (a broker and its carrier arm share a phone). Storing the normalized phone/domain on the ledger row
before the create, and checking it, closes that window entirely without trusting HubSpot's index.

**Why `enrol` happens at acceptance, not at create.** The ticket's hand-off says "promotion ends when the
record exists and the first touch has been requested"; the epic's rule says a candidate graduates when it
qualifies *and* a human accepts. Enrolling at create would put a call task on prospects the founder may
reject — work the machine assigned before the human decided, and a nag about a rejected business (E15's
failure, inverted).

**Size estimate.** ~1,500-1,900 lines, ~45% tests (production ~850: service ~330, repository ~120, models +
migration ~170, mapping ~120, names ~70, eligibility ~70, CLI ~140, gateway/schema/property deltas ~90,
routing delta ~25; tests ~900 incl. `FakeCrm` ~200). Above the ticket's 900-1,200: the acceptance read-back
(OQ3), interrupted-create recovery and the fake CRM were not in the original slice.

**Sequencing risk.** The only shared files with T6 are `CLAUDE.md`, the tickets doc, `_tasks/todo.md`,
`alembic/env.py` (if T6 adds a model) and possibly `app/core/config.py`/`.env.example` — all append-style.
The migration re-number is the one mechanical step (Task 9 GOTCHA).

**Confidence: 7/10** for one pass once the OQs are answered. The risks: the fake CRM's fidelity to HubSpot
(search/association timing), the enumeration-property create body (one live provisioning call settles it),
and association type `1` (one read-only check).

## AMENDMENTS

<!-- Append-only. Newest at the bottom. Leave empty until the plan has been approved/executed. -->

### 2026-10-12: decisions after plan review

1. **All OQs resolved to the recommendation** (see the note under *OPEN QUESTIONS*). The tasks as written stand.
2. **Merge order: T6 PR A, then T9.** T6 ships in two PRs; PR A carries the stage reorder (`classify_rollup`
   before `resolve_owner`), `place_checked(address)`, and the new `email` and `headcount_band` fields (added to
   `unprovenanced_fields()`). Rebase onto it before implementing the eligibility and mapping tasks, and re-point
   `0010_promotion`'s `down_revision` at whatever head main has then (T6 plans no migration).
3. **Main moved to `0b68ab4`**: `app/core/config.py` gained `google_places_api_key`. Task 1 adds its settings
   beside it; rebase first.
4. **Human step after merge (OQ3 A):** add `uv run lpe promotion sync` to the 07:00 launchd job, before
   `lpe cadence sync`. Also creating `lpe_review_status` on the live portal is a one-way door; it happens only
   through `lpe` at the first real `promote`, never through the connector.

### 2026-10-12: part 1 built ahead of T6 PR A (the T6-independent tasks)

1. **Done in this pass**, on `feat/t9-promotion` off main `0b68ab4`, while T6 PR A is built in parallel: Task 1
   (config), Tasks 2-5 (schemas incl. `RecordMeta` and `ReviewStatus`, `lpe_review_status` provisioning, client
   meta merge + `create_contact(company_id=)`, exceptions), Task 6 (`names.py`), Task 9 (`Promotion` model,
   `0010_promotion` off `0009`, `env.py`), Task 10 (repository) and Task 14 (OQ8-B routing exclusion), each with
   its tests. Not rebased; not pushed.
2. **Waiting for the rebase onto T6 PR A** (AMENDMENTS above, item 2): Tasks 7 (`eligibility.py`), 8
   (`mapping.py`), 11-12 (`service.py`), 13 (CLI), the `FakeCrm` and the service/CLI tests of Task 15, and the
   Task 16 docs (README *Promotion*/*Review*/*Names*/*Engine metadata*, `provenance.py` and
   `UnprovenancedWriteError` docstrings, `hubspot-integration.md`, CLAUDE.md, tickets).
3. **Small divergences**: `RefusalReason` lives in `exceptions.py` (its one home, beside the error that
   carries it; `eligibility.py` will import it). `PromotionStatus` (the ledger's states) is a `StrEnum` in
   `schemas.py`, pinned to the CHECK by a test. `PromotionRepository.claim` takes no `run_id`: it uses
   `candidate.run_id`, which the composite FK requires to match anyway. `transition` takes the six settable
   columns as typed keyword arguments instead of `**values`, and never clears a column. Task 1's settings sit
   with the other `hubspot_*` settings (the plan's Task 1), not beside `google_places_api_key` — fewer shared
   lines with T6. `names.py` does not log; the service will log `promotion.names.split_ambiguous`.
