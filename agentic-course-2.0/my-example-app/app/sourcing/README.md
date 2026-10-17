# `app/sourcing/` — runs, and the candidates they find

**A run is one execution of a brief; a candidate is one business it found, every field cited.** This
slice is the sourcing workbench in Supabase: machine state, never human-edited. Nothing here reaches
HubSpot. A candidate gets there only once it qualifies *and* a human accepts it (T9).

T4 built the data model and the run lifecycle. T5 built the pipeline that drives it, the backlog
(`sourcing_pool`), the `search_registry` stage (`app/tools/search_registry.py`) and the FMCSA
adapters (`sources/`).

## The two tables

| Table | Columns (keyed, constrained, joined) | JSONB (read whole) |
|---|---|---|
| `sourcing_run` | `id` · `manifest_id` → `vertical_manifest` · `vertical` · `geography` · `status` · `status_detail` · `started_at` · `finished_at` | `icp_band` · `counts` · `cost` |
| `candidate` | `id` · `run_id` → `sourcing_run` · `registry_id` · `created_at` · `updated_at` | `fields` |

- **The brief** is `vertical` · `icp_band` · `geography` (`SourcingBrief`).
- **`manifest_id`** names the exact manifest *version* the run sourced under. Without it, a rule id
  in a later `disqualification` row (T7) does not say which rule it was.
- **`counts`** is `{stage_name: n}`. Each stage ticket adds its own key, so nobody edits this table.
- **`cost`** is a snapshot of the run's `RunCost`: calls and USD for every `BillableKind`. USD is
  stored as a string, which keeps it exact. The total is computed, not stored.

## Lifecycle

```
start_run ──▶ RUNNING ──finish_run──▶ COMPLETED | DEGRADED | FAILED
                 │
         record_candidates   (idempotent; refused once the run has finished)
```

- `start_run` resolves the vertical's **ACTIVE** manifest and refuses with
  `ActiveManifestNotFoundError` when only drafts exist. Sourcing against a proposal nobody accepted
  would hit sources whose terms of use nobody decided.
- `degraded` is for a run that finished short of its goal, for example when the 500-call Places
  breaker trips (T6). It is not a failure: the run keeps what it already verified.
- `RunOutcome` holds only the terminal statuses, so finishing a run "as running" is a type error.
- `ck_sourcing_run_finished_at_matches_status` means a running run has no finish time and a
  finished run always has one.

## Every field cited, at rest

`CandidateFields` wraps each field in `ProvenancedValue[T]`. Only `registry_id` is required; every
other field is `ProvenancedValue[T] | None`.

**"Unprovenanced" means absent.** There is no slot for a raw value without a citation. That is the
state T1's primitive made unrepresentable, and T6's rule is that a field nobody can cite is *left
absent, not inferred*. So a candidate with a missing phone is **storable but not promotable**:

```python
candidate.fields.phone  # None
is_promotable(candidate.fields.phone)  # False — HubSpot may not receive it
candidate.fields.unprovenanced_fields()
# ("phone", "email", "website", "business_check", "places_verdict", "owner", "headcount_band", "priority")
```

T9 enforces which fields promotion requires (decided 2026-10-11: `registry_id`, `legal_name`, `address`,
`phone`, `owner`; `business_check` left the set at website verification, and since 2026-10-16 verification
is not required either: an unverified candidate is refused only on a contradicting verdict). This slice
only reports which fields are missing.

`registry_id` is stored twice, as the key column and as the cited `fields.registry_id`.
`ck_candidate_registry_id_is_cited` stops the two from ever disagreeing, and refuses a row whose
registry id is missing, JSON `null` or uncited (no source URL, retrieval time or method).

**The rest of the guarantee is held by types, not by the database.** Only `registry_id` is checked in
SQL. Write candidates through `SourcingService` / `SourcingRepository` and nothing else. A `Candidate`
built directly through the ORM can store a raw, uncited value, and that one row then makes
`list_candidates` raise `ValidationError` for the whole run.

## The upsert: keyed on `(run_id, registry_id)`, merged by field ownership

`upsert_candidate(run_id, fields, *, stage)` is `INSERT … ON CONFLICT (run_id, registry_id) DO UPDATE`.
`stage` is required, and names the pipeline stage doing the writing.

**Each field has exactly one owning stage** (`stages.py`, `FIELD_OWNERS`):

| Field | Owner |
|---|---|
| `registry_id` · `legal_name` · `dba_name` · `address` · `phone` · `email` · `source_records` | `search_registry` |
| `website` · `business_check` · `places_verdict` | `verify_business` |
| `priority` · `headcount_band` | `classify_rollup` |
| `owner` | `resolve_owner` (the census adapter *fills* it from `search_registry`) |

`email` is the census's stated contact address, lowercased, kept only when it is one address.
`headcount_band` is a `HeadcountBand(low, high)` range as a page stated it (`50+` is `high=None`),
answered by `classify_rollup`'s call and cited, or absent (M8); bucketing it is T9's mapping.

`cluster_routes` owns none. **`owner` is a `Principal(name, title)`**, the name as the source stated it.
The **census adapter** maps `company_officer_1` to `owner` (`bulk_file`) when `principal_or_none` accepts
it, and `search_registry` writes it as a *fill* — it does not own the field (A2: no generic stage names an
FMCSA column). `resolve_owner` (PR B) then finds it filled and calls its judgment node only where the
census names nobody citable. A name carrying a legal-entity designator (LLC, INC, CORP…) is refused as
E18, a company in the person field. Name equality with `legal_name` is not the test: sole proprietors are
named for themselves. A refused officer counts as absent, is logged without the name, and never fails a
pull; `has_principal` (the backlog's ordering key) uses the same `principal_or_none`.

**D13 moved address and phone to the registry** (2026-10-09). Google Places is a check, never a
source: its terms forbid storing Places content, so the census copy is the one we keep and nothing
replaces it. The website comes from `verify_business`'s free fetch of the census email's domain.

## Verified is a field, not a source

**Owning a field is not verifying it.** A Places check stores only the place ID **and the census
address it checked** (`PlaceCheck(place_id, checked_address)`), in `business_check`, cited to the place
URL (`retrieved_at` = when it ran). The checked address is our registry data, not Places content.
`CandidateFields.has_verified_address()` is the rule T8 clusters on: an address **and** a check *of that
address* — the values are compared, never `retrieved_at`, which every registry refresh bumps (PR #13 L2),
so a business that moved is unverified until it is checked again. The address itself stays the census
copy. "Verified" means Places found the business from the census name and address, not that a second
address matched (D13, option A). A permanently closed listing stores no check.

**Two ways to be verified** (website-verification B1, B2). `CandidateFields.is_verified()` is Places at the
current address **or** `has_own_domain_website()`: a `website` cited `web_lookup` (the free fetch, never the
judgment node's `llm_inference` homepage) whose host, without `www.`, equals the email's own domain. The paid
stages ask `is_verified()`. T9 refuses an unverified candidate only when `has_contradicting_verdict()` (a current
`mail_drop` or `other_business`, founder 2026-10-16); a verified one is exempt. Routing asks `has_routable_address()`, which is Places-verified, or
website-verified with no current `mail_drop` verdict: a website verifies the business, not the door.

**A miss can leave our verdict** (website-verification OQ-1, OQ-2). `places_verdict` (`PlacesVerdict(kind,
place_id, checked_address)`) records what sat at the census address when Places did not match. The kinds are
`mail_drop` (a `post_office`, `business_center` or `shipping_service` at our unit: one at a suite the census
street doesn't name is a neighbour's, PR #36 M1, and so is one in a building or on a floor it doesn't name,
round-2 R1), then `no_listing` (only address types),
then `other_business` (sharing **no** distinguishing word). It keeps the place ID and our kind, never a Places
name, type or address, and it is the second field D13 lets cite Google Maps. Like the check, it is tied to the
address it checked.

**The D13 guard.** `CandidateFields` refuses any other field cited to Google Maps:
`places.googleapis.com`, `maps.googleapis.com`, `maps.google.com`, `maps.app.goo.gl`, and the `/maps`
paths of `google.com` and `goo.gl`. Case, port, userinfo, a trailing dot and a missing scheme do not
hide a host. It runs when a model is built **and again in `upsert_candidate`**, because
`model_copy(update=...)` skips validators. A deny-list of known hosts is a tripwire for our own
adapters, not a proof: a Maps host not on it would pass. Google *search* results are allowed.

- The **owning** stage overwrites its own field. A retried stage re-decides its own fact.
- **Any other** stage only *fills* a field that is empty. It never replaces a stored citation, so a
  retried registry stage cannot put a census officer back over an owner `resolve_owner` decided.
- A field the incoming write does not carry **keeps its citation**. Absent fields are dumped as absent
  keys (`CandidateFields.to_stored()`: no `None`, no empty `source_records`), never as JSON `null`
  or `[]`, and that is what makes the merge safe.
- In SQL the stored value becomes `fill || existing || owned`: last key wins, so owned fields beat what
  is stored, and what is stored beats fill-only fields.
- The registry id is stripped and capped at 128 characters by `CandidateFields`, so `" 555 "` and
  `"555"` are one row, and an over-long id fails validation before the batch starts.
- The same input twice leaves the same row with the same content.
- The same registry id in two runs gives two rows: each run keeps its own record of what it saw.
- `populate_existing=True` refreshes an object the session already holds. Without it, the second
  upsert returns the stale first object. A test proves this by failing when the option is removed.

## Enrichment (T6): `verify_business`

Stage 2 (`app/tools/verify_business.py`), for every candidate no free rule removed, in `registry_id` order:

- **Website, free, first** (`website.py`). The census email's domain, unless it is free-mail or an ISP
  mailbox (gmail, earthlink, swbell, … by name, and their subdomains: `tx.rr.com`) or shared by three or more pool businesses (process agents, franchisors; one grouped
  query, no domain in code). `https://{domain}/` is fetched once, at most three redirects; only a 2xx
  HTML page on that host or its `www.` counts, and the origin is kept, cited `web_lookup` to the page
  read. Any failure is `None` with a logged reason, never an error. Measured 2026-10-11: only 24% of DFW
  brokers carry a business-domain email (not the 67% once assumed).
- **Places, paid** (`sources/google_places.py`). One Text Search per candidate, **Pro fields only**
  (`id`, `displayName`, `formattedAddress`, `businessStatus`, `types`), the key in a header. The match
  is deterministic: the address **starts with** the census street number and holds the 5-digit ZIP, and the
  name carries the legal or DBA name's distinguishing words (3+ letters, designators and `the/and/of`
  removed): all of them for a name of one or two, at least two of a longer one. So a neighbour in the same
  building that shares only a trade word is refused **when the census name has two or more distinguishing
  words** (PR #32 H3). Known limit: a name that reduces to one word (`AJ LOGISTICS LLC` → `LOGISTICS`)
  matches any neighbour carrying it, as does a longer name sharing two trade words with one. The query
  uses the DBA unless it has no distinguishing word, then the legal name. The first live match in Google's
  order wins, ahead of a closed listing. A candidate the matcher could not match (no street number, so a PO
  box; no 5-digit ZIP; no distinguishing name word) is `places_not_checkable` and never paid for.
- **The homepage fetch is fenced** (`website.py`): an email domain whose TLD does not start with a letter is
  refused (every IP address, short forms like `127.1` included); every redirect hop is checked (`https`,
  port 443, the domain or its `www.`, compared as the raw `xn--` host) before it is requested; only status and
  headers are read; and the whole check is bounded at 10 s. It never raises.
- **The channel.** What Places saw for a stored check goes on `StageContext.places`, one dict per run
  created by the runner, for `classify_rollup` within the run (D13: use and discard). Never stored or
  logged. A miss is counted (`places_not_found`). Only a result **at the census address** records
  `places_verdict` (`classify_miss`, counted `places_mail_drop` / `places_no_listing` /
  `places_other_business`). Nothing at the address records nothing. A current verdict, like a current check,
  is not paid for twice in a run. Places still runs for a website-verified candidate (`verify_website_only`
  counts those), because only Places can say the door is a mail drop.
- **The refusal** (A3). Places runs only for a manifest that **declares** it (`base_url` the API or its
  docs page) — terms are accepted per source at `activate`. No declared source, no key, or a key refused
  mid-batch fails the run (its batch goes back to the pool) unless `--allow-degraded`.
- **The breaker.** Each call is charged first (`RunCost.charge`) against `MAX_PLACES_CALLS_PER_RUN` (500),
  at list price (`PLACES_TEXT_SEARCH_USD`, an upper bound: the monthly free tier is invisible to a run).
  Reaching it stops Places and finishes the stage `degraded`; the website step goes on. A retry pays
  nothing for a candidate already verified; a failed call or a refused write is counted and the batch
  goes on.

## Owner resolution (T6): `resolve_owner`

Stage 4 (`app/tools/resolve_owner.py`), after `classify_rollup`, so the owner node never pays for a
candidate the judgment rules removed.

- **Census first** (A2). `search_registry` already filled `owner` from `company_officer_1` wherever
  `principal_or_none` accepts it, cited `bulk_file`. Those candidates cost nothing here.
- **The node only for verified, officer-less survivors.** A candidate is sent to `owner/` only when no rule
  removed it, it `is_verified()` (Places or an own-domain homepage; an unverified candidate is not paid
  for, and reaches review only with a census owner, OQ-5 as amended 2026-10-16), and it still has no `owner`. A refused census officer (`"ACME LLC - JANE DOE"`) counts as absent, so the node runs for it.
- **The gate** (`owner/judgment.py`, A10). A name is kept only when, in order: one was offered; it is
  cited; the URL parses; the node verifiably fetched that page (`ReadTracker`, fail-closed); it is not
  Google Maps; `Principal` accepts it as a person; the quote contains its last word; **the page is tied to
  this business** (it is on the candidate's website host, or the quote carries at least two of the legal or
  DBA name's distinguishing words other than the person's own, by `shares_name`; PR #34 M3, website-verification
  OQ-4, PR #36 M2: so `RODRIGUEZ LLC` or `AJ FREIGHT LLC` ties only on its own site); and the title is not a
  registered, process or statutory agent. The registry record is never citable here. Anything else
  leaves `owner` absent: **never guessed, never repaired.** No name is ever logged.
- **`owner` belongs to `resolve_owner`.** A retry of it may replace its own answer; the registry may only
  fill an empty one (PR #31 L2). An owner already stored is kept, so a retry pays nothing.
- **The cap is degraded, not raised.** `MAX_OWNER_CALLS_PER_RUN` (500) is a breaker; reaching it counts the
  rest `owner_capped` and finishes the stage `degraded`. A failed call is counted and the batch goes on.
- **Counts:** `owner_from_registry`, `owner_kept`, `owner_resolved`, `owner_absent`, `owner_failed`,
  `owner_write_refused`, `owner_skipped_disqualified`, `owner_unverified_skipped`, `owner_capped`,
  `owner_calls`.

`classify_rollup` (stage 3) gained the matching edits in the same PR: it reads the Places channel, skips an
unverified candidate (`judgment_unverified_skipped`), ends degraded at its cap (`judgment_capped`), and keeps a
cited homepage origin as `website` (fill only, never replacing `verify_business`'s) and a cited
`headcount_band` (`judgment_website_filled`, `judgment_headcount_filled`).

## The run: a deterministic pipeline (T5)

```
reap stale runs ─▶ start_run ─▶ registered stages, in PipelineStage order ─▶ COMPLETED | DEGRADED
                                     │ any exception
                                     ▼
                         rollback ─▶ finish FAILED on a fresh session ─▶ re-raise
```

`SourcingPipeline(session_factory, stages=None).run(brief)` (`pipeline.py`). Ordinary async Python,
no agent loop (D1). The stages are whatever `app.tools.registry.registered_stages()` finds; a stage
that reports a `degraded_reason` makes the run `degraded`. Two stages reporting the same count name
fail the run. `run_id` is bound into the log context for the whole run.

- **A failure is recorded with what it spent.** The stage session is rolled back and the run is
  finished `failed` on a fresh session, with the counts and `RunCost` so far, then the error is
  re-raised.
- **A run killed outright** (no `except` runs) is failed by the *next* run of the vertical: runs still
  `running` after `SOURCING_STALE_RUN_HOURS` (6) are reaped first.
- **Finishing is a compare-and-set**: `UPDATE … WHERE status = 'running'`, with `finished_at` from the
  database clock. A second finish changes nothing and raises `SourcingRunNotRunningError`.

## The backlog: `sourcing_pool` (D12)

The registry's free filters run over the whole pool every run, and the pool is persisted. Each run
then takes a fixed batch (`SOURCING_BATCH_SIZE`, 150) **best first** into its own `candidate` rows:

1. a **current** record (`currency_date` within 730 days of the run; FMCSA's `mcs150_date`);
2. then one **naming a principal** (`has_principal`; FMCSA's `company_officer_1`);
3. then `registry_id` (lexical: `usdot:10` before `usdot:9`; stable is all D12 asks).

**Available** = seen by this run's refresh **and** (never batched **or** batched by a `failed` run).
So nothing is batched twice by a run that finished, a failed or reaped run hands its batch back with no
write of its own, and a record that stops matching the manifest drops out by not being seen. Keyed on
`(vertical, registry_id)`, not the manifest version, so a v4 does not forget what v3 already worked.
The pool is the registry's: `fields` is overwritten whole on refresh. No row lock — one user (two
concurrent runs of one vertical could overlap).

## Sources, bound by `base_url` (`sources/`)

`search_registry` binds each source the manifest declares to an adapter by its **`base_url`**, never
by vertical or by source name (`sources/base.py`). Three roles:

| Role | Exactly | FMCSA adapter | Dataset |
|---|---|---|---|
| discovery | one per manifest | `CensusSource` | Company Census File `az4n-8mr2` (SODA) |
| lookup | any | `QcMobileSource` | QCMobile `/carriers/{dot}` (needs `FMCSA_WEBKEY`) |
| join | any | `RevocationsSource` | Motus RevokeSuspend `wb4f-neki` (SODA) |

- A declared source no adapter matches is **unbound** and skipped: Places (never a discovery source,
  D13; `verify_business` binds it separately and stores none of its answer), FMCSA authority history, and the **legacy revocations file** (`rwr4-5nkg`/`sa6p-acbp`, frozen
  since Motus went live on 2026-05-14 — freight v3 declares it, so v3 needs a v4).
- A recognised source that cannot run here (no webKey, or a key refused mid-batch) **fails the run**,
  unless `--allow-degraded` is given (PR #21 review, M1). A candidate batched without its QCMobile
  record is never looked up again, so spending a batch without its evidence has to be a choice. Before
  the pull, the refusal leaves the backlog untouched; mid-batch, the failed run hands its batch back.
  With the flag, the run goes on without that source and finishes `degraded`. A *sporadic* lookup
  failure (a 5xx outlasting its retries) is `degraded` either way.
- **Pushdown** (`sources/socrata.py`): each predicate rule on a census column becomes a SoQL *keep*
  clause. Freight v3's five census rules leave 4,464 rows (live, 2026-10-10).
- Registry ids are namespaced: `usdot:7989`.
- The webKey is a query parameter and never reaches a citation, a log line or an error message.

## Contract with T7

T5 fetches; T7 decides. **Confirmed by T7 (Phase 6, 2026-10-10)**, with the owner's two decisions
recorded in item 3 and item 6.

1. **Where rule fields live**: `candidate.fields.source_records`, one cited `SourceRecord` per source:
   ```
   SourceRecord(source="fmcsa_company_census", rows=(SourceRow(values=(("carship", "B"), ("power_units", 0), …)),))
   SourceRecord(source="fmcsa_qcmobile",       rows=(SourceRow(values=(("allowedToOperate", "Y"), …)),))  # () = looked, not found
   SourceRecord(source="fmcsa_revocations",    rows=(SourceRow(…), SourceRow(…)))                     # () = no revocations
   ```
   `source` is the manifest's source name. **An absent record** — never looked, the lookup failed,
   or `rows=()` (looked, found nothing) — makes every rule on that source **unevaluable**, never a
   drop. `app/qualification/records.py` turns these into the evaluator's `RecordSet`: a one-row
   source is its record as text; no rows and many rows (the revocations) are left out.
2. **Values** are `str | int`. Census `power_units`, `truck_units`, `total_drivers`, `total_cdl` and
   `driver_inter_total` are cast to `int` at the adapter (a non-number is left out, not zeroed).
   Everything else is the source's text, unchanged; census dates stay `YYYYMMDD`. QCMobile's field
   spelling is stored as returned: `allowedToOperate`, verified live 2026-10-10 (the docs say
   `allowToOperate`; manifests up to freight v4 used that and would never have matched).
   QCMobile's `ein` is **never stored** (`_QCMOBILE_NEVER_STORED`); no rule may read it.
3. **Null semantics**, shared by the pushdown and T7's evaluator (owner decision): on a record that
   exists, `not_equals`, `not_in_set` and `not_contains` **fire** on an absent or blank field;
   `equals`, `in_set`, `contains`, `greater_than`, `less_than` and `is_true` do not.
   `tests/qualification/test_parity.py` fails if the SQL and the Python ever read it differently.
4. **Which rules were pushed down** is logged (`sourcing.socrata.where_built`, `rule_ids`) and counted
   (`pool_rules_pushed_down`). Re-evaluating one on a candidate passes by construction.
5. **Suppression key**: `(vertical, registry_id)`. Selection never takes a suppressed id
   (`pool_suppressed`); the pool row stays.
6. **Where the predicates run** (owner decision): at the end of `search_registry`, once the batch's
   evidence is recorded and before any paid step. `app/tools/search_registry.py` calls
   `QualificationService`, so `app/sourcing/` never imports `app/qualification/`. The first rule to
   fire is a cited `disqualification` (`qualification_disqualified`); `classify_rollup` skips it.
7. **A rule must read the source that holds its field.** Without `source`, a rule reads the first
   declared source (the census). The census pull reports its columns (`DiscoveryPull.known_fields`),
   and a predicate routed there on a field it never has **refuses the run before any batch** — an
   absent field would fire a negated rule on every candidate and suppress the whole backlog.
   Lookup sources (QCMobile) declare no field list. There, a **circuit breaker** catches the same
   mistake: the whole batch is evaluated before anything is recorded. If one rule fires on ≥90% of
   the candidates that have a record for its source (with at least 5 of them), the run fails with
   `BatchWideDisqualificationError` and its batch goes back to the pool. Nothing is suppressed.
   Before the pull, a malformed predicate, or a predicate on a join source (the revocations repeat
   per carrier), refuses the run (PR #18 review: M1, M2, L1).

## `lpe sourcing run`

```
uv run lpe sourcing run --vertical freight [--batch-size N] [--geography dfw] [--allow-degraded]
```

Runs every registered stage under the vertical's **ACTIVE** manifest, then prints the run id, status,
counts and cost. A source that cannot run (no `FMCSA_WEBKEY`; no declared Places source or no
`GOOGLE_PLACES_API_KEY`) is `error:` and exit 1 unless `--allow-degraded`; then `degraded` exits 0 with
a `warning:` line. No active manifest is `error:` and exit 1.
Sources today: the census, QCMobile and Motus (free), Places (paid, the check), and the homepage fetch.

## Adding a field

Add it to `CandidateFields` as `ProvenancedValue[T] | None = None`. No migration is needed, because
`fields` is JSONB. Rows written earlier load with the new field absent, which is accurate. A frozen
model is required for any structured value, since `ProvenancedValue` refuses to cite anything
editable.

## Deliberately absent

- **Years in business** and any later enrichment field need no migration (JSONB).
- **Any Places content beyond the place ID** (D13): no name, address, phone, website or types from
  Places is stored, and `tests/test_structure.py` guards the call site and the field mask.
- **`disqualification` and an index on `registry_id` alone.** These are T7's, which adds the index
  together with the cross-run query that uses it.
- **Routes.** `POST /runs` lives in `app/runs/` (T10), which calls this slice's pipeline and reads its
  runs through `SourcingService`. The one CLI here is `lpe sourcing run` (below), kept for hand runs.
- **A run-lock** (one running run per vertical). A run-lock is the stated trigger for reopening the
  Redis decision, so it is not this slice's to add quietly.

## Cross-slice reads

- **Reads** `manifests`: `ManifestService.get_active` / `get` and the `IcpBand` type. It never writes there.
- **Read by** T7 (FKs `disqualification` → `candidate`), T8 (filters on `fields.has_routable_address()`) and T9 (FKs the
  promotion ledger → `candidate` and gates every field on `is_promotable`).
