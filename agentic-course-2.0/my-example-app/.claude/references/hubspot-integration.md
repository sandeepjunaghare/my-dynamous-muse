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
sourced-at, vertical, priority score, route cluster — none of which exist in the portal yet.

**Before any create, dedupe on domain and phone** — the portal already holds 3,118 contacts and 1,040
companies of mixed provenance. Access is a private app token scoped to contacts/companies/deals/tasks
read+write; rate limits apply per tier.

**The cadence is HubSpot's, not ours.** Tasks and workflows drive the three touches; we do not build a
scheduler and do not mirror task state into Supabase — putting tasks in two places is the exact failure M4
exists to prevent. The system's job ends the moment a qualified prospect and its scheduled first touch exist
in HubSpot. Two open dependencies: Spike 3 (does our tier have sequences, or only tasks and workflows?) and
the `$999 assessment` deal pipeline, which does not exist yet — M3 cannot be measured until it does.
