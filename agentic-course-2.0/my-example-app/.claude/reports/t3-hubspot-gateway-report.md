# Implementation Report — T3: HubSpot gateway

**Plan**: `.claude/plans/t3-hubspot-gateway.md`   **Branch**: `feat/t3-hubspot-gateway`   **Status**: COMPLETE

## Summary

`app/promotion/` now holds the one place this service talks to HubSpot: an async `httpx` client that
self-throttles, retries carefully and maps HubSpot's two error envelopes onto typed exceptions;
idempotent provisioning of the five custom properties; and dedupe on domain and phone. The write-gate
is enforced at the client boundary **by signature** — prospect field writes take `ProvenancedValue`s,
so a caller holding a raw `dict[str, str]` does not type-check — while `create_task` is deliberately
ungated, which is the T13 adoption guarantee. T3 ships a gateway with no caller; T9 and T11 are its
consumers.

## Tasks completed

- Dependency move → `pyproject.toml` (UPDATE), `uv.lock` (regenerated, never hand-edited)
- Exception hierarchy → `app/promotion/exceptions.py` (CREATE)
- Wire models → `app/promotion/schemas.py` (CREATE)
- Client, request funnel, throttle, retry, gate → `app/promotion/client.py` (CREATE)
- Property provisioning → `app/promotion/properties.py` (CREATE)
- Dedupe → `app/promotion/dedupe.py` (CREATE)
- Slice surface → `app/promotion/__init__.py` (CREATE)
- Slice docs → `app/promotion/README.md` (CREATE)
- Lifespan disposal → `app/main.py` (UPDATE, one `await` beside `dispose_engine()`)
- Token + the seven scopes → `.env.example` (UPDATE)
- Decision log → `_tasks/todo.md` (UPDATE)

## Tests added

63 new tests in `tests/promotion/`, all against recorded fixtures through an injected
`httpx.MockTransport`. The mock portal raises on any unrouted request, so "no live portal calls"
is proven rather than intended.

| File | Cases | Covers |
|---|---|---|
| `conftest.py` | — | mock portal, responders, fixture loader, singleton reset, sleep recorder |
| `test_gate.py` | 10 | rendering (incl. epoch-ms, UTC normalization, bool), refusal, every offender named once, zero requests on refusal, gate ≡ `is_promotable` |
| `test_client.py` | 23 | bearer header, dated paths, token never logged, rolling vs `DAILY` 429, exhausted retries, header throttle, 5 req/s search pacing, 5xx-on-create not retried, 5xx-on-read retried, timeout-on-create not retried, 477 raised, 423 retried once, 401/403 + missing scope, correlation id, non-JSON body, task ungated, associations inline, naive due date refused, no sequences method, singleton lifecycle |
| `test_properties.py` | 12 | all 8 placements, right object types, group before properties, camelCase bodies, `date` fieldType, `lpe_vertical` a string, second pass writes nothing, 409 as success, drift warns without PATCH |
| `test_dedupe.py` | 18 | `normalize_phone` over 13 real formats, domain hit, formatting-only phone match, miss, unusable phone never searched, dedupe never creates, email hit, phone/mobilephone searched separately |
| `fixtures/*.json` | 14 | synthetic but schema-accurate; scrubbed — no token, no `Authorization` |

## Validation results

| Level | Command | Result |
|---|---|---|
| 1 | `ruff check .` | pass |
| 1 | `ruff format --check .` | 39 files already formatted |
| 2 | `mypy .` | pass, 37 source files, **zero suppressions** |
| 2 | `pyright` (strict) | **0 errors**, 0 warnings |
| 3 | `pytest` | **154 passed** (91 pre-existing + 63 new) — AC9, no regressions |
| 3 | `pytest tests/test_structure.py` | 15 passed — no `Any`, no suppressions, no empty packages, one migration head |
| 4 | boot + `curl /health` | `{"status":"ok",...}`, clean shutdown, **0 `client_created` events** — the client is not built at import or boot |

All nine acceptance criteria met. **AC2's "second run no-ops" is proven by test** (zero POSTs, eight
`property_present`), not by the live portal — see *Not done* below.

## Deviations from the plan

Each of these is an intentional decision, not an oversight.

**Settled an open question with evidence.** The plan left the real `hs_task_status` enum open and
said one live call would settle it. Read read-only from portal 244766495 via the HubSpot connector:

- `hs_task_status` has **five** members — `NOT_STARTED`, `IN_PROGRESS`, `WAITING`, `COMPLETED`,
  `DEFERRED`. Both halves of HubSpot's self-contradicting docs were incomplete. All five modelled.
- `hs_task_priority` includes **`NONE`** alongside `LOW`/`MEDIUM`/`HIGH`. All four modelled.
- `hs_task_type` also offers `LINKED_IN_CONNECT` and `LINKED_IN_MESSAGE`. **Deliberately excluded**
  from `TaskType`: PRD §8 makes LinkedIn automation a non-goal — it violates Sales Navigator's terms
  and risks the single seat. `TaskType` is a request-side enum, so reading a task back is unaffected.

**Typing, to hold zero suppressions under both checkers.**

- Pydantic aliases are split into `validation_alias` (response models) and `serialization_alias`
  (request models) rather than a bare `alias`. A bare alias renames the *constructor* argument too,
  so `PropertyDefinition(field_type=...)` stops type-checking and every call site has to spell
  HubSpot's camelCase. This keeps camelCase on the wire and snake_case in the code.
- `to_property_payload` reaches `is_promotable` through a one-line `_is_cited` wrapper that `cast`s
  to the base type. The shared predicate is generic in `T` and pydantic generics are invariant, so a
  union of four parametrizations gives no single `T` to bind. Same move, and the same reason, as the
  existing `cast` in `app/shared/provenance.py`. `is_promotable` itself is untouched, as instructed.
- HTTP status codes are module constants rather than `httpx.codes` members: that enum carries a
  `(value, phrase)` pair through a custom `__new__`, which Pyright strict reads as a tuple and then
  reports every comparison against it as permanently false.
- `_render` became public `render_property_value` — Pyright strict forbids cross-module private use,
  and the bool branch genuinely needs a direct test (`bool` is an `int` at runtime, so a
  `ProvenancedValue[int]` can hold one; without the branch the portal receives `"True"`).

**Additions beyond the plan's list.**

- `HubSpotTransportError` — a fifth exception type. The plan's own retry table names
  "connect/read timeout on a create" as a distinct row; folding it into `HubSpotResponseError` would
  have meant inventing a fake status code for a request that never got one.
- `ExistingProperty` — a response counterpart to `PropertyDefinition`, so the plan's own
  forbid-on-requests / ignore-on-responses rule holds for the property read.
- `SearchOperator` — a StrEnum with one member (`EQ`), so a typo cannot pass silently. Members get
  added as callers need them.

**Smaller calls.**

- `UnprovenancedWriteError.status_code` is **500**, not the base class's 502. Nothing left the
  process when it fires, so calling it a bad *gateway* would point a reader upstream at exactly the
  wrong moment.
- A **blank** `HUBSPOT_PRIVATE_APP_TOKEN` is treated as missing. `.env.example` now ships the key
  uncommented and empty (as the plan asked), which means the empty string is what a fresh clone
  produces — and a client built on it sends `Bearer ` and 401s three layers from the cause. Mirrors
  provenance's "a blank source is no source".
- `property_group_exists()` returns `bool` rather than a group model — one call site, no schema
  earned.
- **Four event names were renamed** to satisfy the pre-existing guard in
  `tests/core/test_logging.py`, which requires exactly one underscore in the last segment:
  `property_group_created/present` → `group_created/present`, `property_type_mismatch` →
  `property_drifted`, `error_body_unparsed` → `body_unparsed`. The guard was left alone rather than
  loosened.
- **The plan's Level-4 note says a second pass logs "five `property_present`" events; it is
  eight.** Five properties across eight (object type, property) placements — three are on both
  COMPANY and CONTACT. Plus two `group_present`. The tests assert eight.

The plan's one structural open question — dated API version vs legacy `v3` — was left at the plan's
own choice: dated, pinned in `API_VERSION`.

## Not done, and why

**The optional live-portal provisioning run** (plan, Level 4: run `ensure_properties` against the
real portal twice). Not run. It **writes** five custom properties to portal 244766495, and property
internal names are permanent in HubSpot — there is no rename and no clean undo. That needs an
explicit go-ahead rather than being folded into a validation pass. Everything it would prove is
covered by test; what it would add is confirmation of the two facts the plan marked unconfirmed (the
409 conflict body, and phone-search behaviour against this portal's real data).

## Issues encountered

- `uv` commands and all writes into the worktree need the sandbox disabled — the worktree sits
  outside the session's writable roots, and `uv` trips on its cache. Both already recorded in memory.
- One test initially asserted against the wrong request: `MockPortal.bodies()` matches on a path
  fragment, and the group-create path (`.../companies/groups`) *contains* the property-create path,
  so it returned the group body. Fixed with a property-only accessor that excludes `/groups`, and the
  reason is in its docstring.

## Handoff notes

- **For T9**: the plan's open question — whether `lpe_sourced_at` is passed by the caller (as built)
  or derived by the client from a designated record-defining field — is still open, by design.
  Decide it when planning T9 and record it there.
- **For T11**: there is no single "activities since X" endpoint. The path is walk associations, then
  batch-read engagements for `hs_timestamp`, then compare client-side — two or three round trips per
  prospect, not one. Recorded in `app/promotion/README.md`.
