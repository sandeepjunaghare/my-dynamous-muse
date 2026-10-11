# Feature: T5 — Sourcing pipeline, the `search_registry` stage, and the FMCSA adapter

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files.

> **Read this first.** T4 (`app/sourcing/`) and T3 (`app/promotion/client.py`) are the house patterns.
> T4 gives the model/repository/service split (*the repository flushes, the service commits*), JSONB for content
> read whole, hand-written migrations whose constraints are also declared on the model, and `requires_db` on
> every database test. T3 gives the HTTP pattern: one `httpx.AsyncClient` with an **injected transport**,
> retries with jittered backoff, and pacing, tested against an `httpx.MockTransport` that **raises on any route
> it does not know**. The seam PR (`app/tools/registry.py`) gives the `Stage` contract: a stage registers by
> **being** `app/tools/<PipelineStage value>.py` and exporting `STAGE`.
>
> **Four decisions taken with the human on 2026-10-10, before this plan** (do not reopen):
> 1. **The backlog is a new table**, `sourcing_pool` (migration `0006`), keyed `(vertical, registry_id)`. The batch
>    becomes ordinary `candidate` rows in the run.
> 2. **T5 fetches, T7 decides.** QCMobile and the revocations join are stored on the candidate as cited, generic
>    `source_records`. T5 drops nothing after the pool. T7's evaluator reads rule fields from there.
> 3. **Pushdown in T5.** Manifest predicate rules on census columns become a SoQL `$where`. Rules on other fields
>    (`allowToOperate`) are left for T7.
> 4. **`lpe sourcing run`** is added, so a person can run T5 before T10's `POST /runs` exists.

## Feature Description

T5 turns a brief into cited candidates. A **deterministic runner** in `app/sourcing/pipeline.py` opens a
`sourcing_run` under the vertical's ACTIVE manifest, executes every registered stage in `PipelineStage`
order, and closes the run as `completed`, `degraded` or `failed`, with its counts and cost. It is ordinary
async Python with no agent loop (D1). Today exactly one stage exists: `search_registry`, stage 1 of 5.

`search_registry` is generic. It binds the manifest's declared sources to adapters **by the source's
`base_url`**, never by vertical. Then it does four things:

1. **Refresh the pool (free, D12).** It asks the discovery source for every record that survives the manifest's
   pushdown-able predicate rules. For freight v3 that is the FMCSA Company Census File via SODA: 4,449 rows. It
   upserts them into `sourcing_pool`.
2. **Select the batch.** It takes a fixed batch, default 150, **best first**. The order is: a current MCS-150
   (within 2 years of the run), then a named `company_officer_1`, then `registry_id`. Entries already batched by
   a non-failed run are never selected again.
3. **Free checks on the batch (D8).** It does a QCMobile lookup per candidate (`allowToOperate` and friends) and
   one revocations join per batch, both in code. Each result is stored as a cited `SourceRecord`. Found-nothing
   is stored as zero rows, which is different from never-looked.
4. **Record.** It upserts the batch as `candidate` rows in the run (`stage=search_registry`), with every field
   cited.

The FMCSA adapters live in `app/sourcing/sources/fmcsa.py`, on a small generic SODA client
(`sources/socrata.py`) that also owns the rule→SoQL pushdown. A stub adapter bound to the fire manifest's
registry URL proves portability with no code change.

T5 also closes the run-lifecycle items deferred to it by T4's review:
- the crash path: a failed run is finished on a fresh session, with its cost;
- the finish time comes from the database clock;
- the finish is conditional (`WHERE status = 'running'`);
- stale running runs are reaped;
- `run_id` is bound into the log context.

It also closes deferred finding **#2**, URL-shape validation on `source_url`.

## User Story

As **the founder running the weekly list (and T10, which will automate it)**
I want **one command to turn a brief into a batch of cited candidates drawn best-first from a persisted backlog**
So that **each week's paid steps (T6/T7) see only a fixed, well-chosen batch, the backlog works down without
repeats, and every field on every candidate traces to the FMCSA record it came from**.

## Problem Statement

T4 built the tables and the seam PR built the stage contract, but nothing produces a candidate. Three
specific gaps:
- **No discovery.** QCMobile is a per-USDOT lookup. It cannot search by geography, which is exactly the E10
  mistake: a 74-row file of BOC-3 process agents instead of brokers. Discovery has to start from the census file.
- **No backlog.** D12 says the free filters persist the whole pool once and each run takes a batch, never
  repeating. `candidate` is keyed per run `(run_id, registry_id)`, so it cannot remember "already batched"
  across runs.
- **No run driver.** Nothing sequences stages, owns the session model, or owns the crash path. A run that dies
  today leaves a `running` row forever, with `cost = {}`.

## Solution Statement

- **`sourcing_pool` table.** One row per `(vertical, registry_id)`. It holds:
  - the census-derived `CandidateFields` (JSONB);
  - real columns for what the batch orders and filters on: `currency_date` (FMCSA's `mcs150_date`) and
    `has_principal` (a non-blank `company_officer_1`);
  - lifecycle columns: `first_seen_run_id`, `last_seen_run_id`, `batched_run_id`.

  "Available" means **seen in this run's refresh** AND (`batched_run_id IS NULL` OR the batching run `failed`).
  So a failed or reaped run gives its batch back with no extra write, a record that stops matching the manifest
  silently drops out, and nothing is batched twice by a run that finished.
- **Source binding by `base_url`.** `sources/base.py` declares three adapter roles:
  - `DiscoverySource`: enumerate the pool, with pushdown;
  - `LookupSource`: one record → one `SourceRecord`;
  - `JoinSource`: many records → `SourceRecord`s.

  `bind_sources(manifest, adapters)` matches each declared `ManifestSource` against an adapter table. Exactly one
  discovery source is required. Unbound sources are logged and skipped: Places is never a discovery source (D13),
  and the frozen legacy revocations file is unsupported. The table is injectable, which is how the fire stub is
  bound in tests.
- **Generic `source_records` on `CandidateFields`.** A new field holds
  `tuple[ProvenancedValue[SourceRecord], ...]`, where `SourceRecord = (source name, rows of (key, str|int))`. The
  census row, the QCMobile carrier and the revocation rows each get their own citation. This is the data contract
  T7's evaluator reads (decision 2). Census integer columns are **cast at the adapter**; anything non-numeric is
  left absent and logged, never guessed.
- **Pushdown** (`sources/socrata.py`). Each predicate rule on a column the census adapter declares becomes a
  **keep** clause, the negation of the rule. **Null semantics** (see *Contract with T7*):
  - an *exclude-by-absence* operator (`not_equals`, `not_in_set`, `not_contains`) fires on an absent field;
  - every other operator does not fire on an absent field.

  Unsupported operator/value combinations are not pushed down. They are logged and left for T7.
- **Runner** (`pipeline.py`). It takes a session factory, not a session:
  1. reap stale runs;
  2. `start_run`;
  3. bind `run_id` into structlog's contextvars;
  4. run each stage;
  5. on success, finish the run as `completed`, or as `degraded` if any stage reported a reason;
  6. on any exception, roll the stage session back, then finish the run as `failed` on a **fresh** session with
     the cost so far, and re-raise.
- **Lifecycle hardening** (`repository.py`). `mark_finished` becomes one
  `UPDATE … SET finished_at = now() … WHERE id = :id AND status = 'running' RETURNING *`. Zero rows means
  `SourcingRunNotRunningError`. `reap_stale_runs(vertical, older_than)` marks abandoned runs `failed`.
- **Registry ids are namespaced**: `usdot:7989`. Decided now, before the first real write (T4 review decision 4).
- **`lpe sourcing run --vertical <slug> [--batch-size N] [--geography dfw]`** prints the run id, status, counts
  and cost.

## Out of Scope / Non-Goals

- **Not included: evaluating any disqualifier rule on candidates**, including `allowToOperate` and
  `authority_revoked`. That is T7's evaluator and `disqualification` table. T5 only fetches and cites.
- **Not included: re-source suppression of disqualified candidates.** That is T7's, and it reads the pool by
  `(vertical, registry_id)`. T5's pool already stops anything being *batched* twice.
- **Not included: website from the census email domain, owner from `company_officer_1`, headcount.** These are
  T6 (`verify_business`, `resolve_owner`). The raw values stay in the census `SourceRecord` for T6 to read.
- **Not included: QCMobile `/authority`, `/docket-numbers`, BOC-3.** One call per candidate (`/carriers/{dot}`)
  carries `allowToOperate`, the only QCMobile field any manifest rule names. The MC number is already in the
  census `docket1prefix`/`docket1`. Add calls when a rule needs them (see NOTES).
- **Not included: `POST /runs`, the trigger route, the Friday report.** These are T10.
- **Not included: Places, the Census Geocoder, any paid call.** T5 records no `RunCost` spend. The `RunCost`
  passes through for T6.
- **Not included: reconciling the brief's ICP band or geography with the manifest.** The geography is recorded
  only; the manifest's rules define the area (`phy_cnty`). Multi-metro is a non-goal.
- **Not included: the deferred finding #6** (`RunCost.record`/`check_cap` coupling). It is due before T6.
- **Not changing: `candidate`'s key, the upsert merge, the `Stage` protocol's `run()` signature,
  `app/manifests/`.** `StageResult` gains one optional field; nothing else in `app/tools/registry.py` moves.
- **Not changing any manifest.** Freight v3 needs a v4 before its first live run (see Open Questions). That is a
  human action, not code.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: High (three external datasets, a new table, the run's crash path, a cross-ticket data contract)
**Primary Systems Affected**: `app/sourcing/` (pipeline, pool, sources, cli), `app/tools/search_registry.py`, `app/shared/provenance.py`, `app/core/config.py`, `app/cli.py`, `alembic/versions/0006_*`
**Dependencies**: `httpx` (already a dependency). No new packages. External: SODA on `data.transportation.gov` (no key; optional app token), QCMobile (free webKey, **not yet obtained**).

## Related Work

**Implements**: T5 in `docs/tickets/local-prospect-engine.md` · **Epic**: `docs/local-prospect-engine.architecture.md` (+ PRD)

**Back-references**:
- `.claude/plans/t4-sourcing-model.md`: `sourcing_run`/`candidate`, the ownership merge, and the crash-path,
  DB-clock, conditional-finish and namespacing items that review deferred here (`.claude/code-reviews/pr-7-review.md`, decisions 3 and 4).
- `.claude/code-reviews/pr-17-review.md`, the seam PR: the `Stage` contract this plan builds the runner over.
- `.claude/plans/t3-hubspot-gateway.md`: the httpx client, MockTransport and retry pattern.
- `_tasks/todo.md` → *Funnel re-plan* (D12, the 4,449 measurement) and *PR #2 review findings — deferred* (#2).

**Forward-references**:
- T7 (parallel): reads `CandidateFields.source_records` and the null semantics below, and keys suppression on
  `sourcing_pool`/`registry_id`. **T7's plan must read *Contract with T7*.**
- T8 (parallel): no contact. It reads `CandidateFields.address`, which is unchanged.
- T6: fills `website` and `owner` from the census `SourceRecord` and runs on the batch's candidates.
- T10: calls `SourcingPipeline.run`, and wraps `ActiveManifestNotFoundError` as a 404/409.

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `CLAUDE.md`: ground rules. Especially *Vertical is data*, *free before paid*, *Types: zero suppressions*, and the logging event names.
- `app/tools/registry.py` (whole file, 95 lines): `Stage`, `StageContext`, `StageResult`, `registered_stages()`. The runner iterates `registered_stages()`.
- `tests/tools/test_registry.py`: how a stage module is tested via a fake package. Mirror it for `search_registry.py`.
- `app/sourcing/service.py` (whole file): `start_run` / `record_candidates(stage=…)` / `finish_run`. The runner calls these.
- `app/sourcing/repository.py` (lines 60-80 `mark_finished`; 82-150 `upsert_candidate`): rewrite `mark_finished`; mirror the `insert(...).on_conflict_do_update(...)` + `populate_existing` shape for the pool.
- `app/sourcing/models.py` (whole file): the model pattern, including the cited-registry-id CHECK. Copy that expression for the pool.
- `app/sourcing/schemas.py` (lines 141-235 `CandidateFields`): add `source_records`, and extend `_no_places_content` to tuple fields.
- `app/sourcing/stages.py`: `FIELD_OWNERS` needs a row for the new field. `tests/sourcing/test_stages.py:14` fails until it has one.
- `app/sourcing/exceptions.py`: the exception pattern (`default_code`, `status_code`, keyword context).
- `app/shared/provenance.py` (lines 125-135 `_reject_blank_source`): add the URL-shape check here.
- `app/manifests/schemas.py` (lines 56-130): `DisqualifierRule`, `RuleKind`, `RuleOperator`, `ManifestSource`. The pushdown input.
- `app/manifests/service.py:41,50`: `get(manifest_id)` and `get_active(vertical)`.
- `app/promotion/client.py` (lines 1-120, plus its retry/backoff section): the HTTP client pattern. Copy its constants style, its spelled-out status codes (the Pyright note at line ~100), its jitter and its log events.
- `tests/promotion/conftest.py` (lines 1-120): `MockPortal`, `json_responder`, `sequence_responder`, `load_fixture`. Mirror these as `MockSoda`/`MockQcMobile`.
- `app/core/config.py`: add settings next to the existing cost block.
- `app/core/database.py:57`: `get_sessionmaker()` is the runner's default session factory.
- `app/core/logging.py:64`: `merge_contextvars` is already in the processor chain, so `structlog.contextvars.bind_contextvars(run_id=…)` works.
- `app/cadence/cli.py` (lines 40-120): the CLI pattern. `register`/`dispatch`, `asyncio.run`, `get_sessionmaker()()`, `dispose_engine()` in `finally`.
- `app/cli.py:51-70`: where the `sourcing` group registers and dispatches.
- `tests/conftest.py:139-165`: the `db_session` savepoint recipe. The pipeline tests need a **factory** variant (below).
- `tests/sourcing/builders.py`: `an_active_manifest`, `sourced`, `a_brief`. Extend them, don't fork them.
- `tests/test_structure.py:219`: the no-vertical-branch guard. It must stay green, so binding is by URL, never by vertical.
- `alembic/versions/0004_sourcing.py` and `0005_cadence.py`: the migration style. `revision = "0005_cadence"` is the parent.
- `.claude/code-reviews/pr-7-review.md` lines 150-235: the deferred items this plan closes.

### New Files to Create

- `app/sourcing/pipeline.py`: `SourcingPipeline`, the deterministic runner.
- `app/sourcing/pool.py`: `PoolRepository`, which upserts, selects the batch and marks it batched. A separate file from `repository.py`, so T7, which may edit `repository.py`, does not conflict.
- `app/sourcing/sources/__init__.py`: empty.
- `app/sourcing/sources/base.py`: `PoolRecord`, the `DiscoverySource`/`LookupSource`/`JoinSource` Protocols, `AdapterFactory`, `BoundSources` and `bind_sources()`.
- `app/sourcing/sources/socrata.py`: `SodaClient` (paging, retry, app token) and `pushdown_where(rules, columns)`.
- `app/sourcing/sources/fmcsa.py`: `CensusSource`, `QcMobileSource`, `RevocationsSource` and `FMCSA_ADAPTERS`.
- `app/sourcing/cli.py`: `lpe sourcing run`.
- `app/tools/search_registry.py`: `SearchRegistryStage`, and `STAGE = SearchRegistryStage()`.
- `alembic/versions/0006_sourcing_pool.py`: the `sourcing_pool` table.
- `tests/sourcing/conftest.py`: `session_factory` (savepoint-bound factory), `MockSoda`/`MockQcMobile` transports, fixture loader.
- `tests/sourcing/fixtures/`:
  - `census_page.json`: 6–8 synthetic but schema-accurate rows. Include one with no `company_officer_1`, one with an old `mcs150_date`, one with `power_units` = `"N/A"`, and one missing a `phy_street`;
  - `census_page_fire_stub.json`, if needed;
  - `qcmobile_carrier.json`;
  - `qcmobile_not_found.json`;
  - `qcmobile_missing_webkey.json`;
  - `revocations.json`.
- `tests/sourcing/sources/__init__.py`, `test_socrata.py`, `test_fmcsa.py`, `test_base.py`.
- `tests/sourcing/test_pool.py`, `test_pipeline.py`, `test_cli.py`.
- `tests/tools/test_search_registry.py`.

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [SODA query basics: `$where`, `$order`, `$limit`/`$offset`](https://dev.socrata.com/docs/queries/). Page with a stable `$order=dot_number`, and stop on a short page.
- [SoQL functions: `contains`, `IS NULL`, `IN`, `::number`](https://dev.socrata.com/docs/functions/). **Verify `contains()` live** (Level 4); if it 400s, fall back to `like '%B%'`.
- [SODA app tokens](https://dev.socrata.com/docs/app-tokens.html): the `X-App-Token` header. Optional; without it you are throttled per IP.
- [Census dataset metadata](https://data.transportation.gov/api/views/az4n-8mr2.json): the column list. Every column is `text`. `dot_number` is `number`, but JSON returns it as a string. Dates are `YYYYMMDD`. **Empty fields are omitted from the JSON entirely.**
- [QCMobile API](https://mobile.fmcsa.dot.gov/QCDevsite/docs/qcApi) and [elements](https://mobile.fmcsa.dot.gov/QCDevsite/docs/apiElements): `GET /qc/services/carriers/{dot}?webKey=…`. The field is spelled `allowToOperate` in the docs; **the live spelling is unverified**. A missing key answers HTTP 404 with body `{"content":"Must provide Webkey"}`. Unknown-DOT behaviour (404 vs `content: null`) is unverified, so handle both.
- [Motus RevokeSuspend, all with history](https://data.transportation.gov/api/views/wb4f-neki.json): columns `usdot_number`, `docket_number` (`MC-424836`), `op_auth_type`, `order1_type_desc`, `order1_serve_date` / `order1_effective_date` (`YYYYMMDD`). Rows can repeat. **Join on `usdot_number`**, never on the docket (its format differs from the census).
- [FMCSA "What's coming" (Motus)](https://www.fmcsa.dot.gov/registration/whats-coming): the legacy system shut on 2026-05-14. The legacy revocation dataset (`sa6p-acbp`, blob `rwr4-5nkg`) **is frozen**, and freight v3 declares the blob.

### Patterns to Follow

**Logging**: `domain.component.action_state`, for example `sourcing.pipeline.run_started`,
`sourcing.pool.refresh_completed`, `sourcing.sources.source_unbound`, `sourcing.qcmobile.lookup_failed`,
`sourcing.socrata.page_fetched`, `sourcing.socrata.pushdown_skipped`. **Never log the webKey or the app
token**, and assert that in a test, as T3 does for its token.

**Errors**: slice exceptions on `SourcingError` with `default_code`/`status_code` and keyword context, as
`app/sourcing/exceptions.py` does. New ones:
- `NoRegistrySourceError` (409): the manifest binds no discovery source, or binds more than one;
- `SourceRequestError` (502): a source failed after retries;
- `SourceAuthError` (502): QCMobile rejected or lacks its webKey.

**Status codes as ints**, never `httpx.codes` members (`app/promotion/client.py` ~line 100 explains the Pyright reason).

**Session model**: the repository flushes; the service commits. The pipeline owns **which session** gets used,
and never calls `commit` itself except through the service.

**Immutability**: everything inside a `ProvenancedValue` is a tuple or a frozen model (`provenance.py:_assert_immutable`).

**Determinism**: no `set` iteration into output, and no `dict` order from network JSON reaching storage
unsorted. `SourceRow` pairs are sorted by key. The batch order is a total order (`registry_id` last).

---

## Contract with T7 (write this into `app/sourcing/README.md` and the T5 ticket notes)

1. **Where rule fields live.** `candidate.fields.source_records` is a tuple of cited `SourceRecord`s:
   ```
   SourceRecord(source="fmcsa_company_census", rows=(SourceRow(values=(("carship","B"),("power_units",0),…)),))
   SourceRecord(source="fmcsa_qcmobile",       rows=(SourceRow(values=(("allowToOperate","Y"),…)),))   # () = looked, not found
   SourceRecord(source="fmcsa_revocations",    rows=(SourceRow(...), SourceRow(...)))                  # () = no revocations
   ```
   `source` is the **manifest's source name**. A record that is absent means never looked, or the lookup failed.
   An evaluator that cannot find its field must treat the rule as **unevaluable**, not as passed.
2. **Values** are `str | int`. The census integer columns (`power_units`, `truck_units`, `total_drivers`,
   `total_cdl`, `driver_inter_total`) are cast at the adapter. Everything else is the source's text, unchanged.
   Census dates stay `YYYYMMDD` strings.
3. **Null semantics T5 used for pushdown**: `not_equals`, `not_in_set` and `not_contains` **fire** on an absent
   field; `equals`, `in_set`, `contains`, `greater_than`, `less_than` and `is_true` **do not**. If T7 picks
   differently, the pool and the evaluator disagree. Raise it on the T7 plan, not in code.
4. **Which rules were pushed down** is logged per run (`sourcing.socrata.where_built`, with `rule_ids`) and
   counted as `pool_rules_pushed_down`. Re-evaluating a pushed-down rule on a candidate is harmless, because it
   passes by construction.
5. **Suppression key**: `(vertical, registry_id)`, with ids namespaced (`usdot:<n>`).

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (no database, no network)
Shared and schema changes: the `source_url` URL shape, `SourceRow`/`SourceRecord`/`source_records`, the
`FIELD_OWNERS` row, `StageResult.degraded_reason`, settings and exceptions.

### Phase 2: Sources (network via MockTransport only)
**Depends on:** Phase 1 (`SourceRecord`, exceptions, settings)
`sources/base.py` → `sources/socrata.py` (client + pushdown) → `sources/fmcsa.py` (three adapters). Every test
here is offline.

### Phase 3: Persistence
**Depends on:** Phase 1 · **Independent of:** Phase 2
The `SourcingPool` model, migration `0006`, `PoolRepository`, the `mark_finished` rewrite and `reap_stale_runs`.

### Phase 4: Stage, runner, CLI
**Depends on:** Phases 2 and 3
`app/tools/search_registry.py` → `app/sourcing/pipeline.py` → `app/sourcing/cli.py` → `app/cli.py`.

### Phase 5: Tests, docs, validation
Acceptance tests (determinism, disjoint batches, portability, crash path), the README, the tickets note, the
CLAUDE.md architecture map, and `/piv-validate`.

---

## STEP-BY-STEP TASKS

IMPORTANT: Execute every task in order, top to bottom. Each task is atomic and independently testable.
Start a throwaway Postgres on **port 5434** (5433 is taken; T7/T8 use 5435/5436):
`docker run --rm -d -p 5434:5432 -e POSTGRES_PASSWORD=test postgres:16` ·
`export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/postgres`.
`uv` needs the sandbox disabled (memory: *uv commands need the sandbox disabled*).

### UPDATE `app/shared/provenance.py`: deferred finding #2, URL shape
- **IMPLEMENT**: In `_reject_blank_source`, after the strip, `urlsplit` the value and require scheme `http` or
  `https` and a non-empty `hostname`. The message names the bad value. Docstring: why (`"n/a"` passed the gate,
  and T5 is the first ticket writing real sourced URLs).
- **GOTCHA**: `provenance.py` must not import `app.core` (`tests/test_structure.py:203`). Use `urllib.parse` only.
  Check every existing `source_url` in the test suite and builders is http(s). `grep -rn "source_url" tests app`.
  Dev Supabase holds manifests v1–v3, whose citations are all https (checked 2026-10-10 via `lpe manifest show`).
- **VALIDATE**: `uv run pytest tests/shared tests/manifests tests/sourcing -q`
- **SATISFIES**: AC 9

### UPDATE `app/sourcing/schemas.py`: `SourceRow`, `SourceRecord`, `source_records`
- **IMPLEMENT**:
  - `type SourceValue = str | int`.
  - `class SourceRow(BaseModel)`, frozen. `values: tuple[tuple[str, SourceValue], ...]`. A validator **sorts by
    key** and rejects duplicate or blank keys. `def get(self, key: str) -> SourceValue | None`.
  - `class SourceRecord(BaseModel)`, frozen. `source: str` (`SLUG_PATTERN`, max 64), `rows: tuple[SourceRow, ...]`.
    The docstring states that `()` means looked-and-found-nothing.
  - On `CandidateFields`: `source_records: tuple[ProvenancedValue[SourceRecord], ...] = ()`, plus a validator
    rejecting two records with the same `source`. One record per source, which is what makes T7's lookup
    unambiguous.
  - Extend `_no_places_content`: for a tuple field, check each element's `source_url`.
  - `unprovenanced_fields` is unchanged, because a tuple is never `None`. Say so in its docstring.
- **PATTERN**: `PostalAddress`/`PlaceCheck` (frozen leaf models), `schemas.py:91-139`.
- **GOTCHA**: Pydantic's smart-mode union keeps `"010"` a `str` and `10` an `int`. Add a round-trip test
  (`model_dump(mode="json")` → `model_validate`), because JSON turns tuples into lists. `ProvenancedValue`
  rejects lists only at construction, after validation coerces to tuples, so test it.
- **VALIDATE**: `uv run pytest tests/sourcing/test_schemas.py -q && uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC 3, AC 4

### UPDATE `app/sourcing/stages.py`
- **IMPLEMENT**: Add `"source_records": PipelineStage.search_registry` to `FIELD_OWNERS`, with a comment: the
  registry stage writes every source record of the batch in one write, so a retry replaces them whole.
- **VALIDATE**: `uv run pytest tests/sourcing/test_stages.py -q`
- **SATISFIES**: AC 3

### UPDATE `app/tools/registry.py`: `StageResult.degraded_reason`
- **IMPLEMENT**: `degraded_reason: str | None = None` on `StageResult`, with this docstring: set when the stage
  finished but short of its goal; the runner finishes the run `degraded` with the reasons joined. Additive and
  defaulted, so T7/T8, which construct `StageResult(counts=…)`, are unaffected.
- **GOTCHA**: Touch nothing else in this file. T7 and T8 branched from it.
- **VALIDATE**: `uv run pytest tests/tools -q`
- **SATISFIES**: AC 6

### UPDATE `app/core/config.py` and `.env.example`
- **IMPLEMENT**: Below the cost block:
  - `sourcing_batch_size: int = Field(default=150, gt=0)`, with a D12 comment;
  - `sourcing_stale_run_hours: int = Field(default=6, gt=0)`;
  - `fmcsa_webkey: str | None = None`, commented: free, Login.gov; unset means QCMobile is skipped and the run
    ends `degraded`;
  - `socrata_app_token: str | None = None`, commented: optional; without it SODA throttles per IP.

  Mirror each in `.env.example` with a comment and no value.
- **GOTCHA**: `extra="forbid"`. Check whether `tests/core/test_config.py` snapshots the field set, and update it if so.
- **VALIDATE**: `uv run pytest tests/core/test_config.py -q`
- **SATISFIES**: AC 1, AC 7

### UPDATE `app/sourcing/exceptions.py`
- **IMPLEMENT**:
  - `NoRegistrySourceError(SourcingError)`: 409, `manifest_id`, `bound: tuple[str, ...]`;
  - `SourceRequestError`: 502, `source`, `status: int | None`;
  - `SourceAuthError(SourceRequestError)`, for the QCMobile key.

  Messages say what a person does next: "declare exactly one discovery source", "set FMCSA_WEBKEY".
- **VALIDATE**: `uv run mypy app/sourcing`
- **SATISFIES**: AC 2, AC 7

### CREATE `app/sourcing/sources/base.py`
- **IMPLEMENT**:
  - `@dataclass(frozen=True, kw_only=True) class PoolRecord`:
    - `registry_id: str`, namespaced;
    - `native_id: str`, the raw key, e.g. the DOT;
    - `fields: CandidateFields`, identity plus the discovery `SourceRecord`;
    - `currency_date: date | None`: when the registry record was last refreshed by its owner;
    - `has_principal: bool`: whether the record names a principal.

    The last two are the generic ordering keys, with vertical-neutral names. `fmcsa.py` maps `mcs150_date` and
    `company_officer_1` onto them. The pool columns use the same names.
  - `@dataclass(frozen=True) class DiscoveryPull`: `records: tuple[PoolRecord, ...]`,
    `pushed_down: tuple[str, ...]` (rule ids), `not_pushed_down: tuple[str, ...]`.
  - Protocols, each with `name: str` (the manifest source name it is bound to):
    - `DiscoverySource.pull(rules: Sequence[DisqualifierRule]) -> DiscoveryPull`;
    - `LookupSource.lookup(record: PoolRecord) -> ProvenancedValue[SourceRecord] | None`, where `None` means the
      lookup failed (count it) and a record with `rows=()` means not found;
    - `JoinSource.join(records) -> Mapping[str, ProvenancedValue[SourceRecord]]`, keyed by `registry_id`, with
      every requested id present (`rows=()` for no match).
  - Three factory dataclasses, so Pyright never has to narrow a union: `DiscoveryFactory`, `LookupFactory`,
    `JoinFactory`. Each is frozen and has:
    - `matches: Callable[[ManifestSource], bool]`;
    - `unavailable: Callable[[Settings], str | None]`, which returns a reason (for example "FMCSA_WEBKEY is not
      set") or `None`;
    - `build: Callable[[str, httpx.AsyncClient, Settings], <its role's Protocol>]`.

    `type AdapterFactory = DiscoveryFactory | LookupFactory | JoinFactory`.
  - `bind_sources(manifest, factories, client, settings) -> BoundSources(discovery, lookups, joins, unbound, degraded)`:
    - iterate `manifest.body.sources` in declaration order;
    - the first factory whose `matches` holds wins;
    - no factory matches → `unbound`, logged `sourcing.sources.source_unbound` (legacy revocations, Places,
      authority history);
    - a factory matches but `unavailable` returns a reason → `degraded` (`"<source>: <reason>"`), logged
      `sourcing.sources.source_unavailable`;
    - otherwise built, and logged `sourcing.sources.source_bound`;
    - raise `NoRegistrySourceError` unless exactly one discovery source is bound.
  - `normalise_dataset_url(url) -> str`: lower-case the host, strip any trailing slash, map
    `/d/<id>`, `/resource/<id>.json` and `/api/views/<id>` to `host/<id>`. That way the manifest's
    `https://data.transportation.gov/d/az4n-8mr2` and a resource URL match the same adapter.
- **PATTERN**: the `Stage` Protocol in `app/tools/registry.py` (properties, docstrings).
- **GOTCHA**: No `Any`. Pyright strict and Protocols with properties: copy `Stage`'s `@property` style.
- **VALIDATE**: `uv run pytest tests/sourcing/sources/test_base.py -q && uv run pyright app/sourcing/sources`
- **SATISFIES**: AC 2, AC 5

### CREATE `app/sourcing/sources/socrata.py`
- **IMPLEMENT**:
  - `class SodaClient`, built with `(client: httpx.AsyncClient, *, app_token: str | None, page_size: int = 5000)`.
    `async def query(dataset_url: str, *, where: str | None, order: str, select: str | None = None) -> list[dict[str, str]]`
    pages by `$limit`/`$offset` until a short page. It sends `X-App-Token` only when set. It retries 429, 5xx
    and transport errors with jittered backoff, up to 4 attempts, honouring `Retry-After`. Any other non-2xx is
    `SourceRequestError`. It logs `sourcing.socrata.page_fetched` with dataset, offset and row count. The rows
    are parsed as `dict[str, object]`, and only `str` values are kept; anything else is logged and dropped. This
    narrows the JSON without `Any`, and SODA returns strings.
  - `def resource_url(dataset_url) -> str`: build `https://<host>/resource/<id>.json` from any accepted form.
  - `def soql_literal(value: str) -> str`: single-quote the value and double any `'` inside it.
  - `def pushdown_where(rules, columns: frozenset[str], integer_columns: frozenset[str]) -> tuple[str | None, tuple[str,...], tuple[str,...]]`
    returns `(where, pushed_ids, skipped_ids)`. It considers only `RuleKind.predicate` rules whose `field` is in
    `columns`. Each rule becomes a **keep** clause. Every clause is parenthesised and they are joined with ` AND `.

    | operator | value type | keep clause (rule must NOT fire) |
    |---|---|---|
    | `equals` | str | `(f IS NULL OR f != 'v')` |
    | `not_equals` | str | `(f = 'v')` |
    | `in_set` | tuple[str] | `(f IS NULL OR f NOT IN ('a','b'))` |
    | `not_in_set` | tuple[str] | `(f IN ('a','b'))` |
    | `contains` | str | `(f IS NULL OR NOT contains(f, 'v'))` |
    | `not_contains` | str | `(f IS NOT NULL AND contains(f, 'v'))` |
    | `greater_than` | int, f ∈ integer_columns | `(f IS NULL OR f::number <= n)` |
    | `less_than` | int, f ∈ integer_columns | `(f IS NULL OR f::number >= n)` |
    | anything else (`is_true`, a bool value, a mismatched type, a numeric compare on a non-integer column) | — | not pushed down → `skipped_ids`, log `sourcing.socrata.pushdown_skipped` |

    The field name is checked against `columns` before it is interpolated, so it is never user text. Values go
    through `soql_literal`. An `int` value is formatted with `str(int(v))`. The output order follows the
    manifest's rule order.
- **PATTERN**: `app/promotion/client.py` for retry, jitter, constants and int status codes.
- **GOTCHA**:
  - `bool` is a subclass of `int`, so reject `bool` explicitly for the numeric operators.
  - A SoQL numeric cast on a non-numeric text value 400s server-side. The census `power_units` is numeric text in
    practice (the v3 funnel was measured this way), but a 400 must surface as `SourceRequestError` with the
    dataset in the message, never be swallowed.
  - The default `$limit` is 1000. Always send `$limit`.
- **VALIDATE**: `uv run pytest tests/sourcing/sources/test_socrata.py -q`
- **SATISFIES**: AC 2, AC 8

### CREATE `app/sourcing/sources/fmcsa.py`
- **IMPLEMENT**:
  - **Constants**:
    - `CENSUS_DATASET = "data.transportation.gov/az4n-8mr2"`;
    - `REVOCATIONS_DATASET = "data.transportation.gov/wb4f-neki"`, with a comment: Motus RevokeSuspend; the legacy
      `sa6p-acbp`/`rwr4-5nkg` is frozen since 2026-05-14 and is deliberately **not** bound;
    - `QCMOBILE_PREFIX = "mobile.fmcsa.dot.gov/qc/services"`;
    - `CENSUS_COLUMNS`: the full column list from the dataset metadata (see Documentation), as a frozenset;
    - `CENSUS_INTEGER_COLUMNS = {"power_units","truck_units","total_drivers","total_cdl","driver_inter_total"}`;
    - `NAMESPACE = "usdot"`.
  - **`CensusSource`** (discovery):
    1. `pull(rules)` calls `pushdown_where`, then runs `SodaClient.query(order="dot_number")`.
    2. For each row it builds a `PoolRecord`:
       - `registry_id = f"usdot:{dot_number.strip()}"`;
       - `legal_name` and `dba_name` when present;
       - `address = PostalAddress(phy_street, phy_city, phy_state, phy_zip)`, **only if all four are non-blank**,
         else `None`;
       - `phone` when present;
       - `source_records = (census SourceRecord with the whole row, integer columns cast,)`;
       - `currency_date = parse YYYYMMDD of mcs150_date` (invalid → `None`, logged);
       - `has_principal = bool(company_officer_1.strip())`.
    3. Every field is cited with `source_url = f"https://data.transportation.gov/resource/az4n-8mr2.json?dot_number={dot}"`,
       `retrieved_at` = **one timestamp per pull**, and `RetrievalMethod.bulk_file`.
    4. Rows without a `dot_number` are skipped and counted.
    5. It **dedupes by registry_id** (keep the last, and log a duplicate).
    6. It returns the records **sorted by registry_id**.
  - **`QcMobileSource`** (lookup):
    1. `lookup(record)` sends `GET {base}/carriers/{native_id}` with `params={"webKey": key}`, at most 5 requests a
       second (min interval 0.2 s, as T3 paces search). Retry: as `SodaClient`, 3 attempts.
    2. Parse `content`:
       - a dict with a `carrier` dict → one `SourceRow` of the carrier's `str` and `int` values, unchanged.
         Booleans, nulls and nested objects are skipped and their keys logged, never converted;
       - `content` that is `None`, an empty list, or a 404 whose body is not the webKey message → `rows=()`;
       - a body `content` string containing `"Webkey"`, or a 401/403 → raise `SourceAuthError`.
    3. Other failures after retries → return `None` and log `sourcing.qcmobile.lookup_failed`.
    4. The citation `source_url` is `https://mobile.fmcsa.dot.gov/qc/services/carriers/{dot}`, **without the
       webKey**, with `RetrievalMethod.registry_api` and `retrieved_at` per request.
  - **`RevocationsSource`** (join):
    1. `join(records)` chunks native ids by 100 and queries
       `$where=usdot_number in ('7989','12345',…)`, `$order=usdot_number,order1_serve_date`.
    2. It groups rows by the `usdot_number` with its leading zeros stripped, maps them back to `registry_id`, and
       gives `rows=()` for no match.
    3. Rows are deduped (Motus repeats rows) on the full sorted pair tuple.
    4. One citation per candidate:
       `source_url = "https://data.transportation.gov/resource/wb4f-neki.json?usdot_number={dot}"`, with
       `bulk_file`.
  - **`FMCSA_ADAPTERS: tuple[AdapterFactory, ...]`**: census (discovery), QCMobile (lookup), revocations (join).
    Each `matches` compares `normalise_dataset_url(source.base_url)` (a host+path prefix for QCMobile). QCMobile's
    `unavailable` returns `"FMCSA_WEBKEY is not set"` when `settings.fmcsa_webkey` is unset. The stage passes
    `BoundSources.degraded` into its `degraded_reason`.
- **GOTCHA**:
  - **The live QCMobile field spelling is unverified** (`allowToOperate` in the docs, `allowedToOperate`
    elsewhere). Store what the API returns; never rename. Freight v3's rule says `allowToOperate`. T7's dry-run is
    what catches a mismatch, and a real capture is a human follow-up (Open Questions).
  - **Motus `usdot_number` format is unverified** (zero-padded or not). Before writing `revocations.json`, run
    **one** live read-only query: `curl -s 'https://data.transportation.gov/resource/wb4f-neki.json?$limit=3'`.
    Copy the real shape into the fixture and note it in the module docstring. Pad or strip accordingly in the
    `IN (...)` literals; if it is padded, send both forms.
  - **Never put the webKey in a citation, a log line or an exception message.** It is a query param, so log the
    path, not `str(request.url)`.
- **VALIDATE**: `uv run pytest tests/sourcing/sources -q && uv run mypy app/sourcing && uv run pyright app/sourcing`
- **SATISFIES**: AC 2, AC 3, AC 4, AC 7

### UPDATE `app/sourcing/models.py`: `SourcingPool`
- **IMPLEMENT**: `__tablename__ = "sourcing_pool"` with these columns:
  - `id` (UUID pk);
  - `vertical` String(64);
  - `registry_id` String(`REGISTRY_ID_MAX_LENGTH`);
  - `fields` JSONB;
  - `currency_date` Date, nullable;
  - `has_principal` Boolean, not null;
  - `first_seen_run_id`, `last_seen_run_id`: FK `sourcing_run.id`, not null;
  - `batched_run_id`: FK, nullable;
  - `created_at`, `updated_at`.

  `__table_args__`:
  - `UniqueConstraint("vertical","registry_id", name="uq_sourcing_pool_vertical_registry_id")`;
  - `ck_sourcing_pool_registry_id_is_cited`, the same expression as the candidate's, which is cheapest to copy;
  - `Index("ix_sourcing_pool_vertical_last_seen", "vertical", "last_seen_run_id")`.

  The docstring covers D12: what "available" means, why `vertical` and not `manifest_id` (a new manifest version
  must not reset "already batched"), and that the pool is census-owned and overwritten whole on refresh.
- **GOTCHA**: Every constraint goes on the model **and** in the migration (T4's `alembic check` lesson, in the
  `models.py` docstring).
- **VALIDATE**: `uv run mypy app/sourcing`
- **SATISFIES**: AC 1

### CREATE `alembic/versions/0006_sourcing_pool.py`
- **IMPLEMENT**: `revision = "0006_sourcing_pool"`, `down_revision = "0005_cadence"`. A hand-written
  `op.create_table` mirroring the model exactly, and a `downgrade` that drops it. The docstring says it is
  pre-numbered for Wave 5: T8 is `0007` and T7 is `0008`, re-chained at merge.
- **VALIDATE**:
  ```bash
  DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head && \
  DATABASE_URL="$TEST_DATABASE_URL" uv run alembic check && \
  DATABASE_URL="$TEST_DATABASE_URL" uv run alembic downgrade 0005_cadence && \
  DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
  ```
  Never against `.env`'s hosted URL.
- **SATISFIES**: AC 1

### CREATE `app/sourcing/pool.py`: `PoolRepository`
- **IMPLEMENT**. Flush, never commit.
  - **`refresh(vertical, run_id, records) -> int`**, returning the count of new entries.
    - Bulk `insert(SourcingPool).values([...]).on_conflict_do_update(constraint="uq_sourcing_pool_vertical_registry_id", set_={fields, currency_date, has_principal, last_seen_run_id, updated_at=func.now()})`,
      in chunks of 500.
    - **Never set** `batched_run_id` or `first_seen_run_id` on conflict.
    - Count the new entries afterwards with `select count() where first_seen_run_id = run_id and vertical = …`.
    - The records arrive deduplicated (see `CensusSource`). Assert that, because a multi-row `ON CONFLICT` with
      duplicate keys raises "cannot affect row a second time" (T4 review note).
  - **`select_batch(vertical, run_id, size, currency_cutoff: date) -> list[SourcingPool]`**:
    - `WHERE vertical = :v AND last_seen_run_id = :run AND (batched_run_id IS NULL OR batched_run_id IN (SELECT id FROM sourcing_run WHERE status = 'failed'))`;
    - `ORDER BY (currency_date >= :cutoff) DESC NULLS LAST, has_principal DESC, registry_id ASC, id ASC LIMIT :size`;
    - then `UPDATE … SET batched_run_id = :run WHERE id IN (...)`;
    - return the rows in the selected order.
  - **`count_available(vertical, run_id) -> int`**, for the `pool_available` count before selection.
- **PATTERN**: `SourcingRepository.upsert_candidate` (`repository.py:82-150`) for `insert`, `on_conflict_do_update`
  and the logging shape.
- **GOTCHA**:
  - `(currency_date >= cutoff)` is NULL when the date is NULL. `DESC NULLS LAST` puts unknown dates with the old
    ones, after the current ones. Make the expression explicit:
    `case((SourcingPool.currency_date >= cutoff, 1), else_=0).desc()`. That avoids NULL ordering surprises and
    reads clearly.
  - The ordering is lexical on `registry_id` (`usdot:10` < `usdot:9`). It is deterministic, which is all D12 asks.
    Say so.
- **VALIDATE**: `uv run pytest tests/sourcing/test_pool.py -q`
- **SATISFIES**: AC 1, AC 4

### UPDATE `app/sourcing/repository.py` and `app/sourcing/service.py`: lifecycle hardening
- **IMPLEMENT**:
  - **`repository.mark_finished`** becomes:
    `update(SourcingRun).where(id == run.id, status == 'running').values(status=…, status_detail=…, finished_at=func.now(), counts=…, cost=…).returning(SourcingRun)`,
    with `execution_options={"populate_existing": True}`. It returns `SourcingRun | None`.
  - **`repository.reap_stale_runs(vertical, older_than: timedelta) -> list[UUID]`** runs
    `UPDATE … SET status='failed', status_detail='abandoned: still running after <n>h', finished_at=now() WHERE vertical = :v AND status='running' AND started_at < now() - :interval RETURNING id`.
  - **`service.finish_run`**: when the repository returns `None`, raise `SourcingRunNotRunningError`, with the
    current status re-read.
  - **`service.reap_stale_runs(vertical, *, older_than)`** commits and logs `sourcing.service.runs_reaped` with
    the ids.
- **GOTCHA**: The existing `finish_run` tests assert `finished_at` and status. Keep them green, and add one that a
  concurrent finish (a second `finish_run` after the first) raises without overwriting. `func.now()` is the
  transaction start time in Postgres. That is fine, and better than the app clock (T4 review).
- **VALIDATE**: `uv run pytest tests/sourcing/test_repository.py tests/sourcing/test_service.py -q`
- **SATISFIES**: AC 6

### CREATE `app/tools/search_registry.py`
- **IMPLEMENT**: `class SearchRegistryStage`, built with
  `(*, factories: Sequence[AdapterFactory] = FMCSA_ADAPTERS, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], datetime] = lambda: datetime.now(UTC), batch_size: int | None = None)`.
  - `stage` property → `PipelineStage.search_registry`.
  - `run(context)`:
    1. Open `httpx.AsyncClient(transport=self._transport, timeout=30)` for the run (`async with`).
    2. `bind_sources(context.manifest, …)`.
    3. Pull, passing `[r.value for r in manifest.body.disqualifier_rules]`.
    4. `PoolRepository(context.session).refresh(vertical, run_id, pull.records)`.
    5. `count_available`.
    6. `select_batch(size, cutoff = clock().date() - 2 years)`. Use `date.replace(year=…)`, handling Feb 29 by
       falling back to Feb 28, or `timedelta(days=730)`. **Pick `timedelta(days=730)`** and say so: "within 2
       years" with no calendar arithmetic.
    7. Steps 4–6 go through one new service method, `SourcingService.refresh_pool_and_select(run_id, vertical,
       records, *, size, currency_cutoff) -> PoolSelection(seen, new, available, selected)`. It calls
       `PoolRepository` and **commits once**, keeping the commit in the service layer per the house rule. The
       refresh and the batch marking therefore land together or not at all.
    8. For each selected entry, in order, call each lookup source sequentially. A `SourceAuthError` stops further
       lookups for that source and sets `degraded_reason`.
    9. Run each join source once over the batch.
    10. Build `CandidateFields` from the pool entry's fields, with `source_records` = discovery record + lookup and
        join records, **sorted by source name**.
    11. `SourcingService.record_candidates(run_id, fields, stage=PipelineStage.search_registry)`.
  - Return `StageResult(counts=…, degraded_reason=…)` with these count keys, all slugs:
    `pool_seen`, `pool_new`, `pool_available`, `pool_rules_pushed_down`, `pool_rules_not_pushed_down`, `batched`,
    `lookup_found`, `lookup_not_found`, `lookup_failed`, `join_matched`.
  - `STAGE = SearchRegistryStage()`.
- **GOTCHA**:
  - The stage reads `vertical` from `context.manifest.vertical`. It **never compares it to a literal**
    (`test_structure.py:219`).
  - The batch size comes from the constructor arg, else `get_settings().sourcing_batch_size`.
  - `StageContext` has no batch list by design. Later stages read candidates by `run_id`.
  - Lookup failures (`None`) count `lookup_failed`. A degraded reason is set if `lookup_failed > 0`.
- **VALIDATE**: `uv run pytest tests/tools/test_search_registry.py tests/tools/test_registry.py -q`
- **SATISFIES**: AC 2, AC 3, AC 4, AC 5, AC 7

### CREATE `app/sourcing/pipeline.py`: `SourcingPipeline`
- **IMPLEMENT**: `SourcingPipeline(session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession] = get_sessionmaker(), *, stages: Sequence[Stage] | None = None)`.
  `async def run(self, brief: SourcingBrief) -> SourcingRunResponse`:
  1. `async with factory() as session`:
     - `SourcingService(session).reap_stale_runs(brief.vertical, older_than=hours(settings.sourcing_stale_run_hours))`;
     - `run = await service.start_run(brief)`;
     - `manifest = await ManifestService(session).get(run.manifest_id)`.
  2. `bind_contextvars(run_id=str(run.id))`, cleared in `finally` with `unbind_contextvars("run_id")`.
  3. `cost = RunCost()`, `counts: dict[str, int] = {}`, `reasons: list[str] = []`.
  4. `try`: for each stage in `self._stages or registered_stages()`:
     - `result = await stage.run(StageContext(run_id=…, manifest=…, session=session, cost=cost))`;
     - merge the counts, **raising `ValueError` on a duplicate key**;
     - append any reason;
     - log `sourcing.pipeline.stage_completed`.

     Then `finish_run(degraded if reasons else completed, counts=…, cost=…, detail="; ".join(reasons) or None)`.
  5. `except BaseException as exc` (so `KeyboardInterrupt`/`CancelledError` also leave a record): best-effort
     `await session.rollback()` (suppress and log a secondary error); then `async with factory() as fresh:
     SourcingService(fresh).finish_run(run.id, failed, counts=counts, cost=cost, detail=f"{type(exc).__name__}: {exc}"[:500])`;
     log `sourcing.pipeline.run_failed`; `raise`.
  6. If a run fails before `start_run` returns (for example `ActiveManifestNotFoundError`), nothing is opened and
     the error simply propagates.

  The module docstring holds the run diagram (reap → start → stages → finish) and why it takes a factory: a failed
  transaction is aborted, so finishing on the same session raises `PendingRollbackError` (T4 review decision 3).
- **GOTCHA**:
  - Catching `BaseException` and re-raising is the one place it is justified. Comment it.
  - `asyncio.CancelledError` during the fresh-session finish can still lose the record. Accept and document: the
    reaper catches it next run.
  - The duplicate-count-key guard matters because T7/T8 stages will add keys.
- **VALIDATE**: `uv run pytest tests/sourcing/test_pipeline.py -q`
- **SATISFIES**: AC 4, AC 6

### CREATE `app/sourcing/cli.py` and UPDATE `app/cli.py`
- **IMPLEMENT**:
  - `register(parser)`: subcommand `run`, with `--vertical` (required), `--geography` (default `dfw`) and
    `--batch-size` (int > 0, optional).
  - The ICP band for the brief comes from the **active manifest's** `icp_band.value`, because the brief records
    what was asked and the CLI asks for "the manifest's band". Read it with `ManifestService.get_active` inside the
    same `asyncio.run`.
  - `dispatch(args)` builds the pipeline (with `SearchRegistryStage(batch_size=…)` when one is given) and prints
    the run id, status, detail, each count on its own line, and the cost via `format_usd`.
  - Exit codes: `completed`/`degraded` → 0 (degraded also prints `warning: <detail>` to stderr); a raised error →
    `app/cli.py`'s existing handler (1).
  - `dispose_engine()` in `finally`.
  - In `app/cli.py`: `sourcing_cli.register(commands.add_parser("sourcing", help="run the sourcing pipeline"))`
    and the dispatch branch.
- **PATTERN**: `app/cadence/cli.py:40-120`.
- **GOTCHA**: `ActiveManifestNotFoundError` is a `LocalProspectEngineError`. Confirm it renders as
  `error: …`, not a traceback (test it).
- **VALIDATE**: `uv run pytest tests/sourcing/test_cli.py -q && uv run lpe sourcing run --help`
- **SATISFIES**: AC 7

### CREATE the tests (see TESTING STRATEGY) and fixtures
- **IMPLEMENT**: `tests/sourcing/conftest.py`:
  - `session_factory`: the `db_session` recipe, returning a callable that makes a new
    `AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")` on **one**
    connection per test, rolled back at the end. That is how "a fresh session" is tested without leaking state.
  - `MockSoda`, a router keyed on (dataset id, `$offset`), which **raises on unknown requests** and records the
    `$where` it received;
  - `MockQcMobile`, keyed on the DOT path, which records the query params;
  - `load_fixture`.
- **GOTCHA**: Fixtures are synthetic but **schema-accurate**. The census rows carry only string values and omit
  empty fields, as the live API does. Base the shapes on the one live read-only capture taken in the fmcsa task.
- **VALIDATE**: `uv run pytest tests/sourcing tests/tools -q`
- **SATISFIES**: AC 4, AC 5, AC 8

### UPDATE docs
- **IMPLEMENT**:
  - `app/sourcing/README.md`: the pool and batch (D12), the runner and crash path, sources and binding, the
    **Contract with T7** section (verbatim from this plan), and the `lpe sourcing run` usage.
  - `docs/tickets/local-prospect-engine.md` T5: a short "*Built 2026-10-xx*" note. It covers the pool table, the
    contract with T7, the Motus revocations finding, and freight v4 being needed before the first live run.
  - `CLAUDE.md` architecture map: add `app/sourcing/` pipeline, pool, sources and cli, `app/tools/search_registry.py`
    and migration `0006`; add `lpe sourcing run` to *Commands*.
  - `_tasks/todo.md`: **append** a T5 section. Never overwrite (memory: *todo.md is a cumulative work log*).
- **VALIDATE**: read it back. Run `/rules-check-drift` if time allows.
- **SATISFIES**: AC 10

---

## TESTING STRATEGY

pytest + pytest-asyncio, as the existing suites run them. Database tests carry `requires_db`. **No live network
in any test**: every client gets a MockTransport that raises on unknown routes.

### Unit (no DB)
- **`test_socrata.py`**:
  - each operator row of the pushdown table, as exact SoQL strings;
  - quote escaping (`O'Neil`);
  - `bool` rejected for numeric operators;
  - an `is_true` rule, a judgment rule and a non-census field (`allowToOperate`) are skipped and reported;
  - **freight v3's actual rules**, copied from the dev manifest into a builder, produce the expected `$where`, with
    `allowToOperate` skipped;
  - paging stops on a short page;
  - `$limit` is always sent;
  - `X-App-Token` is sent only when set;
  - a 429 followed by a 200 retries;
  - a 400 → `SourceRequestError`, naming the dataset;
  - a non-string JSON value is dropped.
- **`test_fmcsa.py`**:
  - census row → `PoolRecord` field mapping, with `usdot:` prefixed;
  - an address is `None` when any part is missing;
  - `"N/A"` power units → the key is absent from the row, and logged;
  - `currency_date` parsing, including an invalid date;
  - duplicates deduplicated;
  - records sorted;
  - citations: the census URL per DOT, `bulk_file`, one `retrieved_at` per pull;
  - QCMobile found / `content: null` / 404 / webKey-missing (→ `SourceAuthError`) / 500×3 (→ `None`);
  - **the webKey never appears in a citation URL or in captured logs** (`structlog.testing.capture_logs`);
  - revocations: grouping, deduplication of repeated Motus rows, no match → `rows=()`, chunking at 100 ids.
- **`test_base.py`**:
  - `normalise_dataset_url` equivalences (`/d/`, `/resource/….json`, `/api/views/`, case, trailing slash);
  - `bind_sources`: freight-shaped manifest → census + QCMobile + revocations bound and authority-history unbound;
  - legacy `rwr4-5nkg` → unbound;
  - zero or two discovery sources → `NoRegistrySourceError`;
  - fire manifest with the default table → `NoRegistrySourceError`;
  - fire manifest with a stub table → bound.
- **`test_schemas.py`** (extend): the `SourceRow` sort and duplicate keys; a duplicate `source` is rejected; the
  JSON round trip keeps `str` vs `int`; a Places-cited `SourceRecord` is rejected by the D13 guard.
- **`test_provenance.py`** (extend): `"n/a"`, `"ftp://x"`, `"https://"` and `"mobile.fmcsa.dot.gov/x"` are
  rejected; http and https are accepted.

### Integration (DB)
- **`test_pool.py`**:
  - refresh inserts, and a second refresh of the same records changes nothing but `last_seen_run_id`;
  - refresh never clears `batched_run_id`;
  - `select_batch` order: current-dated with a principal → current-dated without → old/none-dated with a principal
    → the rest → `registry_id`;
  - an entry not seen in this refresh is not selectable;
  - an entry batched by a `failed` run is selectable again, and one batched by a `completed`/`degraded`/`running`
    run is not;
  - the cited-registry-id CHECK refuses an uncited row.
- **`test_repository.py` / `test_service.py`** (extend):
  - a conditional finish: a second `finish_run` raises `SourcingRunNotRunningError`, and the stored outcome is
    unchanged;
  - `finished_at` comes from the DB (a non-null value, `>= started_at`);
  - the reaper fails only stale `running` runs of that vertical, sets the detail, and leaves recent or other
    verticals' runs alone.
- **`test_pipeline.py`**, the acceptance tests:
  1. **Determinism**: the same fixtures, two fresh databases or two separate pools (use two verticals with
     identical manifests), give identical candidate `registry_id` lists, in order, with identical
     `fields.model_dump()` apart from `retrieved_at`. Inject the clock **and** pin `retrieved_at` through the
     clock, so compare whole.
  2. **Disjoint batches (D12)**: a pool of 8 and a batch of 3. Run 1 takes A,B,C; run 2 takes D,E,F; run 3 takes
     G,H. Repeat the whole thing in a new transaction: same order. No `registry_id` appears in two runs.
  3. **A failed run gives its batch back**: a stage that raises after `search_registry` →
     - the run is `failed`, `finished_at` is set, and the cost snapshot is present;
     - the exception is re-raised;
     - the next run re-selects the same entries first.
  4. **Crash path on an aborted transaction**: a stage that forces a DB error (for example an insert violating a
     CHECK) is still finished `failed` (fresh session), with no `PendingRollbackError`.
  5. **Degraded**: no `fmcsa_webkey` → `degraded`, with the detail naming `FMCSA_WEBKEY`; candidates still
     recorded, with no QCMobile record.
  6. **Every field cited**: every stored candidate's populated fields carry a citation with an http(s) URL; the
     `source_records` sources are exactly the bound ones.
  7. **The run reaps** a stale `running` run before starting.
  8. **Duplicate count keys** across two stages → `failed`.
  9. **No active manifest** → `ActiveManifestNotFoundError`, and no run row created.
- **`tests/tools/test_search_registry.py`**:
  - `registered_stages()` (the real package) now returns `(search_registry,)`, plus whatever T7/T8 add later, so
    assert membership, not equality;
  - **Portability (M9)**: the fire manifest activated with a `tx_fire_marshal` stub `DiscoverySource` bound by its
    `base_url` → the **same `SearchRegistryStage` class** records the stub's candidates; the stub received the fire
    manifest's rules; **no FMCSA request was made** (the MockSoda raises if touched).
- **`test_cli.py`**: `lpe sourcing run --vertical <active>` prints the run id and counts and exits 0; `degraded`
  exits 0 with a stderr warning; an unknown or draft-only vertical → `error: …` and exit 1; `--batch-size 0` is
  rejected by argparse.

### Edge cases
- A census row with no `dot_number` → skipped and counted.
- An empty pull → run `completed`, `batched = 0`, no candidates.
- A batch larger than the available pool → takes what exists.
- A QCMobile auth failure mid-batch → lookups stop for that source, the rest of the batch is still recorded, and
  the run is `degraded`.
- A revocations join chunk that fails → `SourceRequestError` propagates → the run is `failed` (a join is one
  call per 100, so retries are cheap; failing loudly beats storing a false "no revocations").
- Feb 29 run date → the cutoff via `timedelta(days=730)`, so there is no calendar error.
- `registry_id` longer than 128 → refused by `CandidateFields` before any write (existing validator).

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy . && uv run pyright
```
### Level 2: Unit Tests
```bash
uv run pytest tests/sourcing/sources tests/sourcing/test_schemas.py tests/sourcing/test_stages.py tests/shared tests/tools -q
```
### Level 3: Integration Tests (throwaway Postgres on 5434)
```bash
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/postgres
uv run pytest -q            # full suite; with TEST_DATABASE_URL set, skipped must be 0
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head && DATABASE_URL="$TEST_DATABASE_URL" uv run alembic check
```
Or everything at once: `/piv-validate`.
### Level 4: Manual Validation (read-only, public endpoints; no writes to dev Supabase)
1. Verify the SoQL `contains()` and the numeric cast against the live census, using freight v3's real `$where`
   (printed by a unit test or a scratch script):
   `curl -sG 'https://data.transportation.gov/resource/az4n-8mr2.json' --data-urlencode '$select=count(*)' --data-urlencode "\$where=<v3 where>"`.
   **Expect ≈ 4,449** (the measured funnel, 2026-10-09). A large difference means the pushdown semantics differ
   from the hand-measured funnel. Stop and report it.
2. Check the Motus revocations shape: `curl -s 'https://data.transportation.gov/resource/wb4f-neki.json?$limit=3'`.
   Confirm the `usdot_number` format matches the fixture.
3. `uv run lpe sourcing run --help`.
4. **Do not** run `lpe sourcing run` against dev Supabase. Freight v3 is DRAFT, so it would refuse anyway, and
   activation is the human's call (Open Questions).
### Level 5: Additional
- `code-reviewer` agent on the diff, then `/piv-create-pr` and `/piv-review-pr` as the Wave 5 checklist says.

---

## ACCEPTANCE CRITERIA

1. [ ] `sourcing_pool` exists (migration `0006`, `down_revision = "0005_cadence"`). Refresh upserts on
       `(vertical, registry_id)` and never clears `batched_run_id`. `alembic check` is clean.
2. [ ] `search_registry` is generic. It binds sources by `base_url`, pushes down census-column predicate rules,
       and never branches on vertical. FMCSA lives only in `app/sourcing/sources/fmcsa.py`.
3. [ ] Every candidate field is cited. `source_records` holds the census row, the QCMobile carrier and the
       revocation rows, each with its own http(s) citation. The webKey appears in no citation or log.
4. [ ] **D12**: the batch is best-first (current MCS-150 → named officer → `registry_id`). Two runs over one fixture
       pool select disjoint batches in the same order every time. A failed run's batch returns to the pool.
       **Determinism**: same fixtures → identical candidates and order.
5. [ ] **Portability**: the same stage, pointed at the fire manifest with a stub source, records the stub's
       candidates and makes no FMCSA call.
6. [ ] The runner executes `registered_stages()` in order. A stage exception finishes the run `failed` on a fresh
       session with its cost and counts, and re-raises. `finished_at` comes from the DB clock. The finish is
       conditional. Stale running runs are reaped. `run_id` is in the log context.
7. [ ] `lpe sourcing run --vertical <slug> [--batch-size N]` works. A missing `FMCSA_WEBKEY` → `degraded` with a
       clear detail; no active manifest → `error:` and exit 1.
8. [ ] Census integer columns are cast at the adapter. Non-numeric values are left absent and logged. Recorded
       fixtures only; no live network in tests.
9. [ ] `source_url` must be an http(s) URL with a host (deferred finding #2).
10. [ ] The README (with the Contract with T7), the T5 ticket note, the CLAUDE.md map and commands, and the
        `_tasks/todo.md` section are updated.
11. [ ] ruff, mypy, pyright and pytest are all green, with **zero skips** with `TEST_DATABASE_URL` set and zero
        suppressions.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order, each task's VALIDATE run immediately
- [ ] Level 1–3 green; Level 4 steps 1–2 run, with the result recorded in the PR body (the pool count vs 4,449)
- [ ] Mutation check on the three acceptance tests (disjoint batches, the crash path, portability): break the code
      and watch each fail (the house habit from PR #16/#17)
- [ ] No `if vertical ==`, no `Any`, no `# type: ignore`
- [ ] Execution report via `/system-execution-report` after implementation

---

## OPEN QUESTIONS / ASSUMPTIONS

**For the human (do not block implementation; all are needed before the first *live* run):**
1. **Freight v3 declares the frozen revocations file** (`rwr4-5nkg`, legacy, not updated since 2026-05-14).
   T5 binds only Motus RevokeSuspend (`wb4f-neki`), so under v3 the join is **unbound** and T7's
   `authority_revoked` has no data. **Proposal:** before activating, make a **freight v4** draft that replaces that
   source's `base_url` with `https://data.transportation.gov/d/wb4f-neki`. Also consider swapping
   `fmcsa_authority_history` (`u4i8-4m26`) for Motus Carrier (`inys-ebih`), which carries `op_auth_type`/`status`.
   Is binding the legacy dataset as a fallback ever acceptable? The plan says **no**: stale revocations read as
   "clean".
2. **An FMCSA webKey** (free, Login.gov, ~2 min) is still ❌. Without it every run is `degraded` and has no
   `allowToOperate`. Once it exists, capture **one** real `/carriers/{dot}` response, to settle `allowToOperate`
   vs `allowedToOperate` against v3's rule.
3. **Null semantics** (*Contract with T7* §3) are T5's call. T7's plan should confirm or object before T7
   implements its evaluator.

**Assumptions:**
- `ManifestService.get(id)` returns DRAFT or ACTIVE alike. The runner uses it for the run's pinned version.
- `sourcing_run.counts` accepts any slug keys (T4: "each stage adds its own key").
- One user, so no row lock on `select_batch` (CLAUDE.md: no run-lock until needed). Two concurrent runs of one
  vertical could double-batch. That is accepted and documented, like T4's remote race.
- The census `dot_number` is unique per row. Deduplication protects the bulk upsert anyway.
- Lexical `registry_id` ordering is acceptable as D12's "stable tie-breaker".

## NOTES (open canvas)

**Why a pool table, not candidate rows** (decided with the human). The candidate table is keyed per run, which is
right for "what this run did". The backlog is cross-run state: "has this business ever been batched?". Folding it
into candidates would need either a synthetic never-finishing run or a cross-run scan of every candidate. The
pool is census-owned and overwritten whole on refresh. Candidates are owned per field, so T6/T7 can enrich them
without touching the pool.

**Why `last_seen_run_id` instead of deleting.** A record that stops matching (it went inactive, or moved out of
the area) simply is not seen by the next refresh, so it is not selectable. Its history (`batched_run_id`)
survives for T7's suppression, and nothing is deleted.

**Why "failed returns the batch" is a query, not an update.** A hard kill cannot run an `except`. Making
availability depend on the batching run's *status* means the reaper's one `UPDATE sourcing_run` is the whole
recovery.

**Why bind by `base_url`.** A source name is a per-manifest slug (`fmcsa` in v1, `fmcsa_company_census` in v3),
whereas the dataset URL is the source's identity. Binding by vertical is forbidden. Binding by URL also gives
D13 for free: Places has no discovery adapter, so it cannot discover.

**Why one QCMobile call, not three.** The ticket text predates v3. The only QCMobile field any rule reads is
`allowToOperate`, which is on `/carriers/{dot}`. `/authority` and `/docket-numbers` would triple the calls for
fields no rule consumes (YAGNI). When a rule needs broker authority status, add it as a second lookup. Better,
use Motus Carrier via SODA, with no key and batched (Open Question 1).

**Pushdown vs T7's evaluator: one rule language, two media.** That is accepted for scale: the census is 4.5M
rows. The risk is semantic drift, so the null semantics are written down as a contract and the Level-4 count
check compares the pushdown against the hand-measured 4,449.

**Cost.** T5 makes no paid call. QCMobile and SODA are free. 150 QCMobile calls at 5/s take about 30 s, and the
pool refresh is about 1 SODA page plus about 9 bulk upserts of 500 rows.

**Risk ranking:**
1. The live QCMobile shape. Fixtures can only encode the docs, which was T11's lesson: test doubles encode the
   author's assumption.
2. The Motus `usdot_number` format.
3. SoQL `contains()` support. Mitigated by Level 4.
4. The `session_factory` test fixture, if savepoint-bound fresh sessions misbehave after an aborted statement.
   Fallback: test the crash path with a separate engine on the throwaway DB, cleaning up explicitly.

## AMENDMENTS

- 2026-10-10 — **Implementation deviations** (details in `.claude/reports/t5-pipeline-fmcsa-report.md`):
  `sources/http.py` added (one GET-with-retries for SODA and QCMobile, rather than two copies);
  factories take a `SourceEnv` (client, settings, clock, backoff, throttle) instead of `(client, settings)`,
  so citations can be pinned and tests need not sleep; `CandidateFields.to_stored()` added, because an empty
  `source_records` was stored as `[]`; `SourcingService.refresh_pool_and_select` returns `PoolSelection`/`PoolEntry`
  (new schemas); `SearchRegistryStage.with_batch_size()` so `--batch-size` keeps the stage's configuration;
  the portability test lives in `tests/sourcing/test_portability.py` (where the DB fixtures are), not `tests/tools/`;
  event names renamed to pass the `domain.component.action_state` guard (e.g. `sourcing.qcmobile.lookup_failed`).
- 2026-10-10 — **Level 4 risks closed before coding:** live SODA accepts `contains()` and `::number`; freight v3's
  `$where` returns **4,464** (vs 4,449 hand-measured on 10-09); Motus `usdot_number` is unpadded.
