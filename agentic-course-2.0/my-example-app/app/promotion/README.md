# `app/promotion/` — the HubSpot gateway

The one place this service talks to HubSpot. **T3 ships the gateway and no caller**: the promotion
slice proper (T9 — the write-gate in anger, Company/Contact create, the `promotion` ledger) and the
cadence slice (T11) are what use it.

## The write-gate

Every prospect field carries a source URL, a retrieval timestamp and a retrieval method, and a
field without provenance cannot be written to HubSpot (E18 → M6/M8). `to_property_payload()` is
where that is enforced, and it is reached **by signature**: the write methods take
`ProvenancedValue`s, so a caller holding a raw `dict[str, str]` does not type-check. The gate is
not something a later slice can route around by accident — only by editing this module.

When a field cannot be cited the caller passes `None`, which is a sourcing stage saying *I could
not cite this*. Every offender is collected and raised in one `UnprovenancedWriteError`, and
nothing is serialized until the whole mapping has passed — no partial payload is ever built.

**Task creation is deliberately ungated.** The gate governs prospect *field* writes. T13 adopts the
22 hand-typed prospects already in the portal; they carry no citable fields at all, and a gate
applied to `create_task` would refuse the ticket that exists to rescue them. A test asserts it.

## The five custom properties

| Internal name | Type / fieldType | COMPANY | CONTACT |
|---|---|---|---|
| `lpe_source_url` | `string` / `text` | ✅ | ✅ |
| `lpe_sourced_at` | `datetime` / **`date`** | ✅ | ✅ |
| `lpe_vertical` | `string` / `text` | ✅ | ✅ |
| `lpe_priority_score` | `number` / `number` | ✅ | — |
| `lpe_route_cluster` | `string` / `text` | ✅ | — |

Scoring and routing describe a business; provenance describes a record, and E18 is a *contact*-
quality finding. There is no `datetime` fieldType in HubSpot — the type is `datetime`, the
fieldType is `date`.

`lpe_vertical` is a **string, not an enumeration.** An enum reads better in the HubSpot UI, but it
would make vertical #3 a property migration — exactly the coupling "vertical is data, not code"
exists to prevent, and a direct hit on M9.

Property internal names are **permanent** in HubSpot; there is no rename. The `lpe_` prefix keeps
our fields unmistakably ours in a portal holding 3,118 contacts of mixed provenance.

`ensure_properties()` is read-then-create and safe on every deploy: a second pass issues zero
writes and logs eight `property_present` events. It never PATCHes — a human may have edited a
label, and a property whose type has drifted is logged (`property_drifted`) and left alone.

## The API version

Paths are **date-versioned** (`/crm/objects/2026-09/...`), pinned in `API_VERSION` in `client.py`.
HubSpot moved off `v1`–`v4` on 2026-03-30; the legacy URLs still work but are explicitly the frozen
surface, so starting there would mean owing a migration on day one. A dated version is *Current*
for 6 months, *Supported* for 12 more and unsupported at 18 — expect to bump the constant about
twice a year. Most tutorials still show `/crm/v3/`; that is the old surface.

## Throttle and retry

The throttle matters more than the retry. HubSpot's guidance is to stay under the limit rather than
react to rejection, and **search responses carry no rate-limit headers at all** while the ceiling is
5 requests/second — so search calls are paced 200 ms apart. A run that dedupes 120 candidates is
~25 seconds of deliberate pacing; budget for it.

| Situation | Retry? | Why |
|---|---|---|
| 429, `TEN_SECONDLY_ROLLING` | yes, bounded backoff | burst limit; the request never executed |
| 429, `DAILY` | **no** | 250,000 calls/day is a bug, not load — fail loudly |
| 5xx on GET / search / batch read | yes | idempotent |
| **5xx on POST create** | **no** | HubSpot may have created it and failed to answer |
| 5xx on PATCH update | yes | idempotent by value |
| Timeout on a create | **no** | same ambiguity as the 5xx |
| 401 / 403 | no | a scope problem; the 403 message names the scope |
| 423 Locked | once, after ≥2 s | short-lived bulk-sync throttling |
| 477 Migration | **no** | a data-centre migration lasting up to 24 hours |

There is **no `Retry-After` header on a 429** — it appears only on 477. Do not write code that
reads one. The 429 body is also shaped differently from every other error: `errorType` and
`policyName`, not `category`.

## What dedupe cannot do

HubSpot's search index lags writes by "a few moments", with no published SLA. Two candidates in the
same run that resolve to the same company will both miss and both create. The protection against
that is **T9's `promotion` ledger**, not this module.

## Deliberately absent

- **Sequences** — portal 244766495 is Sales Hub Starter (Spike 3). There is no sequences API, which
  is why `app/cadence/` owns the three-touch state machine. A test asserts no such method exists.
- **Activity *policy*** — deciding "was this touch done" is cadence policy (D5), not transport, and
  lives in `app/cadence/sync.py`. T11 added the two **read** methods it needs here, so there is
  still one client: `batch_read()` (`POST .../{type}/batch/read`, 100 ids per call, retried on 5xx
  because it is a read) and `list_associated_ids()` (the association walk, following the paging
  cursor). There is no single "activities since X" endpoint and `hs_timestamp` is not a server-side
  filter on batch read, so the comparison happens client-side — a handful of round trips per
  prospect, not one. The engagement types (`calls`, `emails`, `notes`, `meetings`) are on
  `ObjectType` for reading only; nothing here creates one.
- **Deals** — the `$999 assessment` pipeline does not exist and is deliberately not being created
  (D13 / GATE-A). The token reads deals for T10's Friday report; nothing here writes one.
- **Batch endpoints** — ~20 promotions a week against 100 requests/10 s. Revisit if a run ever
  approaches the throttle.
- **Sending** — nothing sends from any domain until Spike 4.

## Testing

Everything runs against recorded JSON fixtures through an injected `httpx.MockTransport`, matching
how the parent conftest injects an `ASGITransport`. The mock portal **raises on any unrouted
request**, so a test that reaches for a real endpoint fails by name instead of hanging on a socket.
Fixtures are synthetic but schema-accurate, and carry no token.
