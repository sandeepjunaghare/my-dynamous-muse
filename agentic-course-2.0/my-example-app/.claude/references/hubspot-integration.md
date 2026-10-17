# HubSpot integration

Load this when writing to HubSpot, shaping the Supabase↔HubSpot boundary, or touching the cadence.

**The split store, with one rule: a candidate graduates to HubSpot only when it qualifies *and* a human
accepts it.** Supabase is the sourcing workbench — `vertical_manifest`, `sourcing_run`, `candidate`,
`disqualification`, `promotion` — machine state, never human-edited. `disqualification` is what stops the
engine re-sourcing the same national rollups every week, which a HubSpot-only model cannot do. HubSpot is the
human surface: Company · Contact (owner/principal) · Task (cadence touches) · Note (outcomes) · Deal (the $999
assessment). Anything a person reads or edits lives there and only there — no shadow CRM, no spreadsheet (M4).

**The write-gate.** Every prospect field carries source URL, retrieval timestamp and retrieval method, and a
field without provenance cannot be written to HubSpot. That is the structural fix for E18 (a company name
sitting in a first-name field is exactly a field nobody could cite) and what turns M6 and M8 into something
enforced rather than hoped for. Custom properties carry what the human needs to trust the record: source URL,
sourced-at, vertical, priority score, route cluster, review status — created by the first real
`lpe promotion promote`, never by hand or through the connector. Five of them are **engine metadata**, not
claims about the prospect, and pass in the closed `RecordMeta` beside the gate (T9, OQ6-A): vertical, review
status, route cluster, verification (2026-10-16) and the record owner. The score is gated: it is a judgment
about the business.

**Review lives on the company, in `lpe_review_status`** (T9): `pending_review` at create, then the founder
sets `accepted` or `rejected`, singly or in bulk from the company list view. `lpe promotion sync` reads it back
— accepted → `CadenceService.enrol` (the cadence creates the first task; promotion never does), rejected →
recorded and left alone. The decision is a HubSpot fact we read, never one we mirror. Not `hs_lead_status`:
the founders already use it.

**Before any create, dedupe on domain and phone** — the portal already holds 3,118 contacts and 1,040
companies of mixed provenance. Access is a private app token scoped to contacts/companies/deals/tasks
read+write; rate limits apply per tier.

**We own the cadence schedule; HubSpot owns the outcomes.** Spike 3 is answered: portal 244766495 offers the
seats `core`, `sales-starter`, `service-starter` and `view-only` — no professional seat, so **no sequences**,
and Starter workflows are capped at roughly 10 actions with one workflow per trigger and no branching. There
is no HubSpot scheduler to hand a three-touch state machine to, so `app/cadence/` holds it.

The boundary that keeps this from becoming the shadow CRM M4 exists to prevent: **Supabase holds which touch
is due, which cycle we're in, and whether the prospect is parked; HubSpot holds whether the touch happened
and what was said.** One source of truth per fact, never two copies of one fact. Tasks stay the human
surface and outcomes are never stored on our side — we read them, we do not mirror them.

**A touch is done when its Task is complete *or* a matching activity was logged on the contact after that
task was created.** Ties break toward done, because a machine that nags about a call you already made is
worse than one that occasionally advances early. Cycle position is reconstructed from logged activity rather
than reset, so a prospect touched twice by hand resumes at touch three — park-after-three-cycles still counts
those prior touches.

**Adoption (T13).** Prospects the system did not source (the hand-worked ones already in the portal) are
adopted into the machine with `lpe cadence adopt`, from a roster a person writes after a dry run. Adoption
creates cadence tasks and schedule rows only — **no prospect field write and no `candidate`** — so the
write-gate, which governs field writes and not task creation, has nothing to check. Honest provenance for
these records (`retrieval_method = "manual_hubspot_entry"`, source = the HubSpot record URL, `retrieved_at` =
the record's create date) applies at the first field write to such a record. T9 makes none: a promoted
candidate that matches an existing company is flagged `duplicate` and nothing is written to it (OQ5-A). Evidence is read from the contact
only, as the sync reads it; old hand tasks are listed for a person to close, never written. Details:
`app/cadence/README.md` → *Adoption*.

One dependency left open on purpose: the `$999 assessment` deal pipeline does not exist (9 deals, all
January, none at that value) and we have decided not to create it yet. M3 is therefore unmeasurable, and the
Friday report must render it as **not configured** — never as `0`, which would be indistinguishable from a
real zero.

**The Friday report reads notes, by prefix** (T10, DECISION 1, 2026-10-17). A read of the portal found every
real touch logged as a free-text **note**; its calls and meetings are HubSpot's sample data. So the founders'
logging rule is: **start the note with `Door knock`, `Owner conversation` or `Assessment booked`**. Emoji,
punctuation and case before the label are ignored, and `Door-knock` counts. `lpe runs report` counts each
label toward its playbook number over the Monday-to-Sunday Dallas week (a notes search on `hs_timestamp`,
`GTE`/`LT` in epoch ms, paged with `after`). Every other note is shown as an **unclassified touch**, so the
convention's adoption is visible on Friday. The report is read-only and reads live; it never logs note text.
Accept/reject (M6) comes from our `promotion` ledger as of the last daily sync, not from HubSpot.
