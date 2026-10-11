# Feature: T8 — Routing slice: DFW route clustering (`cluster_routes`, stage 5 of 5)

The following plan should be complete, but its important that you validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils types and models. Import from the right files etc.

> **Where to implement.** This plan was written from the `t5` worktree. Build it in the **`t8` worktree**
> (`worktrees/t8`, branch `feat/t8-routing`, cut from `5a80710`, the same base). Copy this file into that
> worktree's `.claude/plans/` first.

## Feature Description

Stage 5 of the weekly pipeline. It takes a run's candidates that have a **verified address**, geocodes each
census address through the **US Census Geocoder** (free, public domain, no key), and groups the points into
**drive routes** of at most ~12 doors. It labels each route the way the hand-built target list does
("Route Cluster A: Olympic Drive", "Cluster B: 75238"), and persists one assignment row per clustered
candidate in a routing-owned table. Clustering is **deterministic geometry with no LLM**: the same input gives
the same clusters and labels, whatever order the candidates arrive in.

## User Story

As a Compumatrice founder doing door-knock outings
I want each week's qualified prospects to arrive already grouped into two geographic drive routes of ~10–12 doors
So that the Monday-afternoon "build the week's door list, two clusters, 20 names" job (E4) disappears and the outings actually happen (E3)

## Problem Statement

E4 says route-building is a manual weekly task. E5 says addresses get looked up by hand in Google Maps. D13
rules Google Places out as a coordinate source: its lat/lng may be cached for only 30 days and is barred as
input to point-in-polygon analysis. So the system needs its own licensed coordinates and a clustering it can
reproduce.

## Solution Statement

A new vertical slice, `app/routing/`, and a stage module, `app/tools/cluster_routes.py`:

1. **Select** the run's candidates (`SourcingService.list_candidates`) and keep those with
   `CandidateFields.has_verified_address()`. The others are counted and excluded, never geocoded from a guess.
2. **Geocode** each kept census address with the Census Geocoder's one-line JSON endpoint. Exactly one
   `addressMatches` entry is a match. Zero is *unmatched*, and two or more is *ambiguous*. Both are
   excluded and counted. We never pick one of several matches.
3. **Cluster** the points with a pure function, `cluster_points()`: k = ⌈n / max_doors⌉ routes, built by
   **recursive balanced median bisection** on a local km projection. That is deterministic and capacity-safe,
   and it needs no dependency.
4. **Label** routes A, B, C… in a fixed geographic order, each anchored to its modal 5-digit ZIP
   (for example, `A · 75238`).
5. **Persist** a `route_assignment` row per clustered candidate: run, candidate, the cited point, the cluster
   index and label. A re-run of the stage for the same run **replaces** that run's rows in one transaction, so
   the stage is idempotent.
6. **Report** counts back through `StageResult` for `sourcing_run.counts`.

## Out of Scope / Non-Goals

- **Leaving out disqualified candidates.** T7's `disqualification` table is being built in parallel. T8
  clusters every candidate with a verified address. Filtering to "qualified and not disqualified" is a
  **recorded follow-up for T9/T10** (decided with the user, 2026-10-10). Do not import from
  `app/qualification/`.
- **Drive order within a route** (TSP or nearest-neighbour stop sequence). A route is a set of doors, not an
  itinerary.
- **The pipeline runner.** T5 builds it over `registered_stages()`. T8 only exports `STAGE`.
- **Writing the route cluster to HubSpot.** The custom property and the write belong to T9. Routing is
  Supabase machine state.
- **A CLI or HTTP route** for viewing clusters. T10 owns the report. Validate through tests and the Level 4
  snippet.
- **Any `CandidateFields` change** or edit to `app/sourcing/stages.py`. `cluster_routes` "reads fields and owns
  none" stays true (decided with the user, 2026-10-10).
- **Google Places in any form**, and **any metro hard-coding**: there is no DFW bounding box, and no
  `if geography == "dfw"`.
- **The Census batch endpoint** (`addressbatch`, CSV). See NOTES.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium
**Primary Systems Affected**: new `app/routing/` slice, new `app/tools/cluster_routes.py`, `app/core/config.py` (one setting), `alembic/versions/0007_routing.py`
**Dependencies**: none new. `httpx` (already a dependency) for the Census Geocoder, and `math` from the stdlib.

## Related Work

**Implements**: T8 in `docs/tickets/local-prospect-engine.md` (§ *T8 — Routing slice*) · **Epic**: `docs/local-prospect-engine.architecture.md` + `docs/local-prospect-engine.prd.md`

**Back-references**:

- `.claude/plans/t4-sourcing-model.md`: the `candidate` and `sourcing_run` tables and `CandidateFields` that T8 reads.
- PR #17 (`feat/tools-stage-seam`): `app/tools/registry.py`, the `Stage`, `StageContext` and `StageResult` contract T8 implements.
- `_tasks/todo.md` § *Before T8: "owned ≠ verified", redone for D13*: why `has_verified_address()` is presence of `business_check`, not a retrieval method.
- `.claude/plans/t3-hubspot-gateway.md`: the injected-`httpx.MockTransport` test seam and the retry/backoff shape mirrored here.

**Forward-references**:

- T9 (promotion) must filter clusters to promoted or qualified candidates and write the `route cluster` custom property.
- T10 (orchestration) runs T8 after T7, and decides whether to re-cluster after disqualification (see Open Questions).

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `CLAUDE.md` (whole file): ground rules (D13, strict typing, zero suppressions, structlog event names, vertical-is-data).
- `app/tools/registry.py` (whole file, 106 lines): the `Stage` Protocol, `StageContext(run_id, manifest, session, cost)`, `StageResult(counts)`, and how a stage module is discovered (`app/tools/<PipelineStage value>.py` exporting `STAGE`, with `STAGE.stage` an actual `PipelineStage` member).
- `app/sourcing/stages.py` (lines 39–40): the `PipelineStage.cluster_routes` docstring, "Reads fields and owns none". Keep it true.
- `app/sourcing/schemas.py`:
  - lines 86–100: `PostalAddress(street, city, state, postal_code)`, the geocoder input.
  - lines 152–228: `CandidateFields`. Read `has_verified_address()` at lines 210–218.
  - lines 270–281: `CandidateResponse(id, run_id, registry_id, fields)`.
- `app/sourcing/service.py` (lines 112–116): `SourcingService.list_candidates(run_id)`, already ordered by registry id. Call it; do not query `candidate` from routing.
- `app/sourcing/models.py` (whole file): the table/model conventions to mirror. Constraints are declared on the model **and** written by hand in the migration, with a docstring per column and `__table_args__`.
- `app/sourcing/repository.py` (lines 1–35, 59–63): "the repository flushes; the service commits", plus logger usage.
- `app/sourcing/exceptions.py` (whole file): slice exception base pattern (`default_code`, `status_code`, keyword context).
- `app/shared/provenance.py` (lines 66–91 and the `ProvenancedValue` class above them): cite a geocode with `RetrievalMethod.web_lookup` ("a paid or public per-request web/API lookup"). Values must be frozen models.
- `app/promotion/client.py`:
  - lines 1–20: module docstring style.
  - lines 83–90: `_MAX_ATTEMPTS`.
  - lines 182–186: `_backoff_seconds`.
  - lines 219–237: the constructor with an injected `transport: httpx.AsyncBaseTransport | None`.
  - lines 239–400: `_request` retry loop. Mirror its shape at a smaller scale.
- `app/core/config.py` (lines 39–40, 55–56): how a numeric setting with `Field(gt=0)` is declared and commented.
- `app/core/logging.py`: `get_logger`. Event names follow `domain.component.action_state`.
- `alembic/versions/0004_sourcing.py` and `0005_cadence.py` (header + `upgrade`/`downgrade`): hand-written migration style.
- `tests/conftest.py` (lines 35–50, ~140): `requires_db`, `db_session`, `TEST_DATABASE_URL`.
- `tests/sourcing/builders.py` (whole file): `sourced()`, `place_checked()`, `a_candidate()`, `an_active_manifest()`, `a_brief()`. Reuse them; don't re-invent.
- `tests/promotion/conftest.py` (lines 1–120): the `MockPortal` / `json_responder` pattern. **Raise on any unrouted request** so "no live network" is proven.
- `tests/tools/test_registry.py`: how registry tests are shaped. Add one test that the real `app.tools` package now registers `cluster_routes`.
- `tests/test_structure.py` (lines 156–200): guards your slice must pass (no empty packages, exactly one alembic head, no `Any`, no `# type: ignore`, no branch on a vertical literal).

### New Files to Create

- `app/routing/__init__.py`: empty package marker.
- `app/routing/README.md`: one-page slice README, mirroring `app/sourcing/README.md`. Cover what it does, D13, the algorithm, eligibility, and the T9/T10 follow-up.
- `app/routing/schemas.py`: `GeoPoint`, `RoutePoint`, `RouteCluster`, `GeocodeOutcome` and related shapes, plus `RouteAssignmentResponse`.
- `app/routing/clustering.py`: the pure `cluster_points()` and its helpers. No I/O, no DB, no logging side effects beyond debug.
- `app/routing/geocoder.py`: `CensusGeocoder`, an httpx client with an injected transport, retries, and Pydantic-parsed responses.
- `app/routing/models.py`: the `RouteAssignment` ORM model.
- `app/routing/repository.py`: `replace_run_assignments()` and `list_run_assignments()`.
- `app/routing/service.py`: `RoutingService.cluster_run()`, which selects, geocodes, clusters, persists and commits.
- `app/routing/exceptions.py`: `RoutingError`, `GeocoderUnavailableError`, `GeocoderResponseShapeError`.
- `app/tools/cluster_routes.py`: the stage module, exporting `STAGE`.
- `alembic/versions/0007_routing.py`: the `route_assignment` table.
- `tests/routing/__init__.py`, `tests/routing/builders.py`, `tests/routing/fixtures/*.json`, `tests/routing/test_clustering.py`, `tests/routing/test_geocoder.py`, `tests/routing/test_repository.py`, `tests/routing/test_service.py`, `tests/tools/test_cluster_routes.py`.

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [Census Geocoding Services API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html), section *Address lookups → One Line / Address*. Why: the endpoint and parameters used.
  - Endpoint: `GET https://geocoding.geo.census.gov/geocoder/locations/address?street=…&city=…&state=…&zip=…&benchmark=Public_AR_Current&format=json`
  - **Response shape, verified live 2026-10-10** (this is the fixture template):
    ```json
    {"result":{"input":{"address":{"zip":"20233","city":"Washington","street":"4600 Silver Hill Rd","state":"DC"},
      "benchmark":{"isDefault":true,"benchmarkDescription":"Public Address Ranges - Current Benchmark","id":"4","benchmarkName":"Public_AR_Current"}},
     "addressMatches":[{"tigerLine":{"side":"L","tigerLineId":"657091557"},
       "coordinates":{"x":-76.92836638093,"y":38.84505589808},
       "addressComponents":{"zip":"20233","streetName":"SILVER HILL","city":"WASHINGTON","state":"DC","suffixType":"RD", "...": "..."},
       "matchedAddress":"4600 SILVER HILL RD, WASHINGTON, DC, 20233"}]}}
    ```
  - **`x` is longitude and `y` is latitude.** Swapping them is the classic bug, and a test pins it.
  - A miss is `"addressMatches": []` with HTTP 200, not a 404.
- [Census Geocoder documentation (PDF)](https://www2.census.gov/geo/pdfs/maps-data/data/Census_Geocoder_User_Guide.pdf): benchmarks and vintages. Why: `Public_AR_Current` is the only benchmark needed (`locations` returns coordinates only, with no geographies).
- Maps Platform Terms §3.2.3, as summarised in architecture → *Boundaries & contracts → Google Places*. Why: the reason Places coordinates are banned here.

### Patterns to Follow

**Naming:** snake_case modules and functions, PascalCase models. A slice exception base is `RoutingError(LocalProspectEngineError)` with `default_code: ClassVar[str] = "routing_error"`.

**Logging** (`domain.component.action_state`):
```python
logger.info("routing.service.run_clustered", run_id=str(run_id), clustered=n, clusters=k)
logger.warning("routing.geocoder.request_failed", attempt=attempt, status=response.status_code)
logger.info("routing.repository.assignments_replaced", run_id=str(run_id), count=len(rows))
```
Never log a full address at info level (it is prospect data in logs). Log the `registry_id` instead.

**Transactions:** the repository flushes, the service commits (`app/sourcing/repository.py:1-5`).

**Provenance:** a geocoded point is `ProvenancedValue[GeoPoint]` with:
- `source_url` = the exact request URL, built with `httpx.URL(..., params=...)` so it is reproducible.
- `retrieved_at` = `datetime.now(UTC)` at response time.
- `retrieval_method` = `RetrievalMethod.web_lookup`.

`GeoPoint` must be `frozen=True`, because `ProvenancedValue` refuses mutable values.

**Stage module shape** (mirrors the registry test's `_STAGE_MODULE`):
```python
class _ClusterRoutes:
    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.cluster_routes

    async def run(self, context: StageContext) -> StageResult:
        result = await RoutingService(context.session).cluster_run(context.run_id)
        return StageResult(counts=result.counts)

STAGE = _ClusterRoutes()
```
The geocoder is constructed inside the service (with an optional injected `CensusGeocoder` for tests), not in
`StageContext`. The registry contract is not changed.

**Testing HTTP:** an injected `httpx.MockTransport`, **not** `respx` or `pytest-httpx` (see the rationale at
`tests/promotion/conftest.py:1-14`). Unrouted requests raise.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation: shapes and the pure algorithm
Schemas, the setting, `cluster_points()` and its exhaustive unit tests. This needs no DB and no network.

### Phase 2: Geocoder
**Independent of:** Phase 1's clustering (it shares only `GeoPoint`).
`CensusGeocoder` with fixtures and a mock transport.

### Phase 3: Persistence
**Depends on:** Phase 1 (schemas).
`RouteAssignment` model, migration `0007`, repository.

### Phase 4: Service + stage + integration
**Depends on:** Phases 1–3.
`RoutingService.cluster_run`, `app/tools/cluster_routes.py`, the registry test, README, docs.

### Phase 5: Validation
Full `/piv-validate`, the DB tier on the T8 worktree's throwaway Postgres, and the Level 4 snippet.

---

## STEP-BY-STEP TASKS

### 1. UPDATE `app/core/config.py`
- **IMPLEMENT**: add, under a new `# Routing (T8)` comment after the cost block:
  `route_max_doors: int = Field(default=12, gt=0)`. The comment cites E3: ~10–12 doors an outing, 20 names in
  2 clusters a week. It is a cap per route, so the route count follows from n.
- **GOTCHA**: `extra="forbid"`. Name it exactly this, and add `ROUTE_MAX_DOORS=12` (commented) to
  `.env.example` if that file lists the other tunables.
- **VALIDATE**: `uv run pytest tests/core -q`
- **SATISFIES**: AC #3 (size caps)

### 2. CREATE `app/routing/schemas.py`
- **IMPLEMENT**:
  - `GeoPoint(BaseModel, frozen)`:
    - `latitude: float = Field(ge=-90, le=90)` and `longitude: float = Field(ge=-180, le=180)`.
    - Docstring: Census coordinates only, never Places (D13).
  - `RoutePoint(BaseModel, frozen)`: `candidate_id: UUID`, `registry_id: str`, `point: GeoPoint`, `postal_code: str`. This is the algorithm's input.
  - `RouteCluster(BaseModel, frozen)`:
    - `index: int` (0-based, in label order), `label: str`, `anchor_postal_code: str`.
    - `members: tuple[RoutePoint, ...]`, sorted by `registry_id`.
  - `GeocodeMatchStatus(StrEnum)`: `matched`, `unmatched`, `ambiguous`.
  - `GeocodeOutcome(BaseModel, frozen)`: `status`, plus `point: ProvenancedValue[GeoPoint] | None`. A model validator requires point ⇔ matched.
  - `RoutingResult(BaseModel)`: `counts: dict[StageName, StageCount]` (import both from `app.sourcing.schemas`) and `clusters: tuple[RouteCluster, ...]`.
  - `RouteAssignmentResponse(from_attributes)`: id, run_id, candidate_id, cluster_index, cluster_label, `point: ProvenancedValue[GeoPoint]`, created_at.
  - Pydantic models for the Census response, parsing only what we read: `_CensusCoordinates(x, y)`, `_CensusMatch(coordinates, matched_address)`, `_CensusResult(address_matches)` with aliases `addressMatches` and `matchedAddress`, and `CensusResponse(result)`. Use `populate_by_name`. Extra keys are ignored, because the input echo is large.
- **PATTERN**: `app/sourcing/schemas.py:86-100` (a frozen value model) and `:270-281` (a response model).
- **GOTCHA**: no `Any`. Annotate `dict[str, object]` where needed. A `postal_code` may be ZIP+4, so take the first 5 digits for the anchor in `clustering.py`, not here.
- **VALIDATE**: `uv run mypy app/routing && uv run pyright app/routing`
- **SATISFIES**: AC #1, #5

### 3. CREATE `app/routing/clustering.py`
- **IMPLEMENT**: `cluster_points(points: Sequence[RoutePoint], *, max_doors: int) -> tuple[RouteCluster, ...]`
  1. If `points` is empty, return `()`. Raise `ValueError` if `max_doors < 1`. Raise `ValueError` on a duplicate `candidate_id`.
  2. **Canonicalise order:** sort by `(registry_id, str(candidate_id))`. Everything after this depends only on the set, not the input order.
  3. **Project** to a local plane in km:
     - `lat0` = mean latitude of the input.
     - `x = lon × 111.320 × cos(radians(lat0))`, `y = lat × 110.574`.
     - The projection is computed from the data, so no metro is hard-coded.
  4. `k = ceil(n / max_doors)`.
  5. `_bisect(items, k)`, recursive:
     - If `k == 1`, return `[items]`.
     - Choose the axis with the larger spread (`max − min`). On a tie, use x.
     - Sort by `(axis_value, registry_id)`.
     - `k_left = ceil(k / 2)`, `n_left = ceil(n × k_left / k)`. Use integer math: `(n * k_left + k - 1) // k`.
     - Recurse into `items[:n_left]` with `k_left` and `items[n_left:]` with `k - k_left`.
     - This guarantees every group ≤ `max_doors` and sizes differ by at most 1 at each split (20 → 10/10, 23 → 12/11).
  6. **Order clusters for labelling:** sort by centroid `(round(x, 6), round(y, 6))`, west to east, then south to north. Then assign `index` 0… and labels `A`, `B`, … via `_label(i)` (`A`…`Z`, then `AA`, `AB`…).
  7. **Anchor** = the modal 5-digit ZIP among members. On a tie, take the lowest ZIP. `label = f"{letter} · {anchor}"`.
- **PATTERN**: pure-function module with a module docstring explaining *why*. See the docstring tone of `app/sourcing/stages.py:1-18`.
- **GOTCHA**:
  - **Floats and determinism.** Never use set or dict iteration order for output. All sorts carry `registry_id` as the tie-breaker.
  - Identical coordinates (the same suite address) must not break the split. The tie-breaker handles it.
  - Do not import `random`, `sklearn` or `numpy`.
- **VALIDATE**: `uv run pytest tests/routing/test_clustering.py -q`
- **SATISFIES**: AC #2, #3, #4

### 4. CREATE `tests/routing/builders.py` and `tests/routing/test_clustering.py`
- **IMPLEMENT** builders:
  - `a_point(registry_id, lat, lon, zip="75238")`
  - `two_neighbourhoods()`: 10 points around 75238 (Lake Highlands, ~32.88, −96.71) and 10 around Olympic Dr (~32.97, −96.89, the 75220/75247 area). This is the E4 shape.
- **Tests** (class `TestClusterPoints`):
  - empty → `()`.
  - 20 points in two neighbourhoods → exactly 2 clusters of 10, each containing only one neighbourhood's ids.
  - **Input-order independence:** shuffle with a fixed `random.Random(7)` five times, and every result is equal.
  - **Run-twice determinism:** the same input twice gives equal tuples.
  - **Caps:** for n in 1..60 and max_doors in (1, 10, 12), every cluster ≤ max_doors, `k == ceil(n/max_doors)`, and the members partition the input.
  - 23 points with max 12 → sizes {12, 11}.
  - Labels are A then B, west to east. The anchor is the modal ZIP, and a tie takes the lowest ZIP.
  - Label 27 → `AA`.
  - All points at one coordinate → still split into capped groups by registry id.
  - A duplicate candidate id raises `ValueError`, and `max_doors=0` raises `ValueError`.
- **PATTERN**: `tests/sourcing/test_stages.py` (plain pytest classes, no DB).
- **VALIDATE**: `uv run pytest tests/routing/test_clustering.py -q`
- **SATISFIES**: AC #2, #3, #4

### 5. CREATE `app/routing/exceptions.py`
- **IMPLEMENT**:
  - `RoutingError(LocalProspectEngineError)`.
  - `GeocoderUnavailableError(RoutingError)`: transport error or 5xx/429 after retries. Context: `status: int | None`.
  - `GeocoderResponseShapeError(RoutingError)`: a 200 whose body does not parse.
- **PATTERN**: `app/sourcing/exceptions.py`.
- **VALIDATE**: `uv run mypy app/routing`
- **SATISFIES**: AC #6

### 6. CREATE `app/routing/geocoder.py`
- **IMPLEMENT**: `class CensusGeocoder`:
  - **Constructor:** `__init__(self, *, transport: httpx.AsyncBaseTransport | None = None, base_url: str = BASE_URL)`. It owns an `httpx.AsyncClient(timeout=10.0)`. Add `aclose()`, `__aenter__` and `__aexit__`.
  - **`geocode`:** `async def geocode(self, address: PostalAddress) -> GeocodeOutcome`.
    - Params: `street`, `city`, `state`, `zip` (from `postal_code`), `benchmark=Public_AR_Current`, `format=json`.
    - Retry up to `_MAX_ATTEMPTS = 3` on `httpx.TransportError`, 429 or 5xx, with exponential backoff and jitter, mirroring `_backoff_seconds`.
    - Make the sleep injectable (`sleep: Callable[[float], Awaitable[None]] = asyncio.sleep`) so tests don't wait.
    - A 4xx other than 429 raises `GeocoderUnavailableError` immediately.
  - **Parsing:** use `CensusResponse.model_validate(response.json())`. A `ValidationError` or JSON error raises `GeocoderResponseShapeError`.
  - **Mapping:**
    - 0 matches → `unmatched`.
    - More than 1 → `ambiguous`.
    - Exactly 1 → `matched`, with `GeoPoint(latitude=match.coordinates.y, longitude=match.coordinates.x)` cited to `str(response.request.url)`, `web_lookup`, now (UTC).
  - **Constants:** `BASE_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"`, with a docstring saying it is free, public domain and needs no key.
- **PATTERN**: `app/promotion/client.py:182-186, 219-400` (scaled down).
- **GOTCHA**:
  - **x = lon, y = lat.**
  - Don't send `zip` if `postal_code` is blank. It can't be, because `PostalAddress` has `min_length=1`, so no branch is needed.
  - Don't log the street.
  - D13: this module must never import or reference a Places host. The existing `_no_places_content` guard covers `CandidateFields` only, so add a test that asserts the geocoder's citation host is `geocoding.geo.census.gov`.
- **VALIDATE**: `uv run pytest tests/routing/test_geocoder.py -q && uv run mypy app/routing && uv run pyright app/routing`
- **SATISFIES**: AC #5, #6

### 7. CREATE `tests/routing/fixtures/` and `tests/routing/test_geocoder.py`
- **IMPLEMENT** fixtures (synthetic but schema-accurate, built from the live shape above):
  - `census_match.json`: one Dallas match, e.g. `x: -96.7164, y: 32.8839`, `75238`.
  - `census_no_match.json`: `addressMatches: []`.
  - `census_ambiguous.json`: two matches.
  - `census_bad_shape.json`: `{"result": {}}`.
- **Tests:**
  - match → `matched`, latitude 32.8839 and longitude −96.7164 (pins x/y). The citation is `web_lookup` and its host is `geocoding.geo.census.gov`.
  - The request carries `benchmark=Public_AR_Current`, `format=json`, and the street, city, state and zip from the `PostalAddress`.
  - no match → `unmatched`, point None. Ambiguous → `ambiguous`, point None.
  - 503, 503, 200 → matched after 3 attempts, with the injected sleep called twice.
  - 503 ×3 → `GeocoderUnavailableError`. 400 → `GeocoderUnavailableError` after one attempt.
  - Transport error ×3 → `GeocoderUnavailableError`.
  - Bad shape or non-JSON → `GeocoderResponseShapeError`.
  - **An unrouted request fails the test** (mock transport raises).
- **PATTERN**: `tests/promotion/conftest.py` (`load_fixture`, `json_responder`, a raising default route).
- **VALIDATE**: `uv run pytest tests/routing/test_geocoder.py -q`
- **SATISFIES**: AC #5, #6, #8

### 8. CREATE `app/routing/models.py`
- **IMPLEMENT**: `class RouteAssignment(Base)`, `__tablename__ = "route_assignment"`:
  - `id` UUID PK, `default=uuid4`.
  - `run_id` UUID FK → `SourcingRun.id`, not null.
  - `candidate_id` UUID FK → `Candidate.id`, not null.
  - `cluster_index` `SmallInteger`, not null.
  - `cluster_label` `String(32)`, not null.
  - `point` JSONB, not null: a dumped `ProvenancedValue[GeoPoint]`.
  - `created_at` timestamptz, `server_default=func.now()`.
  - `__table_args__`:
    - `UniqueConstraint("candidate_id", name="uq_route_assignment_candidate_id")`
    - `CheckConstraint("cluster_index >= 0", name="ck_route_assignment_cluster_index_non_negative")`
    - `Index("ix_route_assignment_run_id", "run_id")`
- **PATTERN**: `app/sourcing/models.py` (a docstring per column and a module docstring about constraints living in both places).
- **GOTCHA**:
  - Import `Candidate` and `SourcingRun` from `app.sourcing.models`. Reading another slice's model for an FK is the precedent `sourcing` set with `VerticalManifest`.
  - The model must be imported where `alembic/env.py` collects metadata. Check how `app.sourcing.models` and `app.cadence.models` are registered (e.g. an import list in `env.py` or `app/core/database.py`) and add `app.routing.models` the same way, or `alembic check` reports drift.
- **VALIDATE**: `uv run mypy app/routing && uv run pyright app/routing`
- **SATISFIES**: AC #7

### 9. CREATE `alembic/versions/0007_routing.py`
- **IMPLEMENT**: hand-written. `revision = "0007_routing"`, `down_revision = "0005_cadence"`. That is the head at fork time. **Re-chain to T5's `0006_…` at merge** (merge order T5 → T8 → T7; `test_migrations_have_exactly_one_head` will force it). `upgrade` creates the table, constraints and index exactly as the model declares them. `downgrade` drops the index and table.
- **PATTERN**: `alembic/versions/0004_sourcing.py`.
- **GOTCHA**: no RLS or policy statements (structure guard). Constraint names must match the model's exactly.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q`, then with the DB up: `uv run alembic upgrade head && uv run alembic check` (via `/piv-validate`'s drift step).
- **SATISFIES**: AC #7

### 10. CREATE `app/routing/repository.py`
- **IMPLEMENT**: `class RoutingRepository(session)`:
  - `async def replace_run_assignments(self, run_id: UUID, clusters: Sequence[RouteCluster], points: Mapping[UUID, ProvenancedValue[GeoPoint]]) -> list[RouteAssignment]`:
    - `DELETE FROM route_assignment WHERE run_id = :run_id`, then insert one row per member, then flush.
    - Log `routing.repository.assignments_replaced`.
  - `async def list_run_assignments(self, run_id: UUID) -> Sequence[RouteAssignment]`: ordered by `cluster_index`, then `candidate_id`'s registry id via a join on `Candidate`, so the order is stable.
- **PATTERN**: `app/sourcing/repository.py` (flush, never commit).
- **GOTCHA**: dump `point` with `model_dump(mode="json")` so `retrieved_at` round-trips. Use `populate_existing` on reads after a write in the same session (see the `upsert_candidate` docstring).
- **VALIDATE**: `uv run pytest tests/routing/test_repository.py -q` (DB tier)
- **SATISFIES**: AC #4, #7

### 11. CREATE `app/routing/service.py`
- **IMPLEMENT**: `class RoutingService`:
  - **Constructor:** `__init__(self, session, *, geocoder: CensusGeocoder | None = None, max_doors: int | None = None)`. Defaults: a fresh `CensusGeocoder()` (closed at the end if the service created it) and `get_settings().route_max_doors`.
  - **`cluster_run`:** `async def cluster_run(self, run_id: UUID) -> RoutingResult`:
    1. `candidates = await SourcingService(self._session).list_candidates(run_id)`. This raises `SourcingRunNotFoundError` for an unknown run, which is the right error. Do not require `running`: routing writes its own table, not `candidate`.
    2. Split on `c.fields.has_verified_address()`. The rest count as `routing_unverified_address`.
    3. Geocode each eligible candidate **sequentially**, in registry-id order (politeness, determinism; n ≤ ~150).
       - `GeocoderUnavailableError` / `GeocoderResponseShapeError` on one candidate → warn (`routing.service.geocode_failed`, registry_id), count `routing_geocode_failed`, and continue. A re-run of the stage retries them.
       - If **every** eligible geocode failed with `GeocoderUnavailableError`, re-raise. The service is down, and the runner should see a failure, not an empty "success".
    4. Count `routing_geocode_unmatched` and `routing_geocode_ambiguous`.
    5. Build `RoutePoint`s, call `cluster_points(points, max_doors=…)`, then `replace_run_assignments`, then `commit`.
    6. Counts:
       - `routing_eligible`, `routing_unverified_address`, `routing_geocode_unmatched`, `routing_geocode_ambiguous` and `routing_geocode_failed`.
       - `routing_clustered`: the number of rows written.
       - `routing_clusters`: k.
       - Every key is always present, including zeros, so the stored shape is stable.
    7. Log `routing.service.run_clustered`.
- **PATTERN**: `app/sourcing/service.py` (the service commits, `_require_*` style, structured logs).
- **GOTCHA**:
  - Do **not** write to `candidate` (no `record_candidates` call). The point is routing state.
  - Counts keys must match `SLUG_PATTERN` `^[a-z][a-z0-9_]*$`.
  - Zero eligible candidates is a valid outcome: delete the run's rows, return zero clusters, and don't call the geocoder.
- **VALIDATE**: `uv run pytest tests/routing/test_service.py -q` (DB tier)
- **SATISFIES**: AC #1, #2, #4, #5, #6

### 12. CREATE `app/tools/cluster_routes.py`
- **IMPLEMENT**: the stage module exactly as in *Patterns → Stage module shape*. The module docstring says it is stage 5, deterministic, has no LLM, and owns no candidate field.
- **GOTCHA**: `STAGE.stage` must be the `PipelineStage.cluster_routes` member, not the string. The registry rejects a string (PR #17 L1). The module name must equal the enum value.
- **VALIDATE**: `uv run python -c "from app.tools.registry import registered_stages; print([s.stage for s in registered_stages()])"` should include `cluster_routes`.
- **SATISFIES**: AC #1

### 13. CREATE `tests/routing/test_repository.py`, `tests/routing/test_service.py`, `tests/tools/test_cluster_routes.py`
- **IMPLEMENT** (DB tests marked `@requires_db`, using `db_session`, `an_active_manifest`, `a_brief`, `SourcingService.start_run`, and `record_candidates(..., stage=PipelineStage.search_registry)` plus `place_checked()` writes with `stage=PipelineStage.verify_business` to make addresses verified):
  - **Repository:**
    - The point round-trips with its provenance.
    - `replace_run_assignments` twice leaves one set of rows, not two.
    - A second candidate assignment violates `uq_route_assignment_candidate_id`.
    - A negative index is refused by the check.
  - **Service:**
    - 20 verified candidates in two neighbourhoods (mock geocoder keyed by street, from fixtures) → 2 clusters of 10, and counts are correct.
    - A candidate with an address but no `business_check`, and one with neither → excluded, `routing_unverified_address == 2`, and **the geocoder is never called for them**. Assert the request log.
    - Unmatched and ambiguous candidates are excluded and counted.
    - One 503 ×3 among 20 → 19 clustered, `routing_geocode_failed == 1`. All eligible failing → `GeocoderUnavailableError`.
    - **Stable across runs:** two `cluster_run` calls on the same run give identical rows (labels, indexes, membership).
    - **Same input set in two separate runs** (the same candidate fields recorded into run A and run B) → identical labels and membership by registry id.
    - Zero eligible → no rows, no geocoder calls, `routing_clusters == 0`.
    - Unknown run → `SourcingRunNotFoundError`.
  - **Stage** (`tests/tools/test_cluster_routes.py`):
    - `STAGE.stage is PipelineStage.cluster_routes`.
    - `registered_stages()` on the real package contains it exactly once.
    - With the DB, `STAGE.run(StageContext(...))` returns the service's counts. Inject the geocoder by monkeypatching `app.routing.service.CensusGeocoder` to a factory over a mock transport, so the registry contract stays untouched.
- **PATTERN**: `tests/sourcing/test_service.py` and `tests/sourcing/builders.py`.
- **GOTCHA**: build addresses with distinct streets, so the mock transport can route by the `street` query param.
- **VALIDATE**: `TEST_DATABASE_URL=… uv run pytest tests/routing tests/tools -q`
- **SATISFIES**: AC #1–#8

### 14. CREATE `app/routing/README.md` and UPDATE docs
- **IMPLEMENT**:
  - The slice README covers: what it does, the eligibility rule, why Census and not Places (D13), the algorithm and its guarantees, the counts it reports, and **the follow-up**: "T8 clusters every verified address. T9/T10 must restrict clusters to qualified candidates (and decide whether to re-cluster after disqualification)."
  - Update `CLAUDE.md`'s architecture map: add `app/routing/` and `app/tools/cluster_routes.py` to *Today*, add `0007 routing` to the alembic line, and remove `routing/` from *Planned*.
  - In `docs/tickets/local-prospect-engine.md` T8, add a short "decided 2026-10-10" note covering eligibility (verified-only, follow-up to T9/T10) and storage (`route_assignment`).
  - Append a T8 section to `_tasks/todo.md` (a cumulative log; never overwrite).
- **VALIDATE**: `uv run pytest tests/test_structure.py -q`
- **SATISFIES**: AC #9

### 15. RUN full validation
- **VALIDATE**: `/piv-validate` (ruff check and format, mypy, pyright, pytest with the DB tier, alembic drift).
- **SATISFIES**: AC #10

---

## TESTING STRATEGY

### Unit Tests
- `test_clustering.py`: the pure algorithm, exhaustive over sizes and caps, order-independence, ties and labels. No DB, no network. This is where determinism is proven.
- `test_geocoder.py`: the mock transport only. Pins the request parameters, the x/y mapping, match/unmatched/ambiguous, retries and errors.

### Integration Tests (DB tier, `@requires_db`)
- Repository round-trip and constraints. Service end to end over real `candidate` rows with a mocked geocoder. The stage via `StageContext`.
- Run against the T8 worktree's throwaway Postgres:
  `docker run --rm -d --name lpe-test-pg-t8 -p 5436:5432 -e POSTGRES_PASSWORD=test postgres:16`
  `export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5436/postgres`

### Edge Cases
- 0 eligible candidates. 1 candidate (1 cluster, label `A · zip`). Exactly `max_doors` (1 cluster). `max_doors + 1` (2 clusters).
- All candidates at one coordinate. ZIP+4 postal codes (anchor uses the first 5). A tie for the modal ZIP.
- Unverified address (no `business_check`). No address at all.
- Census returns 0 or ≥2 matches. 5xx then success. Persistent 5xx on one candidate. The service down for all.
- A re-run of the stage on the same run (replace, not append). Input arriving in shuffled order.
- More than 26 clusters (label `AA`).

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style
```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy . && uv run pyright
```

### Level 2: Unit Tests
```bash
uv run pytest tests/routing/test_clustering.py tests/routing/test_geocoder.py tests/tools -q
```

### Level 3: Integration Tests
```bash
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5436/postgres
uv run pytest -q
uv run alembic upgrade head && uv run alembic check   # against the throwaway DB
```

### Level 4: Manual Validation
A single live geocode against the real Census endpoint (free, no key; read-only, touches nothing of ours):
```bash
uv run python -c "
import asyncio
from app.routing.geocoder import CensusGeocoder
from app.sourcing.schemas import PostalAddress
async def main():
    async with CensusGeocoder() as g:
        print(await g.geocode(PostalAddress(street='1500 Marilla St', city='Dallas', state='TX', postal_code='75201')))
asyncio.run(main())"
```
Expect `status=matched` with latitude ≈ 32.77 and longitude ≈ −96.80. If it is ambiguous or unmatched, try another public address. The point is that x→longitude holds against live data.

### Level 5: Additional Validation
- `/piv-validate`: the project's Done bar in one PASS/FAIL.
- `mcp__codebase-search__find_references cluster_routes`: confirm nothing outside `app/routing/` and `app/tools/cluster_routes.py` imports routing internals.

---

## ACCEPTANCE CRITERIA

1. [ ] `app/tools/cluster_routes.py` exports `STAGE` for `PipelineStage.cluster_routes`, and `registered_stages()` returns it. It uses no LLM or Agent SDK import (`test_agent_sdk_is_imported_only_at_its_decided_call_sites` stays green).
2. [ ] 20 verified candidates in two neighbourhoods come out as **2 clusters of 10**, each geographically coherent (E4).
3. [ ] Route size ≤ `route_max_doors` (default 12). Route count = ⌈n / max_doors⌉. Sizes are balanced.
4. [ ] **Stable assignment:** the same input set gives identical clusters, labels and membership across repeated runs and in any input order.
5. [ ] Coordinates come **only** from the US Census Geocoder, cited `web_lookup` to a `geocoding.geo.census.gov` URL. Nothing references Places (D13).
6. [ ] A candidate without `has_verified_address()` is excluded and never geocoded. Unmatched, ambiguous and failed geocodes are excluded and counted, never guessed.
7. [ ] `route_assignment` table via `0007_routing`. Constraints exist in both the model and the migration, and `alembic check` is clean.
8. [ ] Recorded fixtures only. Unrouted HTTP fails the test.
9. [ ] Slice README, the CLAUDE.md architecture map, the ticket note and the `_tasks/todo.md` entry are updated. The T9/T10 qualification-filter follow-up is written down.
10. [ ] ruff, mypy, pyright and pytest (including the DB tier) are all green, with zero suppressions.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully
- [ ] Full test suite passes (unit + integration)
- [ ] No linting or type checking errors
- [ ] Manual testing confirms feature works
- [ ] Acceptance criteria all met
- [ ] Code reviewed for quality and maintainability

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decided with the user (2026-10-10):**
- **Eligibility** is verified address only. Leaving out disqualified candidates is a T9/T10 follow-up.
- **Storage** is routing's own `route_assignment` table. `CandidateFields` and `stages.py` are untouched.

**Assumptions** (raise them if wrong):
1. **The cap is per route, with the count derived.** The ticket's "20 names / 2 clusters per week" falls out of ⌈20/12⌉ = 2. With an unfiltered batch (see the next point) a run yields more routes. That is honest until T9/T10 filter.
2. **Because T8 runs before qualification is filtered, a real batch of 150 gives ~13 routes**, not 2, until T10 wires order and filtering. T10 should decide whether routing re-runs after T7/T9 narrow the set (the stage is idempotent and replaces its rows, so a re-run is cheap).
3. **Labels use a ZIP anchor, not a street** ("A · 75238", not "Olympic Drive"). A modal street name is noisier (suite and number formats vary). A ZIP is stable and matches the hand list's "Cluster B: 75238" form.
4. **`web_lookup`** is the right `RetrievalMethod` for a public per-request API. No new enum member is added.
5. **Geocode failure on one candidate degrades rather than failing the stage**, and failure on all of them raises. If the user prefers fail-fast, change step 11.3 only.
6. **Sequential geocoding** at ≤150 requests a run is acceptable (~1–2 minutes worst case). Census publishes no rate limit, so don't parallelise against it.

**Not answered here** (from the architecture's open list): none touched by T8.

## NOTES (open canvas)

**Why recursive median bisection, not k-means or ZIP grouping.**

| Option | Deterministic | Respects cap | Compact | Deps | Verdict |
|---|---|---|---|---|---|
| k-means (sklearn, fixed seed) | only with a seed, and fragile to input order | no (needs constrained variant) | yes | sklearn+numpy | no |
| Group by ZIP | yes | no (one ZIP may hold 30) | roughly | none | no |
| Balanced median bisection (k-d split) | yes, given sorted ties | **yes, by construction** | good for n ≤ ~150 | none | **chosen** |
| Sweep by angle from the centroid | yes | yes | poor (long slivers) | none | no |

Bisection always splits the wider axis, so routes come out roughly square rather than as slivers. With integer
`n_left = ceil(n·k_left/k)` no group ever exceeds the cap. The proof is two lines and sits in the docstring. It
is the kind of "boring on purpose" that the architecture's *Scheduling* section asks for.

**Why the one-line endpoint, not `addressbatch`.** The batch endpoint takes a multipart CSV upload and returns
CSV with `"lon,lat"` packed into one quoted column, plus Match, No_Match and Tie flags. At ≤150 addresses it saves
~150 small requests but costs a CSV parser and a coarser failure mode (one bad response loses the batch). JSON
per address parses straight into Pydantic, retries per candidate, and is trivially fixture-able. Revisit if
batch sizes grow past ~1,000.

**Why not cache geocodes across runs.** Batches are disjoint (D12), so a candidate is routed once. A cache is
YAGNI.

**Projection accuracy.** An equirectangular projection around the data's mean latitude is accurate to <1% over
a metro (DFW is ~100 km across). The algorithm only compares relative spread and order, so that is ample. No
haversine is needed.

**Data flow.**
```
candidate (run_id) ──list_candidates──▶ has_verified_address? ──no──▶ count: unverified
                                              │yes
                                              ▼
                              CensusGeocoder.geocode(census address)
                         matched │ unmatched/ambiguous/failed ──▶ count
                                 ▼
                     cluster_points(points, max_doors=12)
                                 ▼
             route_assignment (replace run's rows) ──▶ StageResult.counts
```

**Merge notes.** `0007_routing.down_revision` → T5's `0006_*` after T5 merges. T8 touches no file in
`app/sourcing/`, so it should merge cleanly beside T5 and T7. The only shared edits are `CLAUDE.md`, the ticket
doc and `_tasks/todo.md`, which are prose and easy to resolve.

## AMENDMENTS

- 2026-10-10 — Implemented in the `t8` worktree. The test Postgres moved to **5436**, because T7 holds 5435.
  `_Misses` is a dataclass, and the service uses no `assert`. The geocoder also counts an unreadable body as
  `routing_geocode_failed`. Follow-up after T5 merges: set T5's new `StageResult.degraded_reason` when any geocode
  failed.
