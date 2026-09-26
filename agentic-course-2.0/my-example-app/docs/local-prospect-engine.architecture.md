# Architecture — Local Prospect Engine

**Status:** Decisions made, pending Spike 1 · **Date:** 2026-09-26
**Intent:** [`local-prospect-engine.prd.md`](./local-prospect-engine.prd.md) — the *what* and *why*. This doc is the *how*.
**Scope:** High-level architecture decisions. Not an implementation plan — that comes per-ticket via `piv-plan-implementation`.

---

## Problem & goals

Compumatrice's founders have ~4 hours a week for new business and spend nearly all of it building prospect
lists by hand, then re-typing outcomes into a spreadsheet that only sometimes reaches HubSpot. The goal is a
weekly, verified, route-clustered prospect list that lands directly in HubSpot with the three-touch cadence
scheduled — so those four hours go into conversations instead of Google Maps.

Every decision below is judged against three constraints from the PRD: **founder time is the scarce resource**
(M2: under 1 hr/week), **HubSpot is the system of record** (M4), and **vertical is an input, not a constant**
(M9: vertical #2 in under a founder-day).

---

## Approaches considered

| | Approach | For | Against |
|---|---|---|---|
| **A** | **New service from the `base-research-agent` template** — FastAPI + Claude Agent SDK, MCP source tools, Supabase, vertical slices | Matches every convention already enforced; evals harness exists; sourcing genuinely *is* agentic work (search → verify → cross-reference → cite); properly proves M9 portability | Heaviest. Weeks before the first list exists, for a tool with one user |
| **B** | **n8n workflow** — already running in `local-ai-packaged`; native HubSpot node and scheduler | Days, not weeks, to a first list. Scheduling and HubSpot solved. Cheap to throw away | No type safety, no evals, painful to version or review. Qualification logic buried in a canvas. Violates every standard in CLAUDE.md, so it becomes a permanent second system if it succeeds |
| **C** | **Assemble, don't build** — HubSpot sequences for cadence, a small script for sourcing, review in HubSpot | Thinnest. Attacks the follow-through failure using an engine already paid for | Sourcing stays semi-manual; never proves portability; low ceiling |

**Chosen: A, sequenced behind a cheap C-shaped spike.**

Not a compromise — a response to evidence. E15 shows 22 qualified prospects already in HubSpot with **0
follow-up tasks completed and 11 past due**. Until we know why, we cannot know whether a sourcing engine helps
or simply lengthens a queue nobody works. Spike 1 settles it for roughly zero cost.

---

## Recommended approach

A single FastAPI service built from the `base-research-agent` template, exposing one capability: **take a
brief (vertical · ICP band · geography) and produce a qualified, route-clustered prospect list in HubSpot with
the cadence scheduled.**

Inside it, a Claude Agent SDK loop calls a small set of generic MCP tools — search a declared registry, verify
a business identity, resolve the owner, classify rollup-vs-local, cluster routes — all parameterized by a
**vertical manifest**. Sourcing state and provenance live in Supabase; anything a human touches lives in
HubSpot. The cadence is HubSpot's, not ours.

The system's job ends the moment a qualified prospect and its scheduled first touch exist in HubSpot. It does
not call, does not knock, and (for now) does not send.

---

## Key decisions

### Stack & libraries

Inherited wholesale from `base-research-agent`, because a stack the team already enforces beats a better one
they don't: **FastAPI · Claude Agent SDK (`ClaudeSDKClient`) · Python 3.12 · Pydantic v2 · Supabase/Postgres ·
structlog (`domain.component.action_state`) · MyPy + Pyright strict, zero suppressions · uv · pytest · ruff ·
Docker Compose · vertical slice architecture.**

Deliberately **not** carried over for the MVP:

- **The Vite/React frontend.** See *Review surface* below.
- **Redis.** The template requires it for caching, sessions and semantic indexing. This service has one user,
  no sessions and no semantic index. Add it only if a run-lock is needed. *(YAGNI — revisit if wrong.)*
- **Langfuse.** Open question below; tracing a single-user weekly job may not earn its keep.

Added: an HTTP client for the source registries. No new framework.

### Data model — the shape

**Split store, with one rule: a candidate graduates to HubSpot only when it qualifies and a human accepts it.**
That keeps M4 honest — there is no shadow CRM — while giving the system a memory of what it already rejected.

**Supabase (the sourcing workbench — machine state, never human-edited):**

- `vertical_manifest` — the M9 lever. Declares the authoritative source(s), disqualifier rules, qualifying
  signals, ICP band and vocabulary for one vertical. Versioned.
- `sourcing_run` — one brief execution: vertical, geography, ICP band, timings, counts.
- `candidate` — a sourced business pre-qualification, with every field carrying its own provenance.
- `disqualification` — candidate + the rule that fired + when. **This is what stops the engine re-sourcing the
  same national rollups every week**, which a HubSpot-only model cannot do.
- `promotion` — the link from a candidate to the HubSpot company/contact ids it became.

**HubSpot (the human surface — everything a person reads or edits):**

- Company · Contact (owner/principal) · Task (cadence touches) · Note (touch outcomes) · Deal (the $999
  assessment — a pipeline that does not exist yet, see *Missing pieces*).
- Custom properties carrying what the human needs to trust the record: source URL, sourced-at, vertical,
  priority score, route cluster.

### Boundaries & contracts

- **HubSpot** — private app token, scoped to contacts/companies/deals/tasks read+write. Dedupe on domain and
  phone *before* create; the portal already holds 3,118 contacts and 1,040 companies of mixed provenance.
  Rate limits apply per tier.
- **FMCSA / SAFER** (first source) — public federal records: MC numbers, authority status, fleet size, BOC-3
  filings. Bulk census files plus a keyed lookup API. Terms of use to confirm before first use.
- **Google Places** — paid, per-request. Needed for address and phone verification and for the review-name
  signal that often surfaces an owner. Cost is per-run and must be bounded.
- **Secrets** — `.env`, never committed. The template's existing posture; nothing new.
- **Auth** — single internal user. **No multi-tenant auth, no Supabase RLS for the MVP.** Building either now
  would be scaffolding for the "product later" path the PRD put in Non-goals.
- **Outbound email** — deliberately unbound until Spike 4. Nothing sends from `compumatrice.com`.

### Other architectural calls

**Vertical as a declarative manifest, not code.** M9 only holds if adding a vertical means writing a manifest,
not a feature slice. Freight's manifest names FMCSA, asset-based-carrier exclusion, "which TMS and does it have
an API", and freight vocabulary. Fire's names the Texas Fire Marshal registry, rollup exclusion, and inspection
language. Same tools, different data.

**Five generic tools, not one per source** — following the template's "fewer, smarter tools" principle. Search a
declared registry · verify a business identity · resolve the owner · classify rollup-vs-local · cluster routes.
Adding freight after fire adds a manifest, not a toolchain.

**Evidence grounding, generalized to field level.** The template mandates `evidence_grounding` before every
response to prevent hallucination. Here the analogue is: **every prospect field carries source URL, retrieval
timestamp and method, and a field without provenance cannot be written to HubSpot.** This is the direct
structural fix for E18 — a company name sitting in a first-name field is exactly a field nobody could cite. It
turns M6 and M8 from aspirations into a write-gate.

**Review surface: HubSpot, no frontend.** The PRD's founding complaint was that *"Apollo doesn't help — it's
still go login to Apollo, search, take action, then put it in HubSpot manually."* A purpose-built review UI
recreates that: another login, another surface, another sync. Sourced prospects instead land at a
`pending review` stage in HubSpot with provenance on the record. Cost: bulk accept/reject is clunkier than a
custom screen. Reversible — the template's frontend pattern remains available if review proves painful.

**Cadence stays HubSpot's.** Tasks and workflows drive the three touches; we do not rebuild a scheduler.
Rebuilding it would put tasks in two places, which is the exact failure M4 exists to prevent.

**Scheduling: boring on purpose.** One weekly run plus ad-hoc. A scheduled trigger against a FastAPI endpoint.
No Celery, no queue framework — YAGNI at one user, one run a week.

---

## Missing pieces

Things this approach depends on that do not exist yet:

- **The vertical manifest schema** — the central abstraction; nothing like it exists today.
- **A rollup-vs-local classifier.** E7's rule currently lives in a founder's head and a hand-written skip list.
- **Owner resolution.** E18: 41% of sourced contacts have no person identified at all. This is the hardest
  single problem in the system and the one M6 measures.
- **HubSpot custom properties** for provenance, route cluster and priority score.
- **DFW geographic route clustering** — done by hand today (Olympic Drive; the 75238 pair).
- **A `$999 assessment` deal pipeline in HubSpot.** It does not exist — 9 deals, all from January, none at that
  value. M3 cannot be measured until it does.
- **A rewritten opener.** Not engineering, but it blocks the email leg. E16: three AI-based rejections against
  two notes where the opener was *"local AI expert"*, contradicting the playbook's own rule.

---

## Spikes & experiments

**Spike 1 — Is the constraint sourcing, or discipline?** *(blocks the first slice)*
- **Question:** would a sourcing engine help, or does the queue simply not get worked?
- **Spike:** work the 22 existing HubSpot prospects through the three-touch cadence. **Timebox: 2 weeks.**
- **Decision rule:** ≥3 decision-maker conversations/week → sourcing is the bottleneck, build the engine as
  specified. Tasks still uncompleted → the first slice is cadence enforcement, not sourcing, and the
  architecture's centre of gravity moves.

**Spike 2 — Is FMCSA enough on its own?**
- **Question:** can we get from an MC number to a named principal *and* a headcount band for DFW non-asset
  brokerages?
- **Spike:** sample 50 DFW brokerages through FMCSA alone. **Timebox: 1 day.**
- **Decision rule:** ≥50% yield a named principal → FMCSA is the spine. Below that → the freight manifest needs
  a second source (Texas SOS or Google Places) from day one, and M6's 70% target needs re-basing.

**Spike 3 — What does our HubSpot tier actually support?**
- **Question:** do we have sequences, or only tasks and workflows?
- **Spike:** check the portal. **Timebox: 1 hour.**
- **Decision rule:** sequences available → use them for the cadence. Not available → tasks plus workflows, and
  the three-touch state machine is thinner than assumed.

**Spike 4 — Sending domain.** *(the one-way door)*
- **Question:** can we send autonomously without risking `compumatrice.com`?
- **Spike:** stand up a separate warmed subdomain with volume caps; send manually first at low volume.
- **Decision rule:** no autonomous sending from any domain until reply and complaint rates are observed
  manually. Domain reputation takes months to repair and would damage client and delivery mail, not just
  outreach.

---

## Open questions

- [ ] **Does Spike 1 change the first slice?** Everything downstream assumes sourcing is the bottleneck.
- [ ] **Redis — needed at all?** The template requires it; this service may not. Decide when a run-lock is.
- [ ] **Langfuse — keep or drop?** Tracing is valuable for an agent loop, but this is one user and one weekly
      run. Cheap to add later; noise to carry now.
- [ ] **Terms of use per source.** FMCSA, Google Places and any state registry each carry their own. Needs a
      decision per source, recorded in the manifest.
- [ ] **Google Places cost ceiling per run** — unbounded verification calls are the obvious way to make this
      expensive.
- [ ] **Who rewrites the opener, and by when?** Blocks the email leg regardless of architecture.
- [x] **New repo, or a slice inside an existing `base-*` project?** **Decided 2026-09-26: new repo.** The
      service is built in `my-example-app/` under `app/`, not as a slice inside a `base-*` project —
      different domain, different lifecycle. Recorded in `CLAUDE.md` → *Architecture map*.

---

*Intent: [`local-prospect-engine.prd.md`](./local-prospect-engine.prd.md) · Next: `piv-slice-epic` to break this into tickets.*
