# `app/promotion/` — the HubSpot gateway, and promotion

The one place this service talks to HubSpot. T3 built the gateway (`client.py`, `dedupe.py`,
`properties.py`); T9 built promotion on it — a finished run's qualified candidates become a
Company + Contact at `pending review`, and the founder's decision is read back and handed to the
cadence (`service.py`, the `promotion` ledger, `lpe promotion`). The cadence slice (T11) is the
gateway's other caller.

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

**Engine metadata is the other carve-out** (T9, OQ6-A). Four properties are not claims about the
prospect but facts about our process, with no honest URL to cite: `lpe_vertical`,
`lpe_review_status`, `lpe_route_cluster` and `hubspot_owner_id`. They travel in `RecordMeta`, a
closed, typed model of exactly those four (a test pins the set), rendered **after** the prospect
fields have passed the gate and merged into the same body; a key claimed by both halves is refused
before any request. `lpe_priority_score` is not metadata — it is a judgment about the business, and
goes through the gate with its `llm_inference` citation.

## The six custom properties

| Internal name | Type / fieldType | COMPANY | CONTACT |
|---|---|---|---|
| `lpe_source_url` | `string` / `text` | ✅ | ✅ |
| `lpe_sourced_at` | `datetime` / **`date`** | ✅ | ✅ |
| `lpe_vertical` | `string` / `text` | ✅ | ✅ |
| `lpe_priority_score` | `number` / `number` | ✅ | — |
| `lpe_route_cluster` | `string` / `text` | ✅ | — |
| `lpe_review_status` | `enumeration` / `select` | ✅ | — |

Scoring, routing and review describe a business; provenance describes a record, and E18 is a *contact*-
quality finding. There is no `datetime` fieldType in HubSpot — the type is `datetime`, the
fieldType is `date`.

`lpe_review_status` (T9, OQ2-A) **is** an enumeration — `pending_review` · `accepted` · `rejected`
— because its values are a closed workflow, not data, and a select is what lets the founder filter
and bulk-edit the company list view. No "pending review" stage existed in the portal, and
`hs_lead_status` is the founders' own (10 contacts carry it), so reusing it would read their status
changes as review decisions. Options a human adds later are not drift: nothing PATCHes options.
It is created by the first real `lpe promotion promote`, which runs `ensure_properties` first.

`lpe_vertical` is a **string, not an enumeration.** An enum reads better in the HubSpot UI, but it
would make vertical #3 a property migration — exactly the coupling "vertical is data, not code"
exists to prevent, and a direct hit on M9.

Property internal names are **permanent** in HubSpot; there is no rename. The `lpe_` prefix keeps
our fields unmistakably ours in a portal holding 3,118 contacts of mixed provenance.

`ensure_properties()` is read-then-create and safe on every deploy: a second pass issues zero
writes and logs nine `property_present` events. It never PATCHes — a human may have edited a
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
that is the `promotion` ledger (below), not `dedupe.py`.

## Promotion (T9)

```
lpe promotion promote --run <id> [--dry-run]
  run completed or degraded? ──no──▶ RunNotPromotableError (a failed run is re-batched)
  resume the vertical's pending_create rows (recovery, below)
  per candidate, in registry-id order:
    eligible? ──no──▶ counted by reason; nothing written, no ledger row
    in the ledger already (any run, any status)? ──yes──▶ already_promoted
    both payloads through the gate ──fail──▶ failed (no orphan company)
    claim the ledger row (pending_create, with company_key + dedupe keys) ── commit
    same phone/domain as another ledger row? ──yes──▶ duplicate (duplicate_in_ledger)
    already in HubSpot (domain, then phone)? ──yes──▶ duplicate (duplicate_in_hubspot); nothing written to it
    create company ── commit its id ── create contact (inline primary association) ── pending_review ── commit
```

- **Eligible** (`eligibility.py`) means the six required fields are cited — `registry_id`,
  `legal_name`, `address`, `phone`, `business_check`, `owner` — and the candidate is **qualified
  under OQ1-D**: not suppressed (no `disqualification` for its `(vertical, registry_id)`) and not a
  cited-*dead* score. **An unscored candidate is promotable** (absent is not bad; the founder judges
  it at review — confirmed 2026-10-13). Reasons are checked in a fixed order: `disqualified`, then
  missing fields (`no_principal` when the owner is the only gap), then `dead_score`.
- **The ledger** (`models.py`) is a state machine, one row per `(vertical, registry_id)`:
  `pending_create → pending_review → enrolling → enrolled`, terminal `rejected`, `duplicate`,
  `withdrawn`. Its unique key is what makes a re-run, or next week's run, promote nothing twice.
- **Mapping** (`mapping.py`): every property carries its source field's citation unchanged — the
  address split into four, the owner's name into two. `lpe_source_url` / `lpe_sourced_at` are
  **derived** from the registry citation (company) and the owner citation (contact). The contact
  gets **no email and no phone**: both census values are the business's. `dba_name` and the
  headcount band are not written.
- **Recovery.** The intent is committed before every create, so an interrupted pass is resumed, not
  repeated. A row with a company id but no contact: walk the company's contacts (associated in the
  same request, so no lag) and adopt ours by `lpe_source_url` + `lastname`, else create it. A row
  with no company id: only once **10 minutes** have passed since its last create attempt (past the
  search lag), search companies by `lpe_source_url`; found → adopt, not found → dedupe and create.
  Sooner → `recovery_deferred`. Every company create stamps the row's `updated_at` first, so a
  recovery whose own create was lost waits the full interval again.
  A create is never retried in process.
- **A bad token or the daily quota stops the pass**; any other HubSpot failure is counted and the
  next candidate goes on.
- **A dry run** makes the same reads (portal dedupe included) and the same in-run ledger dedupe, in
  memory, and writes nothing — no ledger row, no record, no property provisioning.

**The first write to a record that already existed** has not happened yet: under OQ5-A a duplicate
is flagged and nothing is written to it. Whoever builds enrichment (OQ5-B) must stamp what they
write onto a pre-existing record as `manual_hubspot_entry` — source the record's URL, retrieved at
its `createdate` — never as if we had sourced it.

## Review (T9)

`lpe promotion sync [--dry-run]` reads each `pending_review` company's `lpe_review_status`:

| Founder set | The sync | Ledger |
|---|---|---|
| `accepted` | `CadenceService.enrol(contact, company=…)` — the cadence creates the first task | `enrolling` → `enrolled` |
| `rejected` | nothing in HubSpot | `rejected` |
| *(company deleted or archived)* | nothing | `withdrawn` |
| `pending_review`, blank | nothing | waiting |
| anything else | warned about (someone added an option) | waiting |
| *(left out of HubSpot's answer, not reported deleted)* | counted `failed` | unchanged; read again next sync |

A row left `enrolling` by a failed enrolment is read again with the rest and follows the **current**
value: still `accepted` retries the enrolment; `rejected` or deleted is recorded as such; anything
else moves it back to `pending_review`. A founder who rejects a prospect after a failed enrolment
never gets its task.

`decided_at` is when a sync last acted on a decision. Like every ledger column it is never cleared,
so a row set back to `pending_review` keeps the time of the accept it was moved back from.

**Decisions are final once synced.** `rejected`, `withdrawn` and `enrolled` are never read again,
so changing `lpe_review_status` afterwards does nothing. The ledger is machine state and is never
edited by hand, so there is no way back for a synced rejection; if one is ever needed, it is a new
`lpe` command, not a SQL edit.

Promotion never creates a task itself; its job ends at the hand-off. A row left `enrolling` by a
crash is retried first on the next sync — `enrol` reuses a task that carries its key. A contact
already in the cadence counts as enrolled. **Run it daily from the 07:00 launchd job, before
`lpe cadence sync`** (a human edit of the plist).

## Names (OQ4-A)

`names.py` splits the census `company_officer_1` string: the first word is `firstname`, everything
after it is `lastname`, a single-letter initial stays with the first name and a trailing JR/SR/II
with the last. **Case is never changed** and nothing is dropped. Three or more full words —
a middle name, a compound surname, a glued title — are ambiguous: split the same way, **flagged**
in the CLI and counted `name_ambiguous`, for the founder to fix at review. Measured on 3,000 census
values: 73.5% split confidently, 26.5% are ambiguous, 1 is a single word.

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
