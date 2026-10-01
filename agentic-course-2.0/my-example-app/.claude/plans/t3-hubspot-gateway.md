# Feature: T3 — HubSpot gateway: client, custom properties, dedupe

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. Import from the right files — in
particular `ProvenancedValue` / `is_promotable` come from `app.shared.provenance`, and every exception
derives from `app.core.exceptions.LocalProspectEngineError`.

## Feature Description

The one place this service is allowed to talk to HubSpot. T3 builds the gateway — an async `httpx` client
carrying the private app token, self-throttling so it cannot trip the account's rate limit, logging every
call under `promotion.hubspot.*` — and the two things that have to be true *before* any record is ever
created:

1. **The five custom properties exist**, provisioned idempotently, so a human reviewing a record in HubSpot
   can see where it came from (source URL · sourced-at · vertical · priority score · route cluster).
   Verified against the live portal on 2026-09-27: none of them exist today.
2. **A duplicate cannot be created.** The portal already holds 3,118 contacts and 1,040 companies of mixed
   provenance (E14/E18). Dedupe on domain *and* phone runs before any create; an existing match is returned,
   not duplicated.

And the invariant the whole system rests on gets its enforcement point here: **`is_promotable()` is checked
at the client boundary for prospect field writes**, so no later slice can route around the gate by
assembling a raw dict. Task creation is deliberately *not* gated — T13 adopts 22 hand-typed records that
carry no citable fields at all, and a gate that refused them would refuse the ticket that exists to rescue
them.

T3 creates no records of its own. It is the gateway the promotion slice (T9) and the cadence slice (T11)
both call.

## User Story

As the founder running the weekly prospect engine
I want every HubSpot write to go through one client that cannot create a duplicate and cannot write a field
nobody can cite
So that the CRM I actually work out of stays trustworthy, instead of accumulating another 34 contacts where
14 have a company name in the first-name field.

## Problem Statement

Three concrete failures, all measured, all landing at the same boundary:

- **E18 — enrichment quality is poor at the point of list creation.** Of 34 fire-vertical contacts created
  09-22 to 09-25: 14 carry the company name in the first-name field, 11 reach owner/principal level, 0 of 23
  companies have a headcount, 7 of 23 have city/state. Every one of those is a field nobody could cite.
- **E14 — the portal is already full of mixed-provenance records.** 3,118 contacts, 1,040 companies. An
  engine that creates before it checks turns a weekly run into a weekly duplicate-generation job.
- **Nothing in the portal says where a record came from.** Confirmed live: no source-URL, sourced-at,
  vertical, priority-score or route-cluster property exists on COMPANY. Review happens in HubSpot (there is
  no frontend), so a record that cannot show its provenance cannot be reviewed.

Without T3, T9 and T11 each grow their own HubSpot access, each with its own idea of the rules — and the
write-gate becomes a convention instead of a boundary.

## Solution Statement

One slice, `app/promotion/`, holding four modules and nothing else:

- **`client.py`** — `HubSpotClient`, an async `httpx` wrapper. Bearer auth from `Settings`, a single
  `_request` funnel that logs `promotion.hubspot.*`, self-throttles from the rate-limit response headers,
  retries with bounded backoff, maps HubSpot's two different error envelopes onto typed exceptions, and
  exposes exactly the verbs the MVP needs. **Prospect field writes accept `ProvenancedValue`s, not
  strings** — the gate is in the signature, and the runtime check refuses a `None` before anything leaves
  the process. `create_task` takes plain values and is documented as deliberately ungated.
- **`properties.py`** — the property group and the five property definitions as data, plus
  `ensure_properties()`: read-then-create, so a second run issues zero writes and logs `property_present`
  five times.
- **`dedupe.py`** — `find_existing_company` / `find_existing_contact` over the CRM search API, on domain and
  on phone, with the country code stripped because HubSpot's phone search matches on area code plus local
  number only.
- **`exceptions.py`** — `HubSpotError` and friends, all deriving from `LocalProspectEngineError` so
  `app.main`'s existing handlers render them without knowing the slice.

Everything is tested against recorded JSON fixtures served through `httpx.MockTransport`. No test touches
the portal.

## Out of Scope / Non-Goals

- **Not the promotion slice.** No `service.py`, `gate.py`, `models.py`, `repository.py`, `routes.py`, no
  `promotion` table, no migration, no `pending review` stage logic. That is **T9**, which depends on T3, T7
  and T8. T3 ships a gateway with no caller.
- **Not the cadence state machine.** `create_task` exists so T11 has its primitive and so this ticket can
  prove tasks are ungated. Scheduling, the three-touch walk, parking and **outcome sync are T11's** — and so
  is reading activities, because deciding "was this touch done" is cadence policy, not transport. T11 adds
  the read methods to this client; it does not need a second client. (See NOTES for what the research
  turned up about how that read has to work — it is a two-step association walk, not one endpoint, and T11's
  planner should know that before estimating.)
- **Not deals.** The `$999 assessment` pipeline does not exist and we have decided not to create it
  (GATE-A / D13). The token is scoped to read deals so T10's Friday report can, but T3 writes no deal code.
- **Not sending anything.** Nothing sends from any domain until Spike 4. No email, no sequences — the tier
  has none anyway.
- **Not batch endpoints.** HubSpot offers batch create/read for properties and objects. At ~20 promoted
  prospects a week, one call per object is well inside the limit and far easier to reason about. Revisit if
  a run ever approaches the throttle.
- **Not changing `is_promotable()` or `ProvenancedValue`.** T1 shipped them and PR #2 closed an immutability
  hole in them. If T3 appears to need a change there, stop and flag it.
- **Not populating property values.** T3 provisions the properties; the values come from the sourcing line.
- **Not a live-portal test.** Fixtures only, in CI and locally.

## Feature Metadata

**Feature Type**: New Capability (the first feature slice; T1 was infrastructure)
**Estimated Complexity**: Medium — no conceptual difficulty, but three sharp edges: strict-mode typing over
an untyped JSON API, retry semantics that must not double-create, and a dedupe path whose failure mode is
silent.
**Primary Systems Affected**: `app/promotion/` (new) · `app/main.py` (one lifespan line) · `pyproject.toml`
(httpx moves to runtime) · `tests/promotion/` (new)
**Dependencies**: `httpx` (already locked at 0.28.1, currently dev-only) · **no new dependency**

## Related Work

**Implements**: T3 in [`docs/tickets/local-prospect-engine.md`](../../docs/tickets/local-prospect-engine.md)
**Epic**: [`docs/local-prospect-engine.architecture.md`](../../docs/local-prospect-engine.architecture.md)
(the how) + [`docs/local-prospect-engine.prd.md`](../../docs/local-prospect-engine.prd.md) (intent)

**Back-references** (decisions inherited — do **not** reopen):

- `.claude/plans/t1-scaffold-core-provenance.md` — the core patterns this mirrors: lazy singleton +
  lifespan disposal (`core/database.py`), the exception hierarchy, `domain.component.action_state` logging,
  `Settings.model_validate({})`, zero suppressions.
- **D4** — we own the cadence schedule, HubSpot owns outcomes. **D5** — a touch is done when its Task is
  complete *or* a matching activity was logged after task creation. Both constrain T11, not T3, but they are
  why `create_task` lives here and the activity read does not.
- **D13 / GATE-A** — the `$999` pipeline stays uncreated.
- **Spike 3, answered** — portal 244766495, seats `core` · `sales-starter` · `service-starter` ·
  `view-only`. No professional seat, therefore **no sequences**, and Starter workflows are capped around 10
  actions with no branching. Re-confirmed live 2026-09-27.

**Forward-references** (plans that extend this — append as follow-ups get created):

- (none yet — T9 and T11 will both land here)

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `app/shared/provenance.py` (whole file, 168 lines) — Why: `ProvenancedValue[T]`, `RetrievalMethod` and
  `is_promotable()` are the types this ticket enforces. Note `_assert_immutable` — a `ProvenancedValue`
  cannot hold a list or dict, which constrains what a property value may be.
- `app/core/exceptions.py` (whole file, 52 lines) — Why: the base class every new exception derives from,
  and the `default_code` / `status_code` ClassVar pattern to copy. `CostLimitExceededError` shows how to add
  structured keyword-only attributes to an exception.
- `app/core/config.py` (lines 36-57) — Why: `hubspot_private_app_token: str | None = None` already exists;
  `get_settings()` is `lru_cache`d and uses `model_validate({})` for a reason spelled out in its docstring.
- `app/core/database.py` (lines 30-91) — Why: **the pattern to mirror for the client singleton.** Lazy
  module-level global, `get_engine()` builds on first use, `is_initialised()` exists so tests can assert
  isolation, `dispose_engine()` is called from the lifespan. Do the same for the HTTP client.
- `app/core/logging.py` (lines 1-45) — Why: `get_logger(name) -> FilteringBoundLogger` and the event-name
  convention. Never `logging.getLogger`.
- `app/core/middleware.py` (lines 28-53) — Why: the started / succeeded / failed logging triple with
  `duration_seconds=round(..., 3)` — mirror it in the request funnel.
- `app/main.py` (lines 40-53) — Why: the lifespan you will add one `await` to, next to `dispose_engine()`.
- `tests/conftest.py` (whole file, 82 lines) — Why: the autouse `_clear_settings_cache` fixture is what lets
  a test monkeypatch `HUBSPOT_PRIVATE_APP_TOKEN`; `_reset_database_state` is the shape to copy for resetting
  the HTTP client singleton; and note that the existing `client` / `error_client` fixtures **inject an
  `ASGITransport`** — which is why this plan mocks by injecting a transport rather than patching httpx
  globally.
- `tests/core/test_cost.py` (whole file, 101 lines) — Why: the test style — plain classes grouping
  behaviour, one assertion idea per test, a docstring only where the *why* is not obvious.
- `tests/core/test_health.py` (lines 28-40) — Why: the `patch(..., side_effect=AssertionError(...))` trick
  for proving something is *not* called. Use it to prove dedupe runs before create.
- `tests/test_structure.py` (lines 78-118, 137-161) — Why: the two guards that will fail on sloppy code — no
  `# type: ignore` / `# pyright: ignore` anywhere in `app/`, no `Any` in any annotation (AST-checked, so
  aliasing it does not help), and no empty package under `app/`.
- `.claude/references/hubspot-integration.md` (whole file, 47 lines) — Why: the per-ticket context the
  ticket names. The split store, the write-gate, the adoption carve-out.
- `.claude/references/conventions.md` (whole file, 61 lines) — Why: commit/PR format, and the `## merge`
  rule that matters here: **`uv.lock` is never hand-merged** — this ticket regenerates it.
- `docs/tickets/local-prospect-engine.md` (lines 96-112 for T3; 194-226 for T9 and T11) — Why: T3's
  acceptance criteria, and what its two consumers will expect from it.

### New Files to Create

- `app/promotion/__init__.py` — slice docstring + the public surface
- `app/promotion/exceptions.py` — `HubSpotError` hierarchy + `UnprovenancedWriteError`
- `app/promotion/schemas.py` — typed models for every HubSpot payload we send or parse
- `app/promotion/client.py` — `HubSpotClient`, the request funnel, throttle, retry, the boundary gate
- `app/promotion/properties.py` — the group, the five definitions, idempotent `ensure_properties()`
- `app/promotion/dedupe.py` — `find_existing_company` / `find_existing_contact`
- `app/promotion/README.md` — slice documentation
- `tests/promotion/__init__.py`
- `tests/promotion/conftest.py` — token fixture, `MockTransport` factory, fixture loader, singleton reset
- `tests/promotion/test_client.py` · `test_gate.py` · `test_properties.py` · `test_dedupe.py`
- `tests/promotion/fixtures/*.json` — recorded response bodies (scrubbed)

### Files to Update

- `pyproject.toml` — move `httpx` from `[dependency-groups].dev` into `[project].dependencies`. Then
  `uv lock`.
- `app/main.py` — one line in `lifespan`, after `await dispose_engine()`.
- `.env.example` — uncomment `HUBSPOT_PRIVATE_APP_TOKEN` and list the exact scopes (below).

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

Every fact in the next section was pulled from these pages on 2026-09-27. Re-check anything marked
**unconfirmed**.

- [API versioning](https://developers.hubspot.com/docs/developer-tooling/platform/versioning#api-versioning)
  — **Read this first.** HubSpot moved from `v1`–`v4` to **date-based versions** (`/2026-09/`) on
  2026-03-30. Legacy `v3`/`v4` URLs still work but are the frozen surface. A dated version is *Current* for
  6 months, *Supported* for 12 more, unsupported at 18.
- [API usage guidelines and limits](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines#rate-limits)
  — the Starter numbers, the `X-HubSpot-RateLimit-*` headers, and the 429 body shape.
- [CRM Search API](https://developers.hubspot.com/docs/api-reference/latest/crm/search-the-crm#limits)
  — `filterGroups` shape, the operator list, the **5 req/s** search limit, the phone-standardization rule,
  the filter and pagination caps.
- [Properties API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/properties/guide#property-type-and-fieldtype-values)
  — required body fields and the full `type` / `fieldType` table.
- [Tasks API guide](https://developers.hubspot.com/docs/api-reference/latest/crm/activities/tasks/guide)
  — task properties and their enums, plus the scope quirk.
- [Associate records guide](https://developers.hubspot.com/docs/api-reference/latest/crm/associations/associate-records/guide#association-type-id-values)
  — the default association type IDs.
- [Error handling](https://developers.hubspot.com/docs/api-reference/error-handling) — the standard error
  envelope and the full non-2xx status list.
- [Legacy private apps overview](https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview)
  — the Bearer header. Note the rename: what we call a private app is now "legacy private app", still fully
  supported for a single internal account.
- [httpx — Transports](https://www.python-httpx.org/advanced/transports/) and
  [Timeouts](https://www.python-httpx.org/advanced/timeouts/) — the test seam and the timeout object.

### API facts this plan depends on (researched 2026-09-27)

**Auth.** `https://api.hubapi.com`, header `Authorization: Bearer <token>`.

**Scopes the private app token needs** — put these verbatim in `.env.example`:
`crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.companies.read`,
`crm.objects.companies.write`, `crm.objects.deals.read`, `crm.schemas.companies.write`,
`crm.schemas.contacts.write`.
**Quirk, confirmed twice in the docs:** the Tasks API's required-scopes list is
`crm.objects.contacts.read/write` — there is **no** `crm.objects.tasks.*` scope. Engagement objects are
gated behind the contacts scope. Do not go looking for a tasks scope; it does not exist.

**Rate limits (Sales Hub Starter, private app):** **100 requests / 10 seconds** per app, **250,000 / day**
per account. Headers on non-search responses: `X-HubSpot-RateLimit-Max`, `-Remaining`,
`-Interval-Milliseconds`, `X-HubSpot-RateLimit-Daily`, `-Daily-Remaining`.
**`Retry-After` is NOT part of the 429 contract** — it appears only on `477 Migration in Progress`. The
documented guidance is *throttle to stay under the limit*, not *retry on rejection*. The 429 body is a
different shape from every other error: `{"status":"error","message":...,"errorType":"RATE_LIMIT",
"correlationId":...,"policyName":"DAILY"|"TEN_SECONDLY_ROLLING","requestId":...}` — note `errorType`, not
`category`.

**Search is separately and much more tightly limited: 5 requests per second per account**, and **search
responses carry no rate-limit headers at all**, so the only way to respect it is a hardcoded client-side
throttle. Search body caps: max 5 `filterGroups`, 6 filters per group, 18 filters total; `limit` max 200;
10,000 results total before a 400.

**Phone search, the finding that shapes dedupe:** HubSpot standardizes phone numbers into calculated
`hs_searchable_calculated_*` properties and **matches on area code and local number only** — the docs say
explicitly to leave the country code out of search criteria. So an `EQ` filter on `phone` does work across
formatting differences, provided we strip `+1`. `domain` and `phone` are both default-searchable.

**Properties:** required body fields are `groupName`, `name`, `label`, `type`, `fieldType`. It is
**`groupName` (a string), not `groupId`** — and the group must already exist or the create fails. Relevant
type/fieldType pairs: text → `string`/`text`; datetime → `datetime`/`date` (fieldType is `date`, not
`datetime`); number → `number`/`number`; dropdown → `enumeration`/`select`.
**Duplicate-create behaviour is unconfirmed** in the official reference — community reports say `409` with
`"Property named '{name}' already exists"`. Build idempotency as read-then-create and catch 409
defensively; do not branch on the error string.

**Tasks:** object type `tasks`. `hs_timestamp` (due date, epoch ms or ISO) is the only required property.
Others: `hs_task_subject`, `hs_task_body`, `hubspot_owner_id`, `hs_task_status`, `hs_task_priority`
(`LOW`/`MEDIUM`/`HIGH`), `hs_task_type` (`EMAIL`/`CALL`/`TODO`).
**`hs_task_status` is inconsistent in HubSpot's own docs** — the property table says `NOT_STARTED` /
`COMPLETED`, the example on the same page uses `WAITING`. Confirm with a live
`GET /crm/properties/{version}/tasks/hs_task_status` before hardcoding the enum.
Associations go inline in the create body; the default type IDs are **Task → Contact `204`** and
**Task → Company `192`**, category `HUBSPOT_DEFINED`.

**Errors:** the standard envelope is
`{"status","message","errors":[{"message","code","context"}],"category","correlationId"}` with
`category: "VALIDATION_ERROR"` and friends. Rate-limit errors use `errorType` + `policyName` instead of
`category`. **All fields are documented as optional and varying by endpoint**, so parse defensively. Other
statuses worth handling: `403` (missing scope — the message names it), `423 Locked` (wait ≥2s, retry),
`477` (migration, can last hours — do not sit in a retry loop), `502/503/504/52x` (transient).

**Client library — decided: raw `httpx`.** `hubspot-api-client` 12.0.0 is OpenAPI-generated, `requests`-based
(sync only), and **ships no `py.typed`** — under Pyright strict with zero suppressions that is a
non-starter. The newer Stainless-generated `hubspot-sdk` is httpx-based, async, and typed, but is at
`0.1.0a9` alpha on a 6-star repo created five months ago. A thin typed wrapper over the six endpoints we
need is less risk than either. Revisit if `hubspot-sdk` reaches a stable 1.0.

### Patterns to Follow

**Naming:** snake_case modules and functions, PascalCase classes. Slice-internal helpers take a leading
underscore. Property internal names are `lpe_`-prefixed (see the task list for why).

**Logging — `domain.component.action_state`, four segments of meaning in three dots:**

```python
logger.info("promotion.hubspot.request_started", method="POST", path=path)
logger.info("promotion.hubspot.request_succeeded", status_code=response.status_code, duration_seconds=...)
logger.warning("promotion.hubspot.rate_limited", attempt=attempt, policy=policy, remaining=remaining)
logger.error("promotion.hubspot.request_failed", status_code=..., category=..., correlation_id=...)
logger.info("promotion.hubspot.property_created", object_type="companies", name="lpe_source_url")
logger.info("promotion.hubspot.property_present", object_type="companies", name="lpe_source_url")
logger.info("promotion.hubspot.duplicate_found", object_type="companies", matched_on="domain", object_id=...)
logger.warning("promotion.hubspot.write_refused", fields=["headcount"])
```

**The token never appears in a log line, an exception message, or a fixture.** Log the path, the status, the
`correlationId` HubSpot hands back — never headers.

**Exceptions — mirror `app/core/exceptions.py:26-52`:**

```python
class HubSpotError(LocalProspectEngineError):
    """Base for every failure of the HubSpot gateway."""
    default_code: ClassVar[str] = "hubspot_error"
    status_code: ClassVar[int] = 502
```

Structured attributes go on the subclass (`CostLimitExceededError` is the model: keyword-only `kind`, `cap`,
`recorded`, stored on `self`).

**Singleton + lifespan disposal — mirror `app/core/database.py:30-91` exactly:**

```python
_client: HubSpotClient | None = None

def get_hubspot_client() -> HubSpotClient: ...   # builds on first use
def is_initialised() -> bool: ...                 # so tests can assert isolation
async def aclose_hubspot_client() -> None: ...    # called from the lifespan
```

**Config — read through `get_settings()`, never `os.environ`.** The token stays `str | None` in `Settings`
(T1's decision, so `/health` boots without it); the client raises `ConfigurationError` at construction when
it is absent, with a message naming the variable.

**Anti-patterns, all of which this repo will reject:**

- `**kwargs` on a client method. The VSA reference at
  `.claude/references/vertical-slice-architecture.md:767-820` shows exactly this — **do not copy it.**
  `kwargs.get(...)` is `Any` and both the `Any` guard and the type checkers will fail.
- `response.json()` bound to a variable or returned. It is `Any`. Every body goes straight into
  `Model.model_validate(response.json())` on one line.
- `# type: ignore` / `# pyright: ignore`. Zero, enforced by `tests/test_structure.py:79`.
- A bare `except Exception` that swallows. Catch `httpx.HTTPError` and re-raise as a typed `HubSpotError`.
- `time.sleep` in async code. Always `await asyncio.sleep(...)`.

---

## IMPLEMENTATION PLAN

### Phase 1: Dependency move and the slice skeleton

**Tasks:** move `httpx` to runtime deps and regenerate the lock; create `app/promotion/` with
`__init__.py`, `exceptions.py`, `schemas.py`. Nothing empty — the structure guard at
`tests/test_structure.py:138` fails on a package with no module in it.

### Phase 2: The client

**Depends on:** Phase 1 (needs the exception types and the schemas).

The request funnel, auth, throttle, retry, error mapping, lifecycle. This is the load-bearing module;
everything else in the slice is a caller.

### Phase 3: The boundary gate

**Depends on:** Phase 2 (it lives on the client's write path).
**Independent of:** Phases 4 and 5 — nothing in provisioning or dedupe touches it.

### Phase 4: Property provisioning

**Depends on:** Phase 2. **Independent of:** Phase 5.

### Phase 5: Dedupe

**Depends on:** Phase 2. **Independent of:** Phase 4.

### Phase 6: Tests, fixtures, docs and integration

**Depends on:** Phases 3, 4 and 5.

---

## STEP-BY-STEP TASKS

IMPORTANT: Execute every task in order, top to bottom. Each task is atomic and independently testable.

### UPDATE `pyproject.toml`

- **IMPLEMENT**: move `"httpx>=0.28"` out of `[dependency-groups].dev` into `[project].dependencies`,
  keeping the list alphabetical. Then `uv lock && uv sync`.
- **PATTERN**: `pyproject.toml:7-16` (the runtime list, alphabetical)
- **GOTCHA**: httpx was dev-only because T1 used it solely as the ASGI test client; it is runtime now. Do
  **not** add `respx` or `pytest-httpx` — see the NOTES section for why `httpx.MockTransport` wins here
  despite being the less fashionable choice. Per `.claude/references/conventions.md` `## merge`, a
  `uv.lock` conflict is regenerated, never hand-merged.
- **VALIDATE**: `uv sync && uv run pytest -q`
- **SATISFIES**: AC1

### CREATE `app/promotion/exceptions.py`

- **IMPLEMENT**: `HubSpotError(LocalProspectEngineError)`, `default_code = "hubspot_error"`,
  `status_code = 502`. Subclasses:
  - `HubSpotAuthError` — 401/403. Docstring: the fix is a token with the right scopes, not a retry. HubSpot
    names the missing scope in the 403 message; carry it through.
  - `HubSpotRateLimitError` — raised only when the bounded retries are exhausted. Keyword-only
    `attempts: int`, `policy: str | None`.
  - `HubSpotResponseError` — any other non-2xx. Keyword-only `status_code_received: int`,
    `category: str | None`, `correlation_id: str | None`.
  - `UnprovenancedWriteError` — the gate refusing. Keyword-only `fields: tuple[str, ...]`. Docstring records
    that this covers **prospect field writes only**, and that task creation is ungated by design (T13).
- **PATTERN**: MIRROR `app/core/exceptions.py:26-52`, especially `CostLimitExceededError`.
- **IMPORTS**: `from typing import ClassVar`, `from app.core.exceptions import LocalProspectEngineError`
- **GOTCHA**: do **not** name the attribute `status_code` on `HubSpotResponseError` — it would shadow the
  `ClassVar` that `app/main.py:88` reads to pick the HTTP status of the rendered error. Hence
  `status_code_received`.
- **VALIDATE**: `uv run pyright app/promotion/exceptions.py && uv run mypy app/promotion/exceptions.py`
- **SATISFIES**: AC1, AC4

### CREATE `app/promotion/schemas.py`

- **IMPLEMENT**: Pydantic models for everything crossing the wire.
  - `HubSpotObject` — `id: str`, `properties: dict[str, str | None]`, `created_at`/`updated_at`
    (aliases `createdAt` / `updatedAt`), `model_config = ConfigDict(populate_by_name=True, extra="ignore")`.
  - `PropertyDefinition` — `name`, `label`, `type`, `field_type` (alias `fieldType`), `group_name`
    (alias `groupName`), `description`.
  - `PropertyGroupDefinition` — `name`, `label`, `display_order` (alias `displayOrder`).
  - `SearchFilter` (`property_name` alias `propertyName`, `operator`, `value`), `SearchFilterGroup`,
    `SearchRequest` (`filter_groups`, `properties: list[str]`, `limit: int = 10`), `SearchResponse`
    (`total: int`, `results: list[HubSpotObject]`).
  - `HubSpotErrorBody` — `status`, `message`, `category: str | None`, **`error_type: str | None`
    (alias `errorType`)**, `policy_name: str | None` (alias `policyName`), `correlation_id: str | None`
    (alias `correlationId`). `extra="ignore"`, and **every field optional** — HubSpot documents the envelope
    as varying per endpoint.
  - `TaskCreate` — `subject: str`, `due_at: datetime`, `body: str | None`, `task_type: TaskType`,
    `priority: TaskPriority`, `owner_id: str | None`.
  - `TaskType` (`EMAIL`/`CALL`/`TODO`), `TaskPriority` (`LOW`/`MEDIUM`/`HIGH`), `TaskStatus` as `StrEnum`s.
- **PATTERN**: MIRROR `app/shared/provenance.py:66-93` for the `StrEnum`-with-docstrings style.
- **GOTCHA**: `extra="ignore"` on **response** models (HubSpot adds fields without warning) but
  `extra="forbid"` on **request** models (a typo'd key should fail here, not silently no-op in the portal).
  Serialize with `model_dump(by_alias=True, exclude_none=True)` — sending `null` for an omitted property is
  not the same as omitting it. **`TaskStatus`: confirm the real enum against the live property** before
  committing to members; HubSpot's own docs contradict themselves (see the API facts section).
- **VALIDATE**: `uv run pyright app/promotion/schemas.py`
- **SATISFIES**: AC1

### CREATE `app/promotion/client.py` — the request funnel

- **IMPLEMENT**:
  - Module constants: `BASE_URL = "https://api.hubapi.com"` and **`API_VERSION = "2026-09"`** in exactly one
    place, with a comment recording the 18-month support window and that bumping it is a one-line change.
    Paths are built as `f"/crm/objects/{API_VERSION}/companies"`, `f"/crm/properties/{API_VERSION}/{obj}"`.
  - `HubSpotClient.__init__(self, *, token: str, transport: httpx.AsyncBaseTransport | None = None)`
    building an `httpx.AsyncClient(base_url=BASE_URL, headers={"Authorization": f"Bearer {token}",
    "Content-Type": "application/json"}, timeout=httpx.Timeout(10.0, read=30.0), transport=transport)`.
  - A private `_request` funnel every public method goes through, taking the method, path, an optional typed
    body, and `retry_on_server_error: bool`.
  - **A throttle, not just a retry.** After each non-search response, read `X-HubSpot-RateLimit-Remaining`
    and `-Interval-Milliseconds`; when remaining is low, sleep out the interval before the next call.
    Separately, hold a `asyncio.Lock` + last-call timestamp enforcing **≥200 ms between search calls**
    (5 req/s), because search responses carry no headers to react to.
  - `async def aclose(self)`, plus the module-level `get_hubspot_client()` / `is_initialised()` /
    `aclose_hubspot_client()` trio. `get_hubspot_client()` reads
    `get_settings().hubspot_private_app_token` and raises `ConfigurationError` naming
    `HUBSPOT_PRIVATE_APP_TOKEN` when it is `None`.
- **PATTERN**: MIRROR `app/core/database.py:30-91` for the singleton trio and `app/core/middleware.py:28-53`
  for the logging triple.
- **IMPORTS**: `import asyncio`, `import httpx`, `from app.core.config import get_settings`,
  `from app.core.exceptions import ConfigurationError`, `from app.core.logging import get_logger`
- **GOTCHA** — read all six; they are the whole difficulty of this task:
  1. **`retry_on_server_error` is not a style choice.** Retrying a 429 is safe: the request never executed.
     Retrying a **5xx on a POST that creates an object is not** — HubSpot may have created it and failed to
     respond, and the retry creates a second one. Pass `False` from every create path, `True` from reads.
     Same reasoning for a connect/read timeout on a create: do not retry.
  2. **There is no `Retry-After` on a 429.** Do not write code that reads one. Back off on a fixed
     exponential schedule with jitter, bounded at ~4 attempts, then raise `HubSpotRateLimitError`. The
     documented strategy is to *stay under* the limit — the throttle is the real mechanism, the retry is the
     safety net.
  3. **The 429 body uses `errorType` and `policyName`, not `category`.** Parsing only `category` loses the
     `DAILY` vs `TEN_SECONDLY_ROLLING` distinction — and those want different treatment: a secondly-rolling
     rejection is worth retrying, a daily-quota rejection is not (250,000/day means something is badly
     wrong; fail loudly rather than sleeping).
  4. **`477` must not be retried in-process.** It means a data-centre migration that can last up to 24
     hours. Raise. Likewise `423 Locked` gets at most one retry after ≥2 s.
  5. **`response.json()` is `Any`.** Never bind it. `HubSpotErrorBody.model_validate(response.json())` on
     one line — wrapped in a `try`, because an HTML error page from a proxy is not JSON and must not turn a
     503 into an unhandled `ValidationError`.
  6. **Build paths from `API_VERSION`, never hardcode `/crm/v3/`.** Most tutorials and StackOverflow answers
     still show `v3`; it works, but it is the frozen legacy surface.
- **VALIDATE**: `uv run pyright app/promotion/client.py && uv run mypy app/promotion/client.py`
- **SATISFIES**: AC1, AC7

### ADD the boundary gate to `app/promotion/client.py`

- **IMPLEMENT**:
  - `type ProvenancedProperty = ProvenancedValue[str] | ProvenancedValue[int] | ProvenancedValue[float] | ProvenancedValue[datetime]`
  - `def to_property_payload(properties: Mapping[str, ProvenancedProperty | None]) -> dict[str, str]` —
    **the gate.** Collect every name where `is_promotable(value)` is `False`; if any, raise
    `UnprovenancedWriteError(fields=tuple(sorted(names)))` **before serializing anything**, so no partial
    payload is ever built. Then render: `datetime` → epoch-milliseconds string, `bool` → `"true"`/`"false"`,
    everything else → `str(value)`.
  - `async def create_company` / `update_company` / `create_contact` / `update_contact`, each taking
    `Mapping[str, ProvenancedProperty | None]`, calling `to_property_payload` first, then
    `_request(..., retry_on_server_error=False)`.
  - `async def create_task(self, task: TaskCreate, *, contact_id: str | None = None,
    company_id: str | None = None) -> HubSpotObject` — **plain values, no provenance.** Associations go
    inline in the create body using `HUBSPOT_DEFINED` type IDs **204** (task→contact) and **192**
    (task→company). Its docstring must say why it is ungated, naming T13 and the 22 records: *the gate
    governs prospect field writes, not task creation.*
- **PATTERN**: the gate's docstring restates the rule at the enforcement point rather than linking to
  `app/shared/provenance.py:156-168`.
- **IMPORTS**: `from collections.abc import Mapping`, `from app.shared.provenance import ProvenancedValue, is_promotable`
- **GOTCHA**: the signature is the real gate — a caller holding a `dict[str, str]` cannot compile. The
  runtime `is_promotable` check exists for the `| None` case, which is how a sourcing stage says "I could
  not cite this field". **Collect every offender and raise once**; refusing on the first makes the caller
  fix fields one round-trip at a time. Pydantic generics are invariant, so the union alias is deliberate —
  `ProvenancedValue[object]` will not substitute.
- **VALIDATE**: `uv run pytest tests/promotion/test_gate.py -q && uv run pyright app/promotion/client.py`
- **SATISFIES**: AC4, AC5

### CREATE `app/promotion/properties.py`

- **IMPLEMENT**: `async def ensure_property_group(client)` then `async def ensure_properties(client) -> None`.
  - Group `lpe` / "Local Prospect Engine", created first — `groupName` is **required** on every property
    create and the group must already exist.
  - `lpe_source_url` — `string` / `text` — COMPANY **and** CONTACT
  - `lpe_sourced_at` — `datetime` / **`date`** (the fieldType is `date`; there is no `datetime` fieldType) —
    COMPANY **and** CONTACT
  - `lpe_vertical` — `string` / `text` — COMPANY **and** CONTACT
  - `lpe_priority_score` — `number` / `number` — COMPANY only
  - `lpe_route_cluster` — `string` / `text` — COMPANY only
  - Idempotency: `GET /crm/properties/{API_VERSION}/{objectType}/{name}` first. 404 → create, log
    `property_created`. 200 → log `property_present`, **create nothing and do not PATCH** (a human may have
    edited the label). If the existing property's `type` differs from ours, log a warning naming both types
    and carry on — that is a human's call, not a silent overwrite. Catch a 409 from the create as success
    too, for the race.
- **GOTCHA**: **`lpe_vertical` is a plain string, not an enumeration.** An enum reads better in the HubSpot
  UI, but it would make vertical #3 a property migration — the exact coupling "vertical is data, not code"
  exists to prevent, and a direct hit on M9. Also: **property internal names are permanent in HubSpot.** And
  do not branch on the 409 error *message*; the string is community-reported, not documented.
- **VALIDATE**: `uv run pytest tests/promotion/test_properties.py -q`
- **SATISFIES**: AC2

### CREATE `app/promotion/dedupe.py`

- **IMPLEMENT**:
  - `def normalize_phone(raw: str) -> str` — digits only, drop a leading US `1`, keep the last 10. Pure, no
    I/O. This exists because **HubSpot's phone search deliberately ignores the country code** and matches on
    area code + local number, so the country code must be stripped from the query.
  - `async def find_existing_company(client, *, domain: str | None, phone: str | None) -> HubSpotObject | None`
    — `EQ` on `domain` first (the reliable key), then `EQ` on `phone`. Returns the first match; logs
    `promotion.hubspot.duplicate_found` with `matched_on`.
  - `async def find_existing_contact(client, *, email: str | None, phone: str | None) -> HubSpotObject | None`
    — `EQ` on `email`, then `phone` and `mobilephone` (both confirmed present on CONTACT in portal 244766495
    on 2026-09-27).
  - Re-check every candidate client-side with `normalize_phone` before declaring a phone match. HubSpot's
    standardization is good but it is not ours, and a false dedupe silently drops a real prospect.
  - Keep each search to one `filterGroup`. The caps are generous (5 groups / 18 filters) but the OR-semantics
    of multiple groups are easy to get subtly wrong; two sequential searches are clearer than one clever one.
- **GOTCHA** — the one that will bite in production: **HubSpot's search index lags writes** ("a few
  moments", no SLA). Dedupe cannot protect a single run from duplicating something *it just created*. That
  protection is T9's `promotion` ledger. Say so in the module docstring so T9's implementer does not assume
  this function covers it.
- **VALIDATE**: `uv run pytest tests/promotion/test_dedupe.py -q`
- **SATISFIES**: AC3

### CREATE `tests/promotion/conftest.py` and the fixtures

- **IMPLEMENT**:
  - `hubspot_token` fixture monkeypatching `HUBSPOT_PRIVATE_APP_TOKEN` (the autouse `_clear_settings_cache`
    at `tests/conftest.py:33` makes it take effect).
  - An autouse fixture resetting the client singleton around every test — copy `_reset_database_state` at
    `tests/conftest.py:41`.
  - `load_fixture(name)` reading `tests/promotion/fixtures/<name>.json`.
  - A `MockTransport` factory taking a route table whose **default branch raises `AssertionError` naming the
    unmatched request**. That is what makes "no live portal calls in CI" a proven fact rather than an
    intention.
  - Fixtures: company search hit and miss, contact search hit, created company, created task, property 200
    and property 404, a 429 with the `errorType`/`policyName` body, a 500, and a standard error envelope.
- **GOTCHA**: **scrub the fixtures** — no token, no `Authorization` header. Prefer synthetic company and
  contact data that matches the real schema over a verbatim portal dump; the fixture's job is to pin the
  response *shape*.
- **VALIDATE**: `uv run pytest tests/promotion -q`
- **SATISFIES**: AC6

### CREATE the four test modules

- **IMPLEMENT**:
  - `test_gate.py` — a provenanced value passes; a `None` is refused; **several `None`s are reported in one
    exception**, not the first; a refused write issues **zero** HTTP requests (assert against the transport);
    `datetime` renders as epoch millis and keeps its UTC normalization.
  - `test_client.py` — the `Authorization` header is set; paths carry `API_VERSION`, not `v3`; a 429 is
    retried and then succeeds; retries exhausted raise `HubSpotRateLimitError`; a `DAILY` policy 429 fails
    fast rather than sleeping; **a 5xx on a create is not retried** (the transport counts exactly one
    request); a 401 raises `HubSpotAuthError`; `correlationId` reaches the exception; a non-JSON error body
    does not raise `ValidationError`; a missing token raises `ConfigurationError`; the token appears in no
    captured log line; two search calls are spaced ≥200 ms; **the client exposes no sequences method** —
    Starter has none, and in this repo the absence is the assertion.
  - `test_properties.py` — all five created on an empty portal, on the right object types; a second
    `ensure_properties` issues zero POSTs; a 409 on create is treated as success; a type mismatch warns and
    does not PATCH; the group is created once and before any property.
  - `test_dedupe.py` — `normalize_phone` over a table of real formats (`(214) 555-0147`, `+1 214-555-0147`,
    `214.555.0147`, `2145550147`, `+12145550147 ext 3`); a domain hit returns the match; a phone hit that
    differs only in formatting still matches; a miss returns `None`; **a create path with an existing match
    creates nothing** — prove it with `patch(..., side_effect=AssertionError(...))` per
    `tests/core/test_health.py:28`.
  - One test in `test_client.py` for the T13 guarantee: **a task created for a record with zero provenanced
    fields succeeds.**
- **PATTERN**: MIRROR `tests/core/test_cost.py` for class grouping and assertion density.
- **VALIDATE**: `uv run pytest tests/promotion -v`
- **SATISFIES**: AC1–AC8

### UPDATE `app/main.py`, `.env.example`, and CREATE `app/promotion/README.md`

- **IMPLEMENT**: add `await aclose_hubspot_client()` to the lifespan next to `await dispose_engine()`
  (`app/main.py:52`). Uncomment `HUBSPOT_PRIVATE_APP_TOKEN` in `.env.example` and list the seven scopes
  verbatim, including the note that tasks are gated behind the **contacts** scope. Write the slice README:
  what the gateway does, the gate, the five properties and why `lpe_vertical` is a string, the pinned
  `API_VERSION` and its 18-month clock, the search-lag caveat, and what is deliberately absent (sequences,
  deals, activity reads, batch endpoints, sending).
- **GOTCHA**: **T2 is running in a parallel worktree and also edits `app/main.py`** (it mounts the manifest
  router) and `pyproject.toml` (the `lpe` CLI entry point). The edits sit in different places so a textual
  conflict is unlikely, but `uv.lock` will conflict — regenerate it. Whoever merges second re-runs the full
  validation suite before pushing, not just their own tests.
- **VALIDATE**: `uv run uvicorn app.main:app --reload` then `curl -fsS localhost:8000/health`
- **SATISFIES**: AC1

### RUN full validation and commit

- **IMPLEMENT**: the whole suite, then commit per `.claude/references/conventions.md` — conventional tag,
  imperative subject ≤72 chars, **no AI attribution**. Add a T3 section to `_tasks/todo.md`.
- **VALIDATE**: `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pyright && uv run pytest -q`
- **SATISFIES**: every AC

---

## TESTING STRATEGY

### Unit Tests

Pure functions get direct tests: `normalize_phone`, `to_property_payload`, the backoff schedule, the
throttle's interval arithmetic. No transport, no fixtures — a table of inputs and expected outputs, in the
style of `tests/core/test_cost.py`.

### Integration Tests

Everything touching the client runs through `httpx.MockTransport` with recorded JSON fixtures. "Integration"
here means *the client, its throttle, its retry policy, its parsing and its callers together* — not a live
portal. The mock transport's unmatched-route branch raises, so a test that reaches for a real endpoint fails
loudly instead of hanging on a network call.

### Edge Cases

Each of these must have a test:

- A `TEN_SECONDLY_ROLLING` 429 (retry) and a `DAILY` 429 (fail fast — 250,000 calls means a bug, not load).
- Retries exhausted → `HubSpotRateLimitError`, not an infinite loop.
- **A 5xx on a create is not retried.** The single most expensive bug available in this ticket is a retry
  that silently double-creates a company.
- A non-JSON error body (proxy HTML) — must not raise `ValidationError`.
- A `477` — raised, not slept through.
- A property that already exists with a *different* type — warn, do not overwrite.
- A 409 on property create — treated as success.
- A phone differing only in formatting — must still dedupe. A phone with a country code — must still dedupe.
- A phone normalizing to fewer than 10 digits (extension, garbage) — must not match everything.
- Multiple unprovenanced fields — one exception listing all of them; zero HTTP requests issued.
- A task created for a record with **no** provenanced fields at all — must succeed. This is the T13
  guarantee; if it fails, adoption is impossible.

---

## VALIDATION COMMANDS

Execute every command to ensure zero regressions and 100% feature correctness.

**Baseline, verified in this worktree on 2026-09-27 before any T3 code:** 91 tests pass, ruff clean, mypy
clean over 25 source files, pyright 0 errors. Any failure below is yours.

### Level 1: Syntax & Style
```bash
uv run ruff check .
uv run ruff format --check .
```

### Level 2: Types
```bash
uv run mypy .
uv run pyright
```
Both must pass. Zero `# type: ignore`, zero `Any` escape hatches.

### Level 3: Unit & Integration Tests
```bash
uv run pytest -q
uv run pytest tests/promotion -v            # the new suite
uv run pytest tests/test_structure.py -q    # no Any, no suppressions, no empty packages
```

### Level 4: Manual Validation
```bash
uv run uvicorn app.main:app --reload
curl -fsS localhost:8000/health             # still 200 — the client must not be built at import time
```
Optionally, **once**, against the real portal with a valid token — not in CI, not in a test:
```bash
uv run python -c "
import asyncio
from app.promotion.client import get_hubspot_client, aclose_hubspot_client
from app.promotion.properties import ensure_properties
async def main() -> None:
    await ensure_properties(get_hubspot_client())
    await aclose_hubspot_client()
asyncio.run(main())
"
```
Run it twice: the second pass must log five `property_present` events and zero `property_created`.
While you are there, settle the open enum question:
```bash
curl -fsS -H "Authorization: Bearer $HUBSPOT_PRIVATE_APP_TOKEN" \
  "https://api.hubapi.com/crm/properties/2026-09/tasks/hs_task_status" | python -m json.tool
```

### Level 5: Everything at once
```bash
/piv-validate
```

---

## ACCEPTANCE CRITERIA

- [ ] **AC1** — `ruff`, `mypy --strict`, `pyright --strict` and `pytest` all pass with **zero suppressions**;
      `httpx` is a runtime dependency and the lockfile is regenerated
- [ ] **AC2** — all five custom properties are provisioned idempotently on the right object types, under a
      property group that is created first; **a second run no-ops** (zero writes, proven by test)
- [ ] **AC3** — dedupe on domain **and** phone runs before any create; an existing match is returned and
      nothing is created; phone matching survives formatting differences and a country code
- [ ] **AC4** — `is_promotable()` is enforced **at the client boundary** for prospect field writes: the
      signature makes a raw dict impossible, and an unprovenanced field raises `UnprovenancedWriteError`
      listing every offender with no partial payload sent
- [ ] **AC5** — **task creation is not gated**; a task for a record with zero citable fields succeeds
      (the T13 guarantee)
- [ ] **AC6** — every test runs against recorded fixtures; an unmatched request fails the test rather than
      reaching the network
- [ ] **AC7** — the client self-throttles from the rate-limit headers, spaces search calls to ≤5/s, retries
      a rolling 429 with bounded backoff, **never retries a 5xx on a create**, raises on `477`, and logs
      every call under `promotion.hubspot.*` with no token in any log line, exception or fixture
- [ ] **AC8** — tier-aware: no sequences method exists on the client, and a test asserts its absence
- [ ] **AC9** — no regressions: the 91 pre-existing tests still pass

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully
- [ ] Full test suite passes
- [ ] No linting or type checking errors, zero suppressions
- [ ] Manual validation confirms `/health` still answers and the second provisioning pass no-ops
- [ ] Acceptance criteria all met
- [ ] `app/promotion/README.md` written; `_tasks/todo.md` updated; committed per
      `.claude/references/conventions.md` (no AI attribution)

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decide this one before writing the client — it is the only structural choice left:**

- **Dated API version (`/crm/objects/2026-09/...`) or the legacy `v3`/`v4` URLs?** This plan picks the
  **dated version**, pinned in one constant. Reasoning: legacy is explicitly the frozen surface, so starting
  there means a migration is already owed on day one; the dated version costs a one-line bump roughly every
  6 months (unsupported at 18), and a single constant makes that bump trivial. The counter-argument is real
  though — this is an unattended weekly job maintained by one person, and legacy URLs have no announced
  sunset, so "frozen" is also "will not surprise you". **If the weekly-run-must-never-break instinct wins,
  switch the constant to `v3` and record it here**; nothing else in the plan changes.

**Assumptions this plan makes — flag rather than silently diverge if any is wrong:**

1. **The `lpe_` prefix on property internal names.** Chosen so our fields are unmistakably ours in a portal
   with 3,118 contacts of mixed provenance, and so a future HubSpot-native `source_url` cannot collide.
   Property internal names are permanent once created — close to a one-way door, worth agreeing before the
   first run against the real portal.
2. **`lpe_vertical` is a string, not an enumeration.** Rationale in the task. Reversible only by creating a
   new property, so it is a real decision.
3. **Priority score and route cluster on COMPANY only**; source URL / sourced-at / vertical on both COMPANY
   and CONTACT. Scoring and routing describe a business; provenance describes a record, and E18 is a
   *contact*-quality finding.
4. **The token is validated at client construction, not at startup.** `Settings` keeps
   `hubspot_private_app_token: str | None` (T1's decision) so the service still boots without it — `/health`
   must not need HubSpot.
5. **`create_task` belongs in T3.** The ticket scopes the token to tasks read+write and makes "tasks are not
   gated" an acceptance criterion, which is untestable without a task path. **Activity *reads* are deferred
   to T11** — interpreting "was this touch done" is cadence policy (D5), not transport.
6. **Fixtures are synthetic but schema-accurate**, not verbatim portal dumps.
7. **One object per call, no batch endpoints.** ~20 promotions a week against 100 requests/10s.

**Genuinely open — do not silently answer these (CLAUDE.md, *Working principles*):**

- **Who owns `lpe_sourced_at` — the caller or the client?** This plan has T9 pass `lpe_source_url` and
  `lpe_sourced_at` as ordinary provenanced properties, keeping T3 dumb. The alternative is for the client to
  *derive* both from the provenance of a designated record-defining field, making it structurally impossible
  for the meta-properties to disagree with the fields they describe. The simple version is here; the safer
  version costs T9 an argument. **Decide when planning T9**, and record it there.
- **The real `hs_task_status` enum.** HubSpot's own docs contradict themselves (property table says
  `NOT_STARTED`/`COMPLETED`; the example on the same page uses `WAITING`). The Level 4 curl above settles it.
  Blocks T11 more than T3, but `TaskStatus` is defined here.
- **The 409 body for a duplicate property** is community-reported, not documented. Handled defensively; if
  the live behaviour differs, record what it actually was.
- **Google Places terms of use and real pricing** remain unread/unverified (ticket doc → *Still open*).
  Blocks nothing in T3.

**Confirmed live against the portal on 2026-09-27** (read-only, via the HubSpot connector) — recorded so the
implementer need not re-check:

- Portal **244766495**, *CompuMatrice Inc.*, `accountType: STANDARD`, UI domain `app-na2.hubspot.com`,
  timezone `America/Chicago`, USD. (The UI domain is what T13 will need to build a HubSpot record URL for
  `retrieval_method = "manual_hubspot_entry"`.)
- Seats: `core`, `sales-starter`, `service-starter`, `view-only`. **No professional seat → no sequences.**
  Spike 3's answer still holds.
- **None of the five custom properties exist on COMPANY.** A keyword search returned only HubSpot's own
  analytics-source and `website` fields. The ticket's claim is current.
- `phone` and `mobilephone` both exist on CONTACT. `hs_searchable_calculated_phone_number` did **not** appear
  in the portal search results, though the docs describe the `hs_searchable_calculated_*` family as the
  mechanism behind phone search — treat querying it directly as **unconfirmed**; filter on `phone` and let
  HubSpot's standardization do the work.

## NOTES (open canvas)

**Why the gate is a signature and not a runtime check.** `to_property_payload(Mapping[str,
ProvenancedProperty | None])` means the only way to pass an unprovenanced value is to pass `None` — a
sourcing stage explicitly saying "I could not cite this". A raw `dict[str, str]` does not type-check, so the
gate cannot be routed around by accident, only by a deliberate edit to this module. Same move
`ProvenancedValue` itself makes: turn "wrong data in HubSpot" into "code that does not type-check".

**Why `httpx.MockTransport` and not `pytest-httpx` or `respx`.** The research recommended a library, and on
ergonomics it is right — `pytest-httpx` 0.36.2 is typed and fixture-based, `respx` 0.23.1 is more
expressive. Two things decide it the other way here. First, both work by patching httpx globally for the
duration of a test, and this repo's existing `client` / `error_client` fixtures (`tests/conftest.py:56-82`)
run an **`ASGITransport`** through the same library — a global patch is a live collision risk with the
suite that already exists. Second, `MockTransport` is injected exactly the way `ASGITransport` already is,
so the slice's tests read like the ones next to them and no dependency is added to get a seam httpx ships.
Roughly fifteen lines of `conftest.py`. Revisit if the route tables get unwieldy.

**The retry table, spelled out** — the part most likely to be got wrong:

| Situation | Retry? | Why |
|---|---|---|
| 429, `policyName: TEN_SECONDLY_ROLLING` | **yes**, bounded backoff | burst limit; the request never executed |
| 429, `policyName: DAILY` | **no** | 250,000 calls/day means a bug, not load. Fail loudly |
| 5xx on GET / search | yes | idempotent |
| 5xx on POST create | **no** | HubSpot may have created the object and failed to respond; a retry duplicates it |
| 5xx on PATCH update | yes | idempotent by value |
| Connect / read timeout on a create | **no** | same reasoning as the 5xx POST case |
| 401 / 403 | no | a scope problem; retrying cannot fix it. The 403 message names the missing scope |
| 423 Locked | once, after ≥2 s | documented as short-lived bulk-sync throttling |
| 477 Migration | **no** | a data-centre migration lasting up to 24 hours. Raise |

**The throttle matters more than the retry.** HubSpot's own guidance is to stay under the limit, not to
react to rejection — and on search there is no choice, because search responses carry **no rate-limit
headers at all** and the ceiling is 5 req/s. A run that dedupes 120 qualified candidates is 120+ search
calls; at 5/s that is ~25 seconds of deliberate pacing. Budget for it rather than discovering it.

**The duplicate risk dedupe does *not* cover.** HubSpot's search index lags writes, so two candidates in the
same run resolving to the same company will both miss and both create. T9's `promotion` ledger is the real
protection; T3's job is to be honest about the boundary rather than imply a guarantee it cannot make. This
goes in the `dedupe.py` module docstring for exactly that reason.

**A finding T11's planner needs.** There is no single "activities on this contact since X" endpoint. The
documented path is: walk associations (`GET /crm/objects/{v}/contacts/{id}/associations/{calls|emails|notes|meetings}`),
then batch-read those engagement objects for `hs_timestamp`, then compare client-side against the task's
creation time — because `hs_timestamp` is not a server-side filter on batch-read. Whether the engagement
*search* endpoints support filtering by association to a contact is unconfirmed. D5's "a touch is done when
a matching activity was logged after the task was created" is therefore two or three round trips per
prospect, not one. That is a T11 sizing fact, discovered here, recorded here.

**Parallel-merge surface with T2.** T3 touches `pyproject.toml` (httpx), `uv.lock`, `app/main.py` (lifespan)
and `.env.example`. T2 touches `pyproject.toml` (the `lpe` CLI entry point), `uv.lock`, `app/main.py` (a
router) and adds a migration. `uv.lock` will conflict; regenerate it, never hand-merge. T3 adds **no
migration**, so the single-head guard added in wave-2 prep will not fire on this branch.

**Confidence for one-pass success: 8/10.** The slice shape, the patterns, the tests and — after the
research pass — the API contract are all pinned down, including the two things most likely to have been got
wrong from memory (there is no `Retry-After` on a 429, and the paths are date-versioned now). The remaining
two points: the property-conflict body is community-reported rather than documented, the `hs_task_status`
enum needs one live call to settle, and the phone-search behaviour is documented but unexercised against
this portal's actual data.

## AMENDMENTS

<!-- Append-only. Newest at the bottom. Leave empty until the plan has been approved/executed. -->
