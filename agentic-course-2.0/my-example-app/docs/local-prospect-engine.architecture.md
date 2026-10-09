# Architecture — Local Prospect Engine

**Status:** Decisions made · **Date:** 2026-09-26 · **Revised:** 2026-09-26
**Revision note:** two original decisions were reversed on evidence — the weekly run is a deterministic
pipeline rather than an agent loop, and the cadence state machine is ours rather than HubSpot's (Spike 3
answered: the portal is Sales Hub Starter, which has no sequences). Both reversals are marked inline.
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

Inside it, a **deterministic pipeline** of five generic stages — search a declared registry, verify a business
identity, resolve the owner, classify rollup-vs-local, cluster routes — all parameterized by a **vertical
manifest**. The Claude Agent SDK is called at exactly **two judgment nodes** (`resolve_owner`,
`classify_rollup`), each returning structured output with a citation; the other three stages are ordinary
HTTP and arithmetic. Sourcing state and provenance live in Supabase; anything a human touches lives in
HubSpot.

**Why a pipeline rather than an agent loop** *(revised 2026-09-26 — this reverses the original decision)*.
Three of the five stages never needed judgment, and the properties the tickets demand actively fight
model-driven control flow: route clustering must be stable across runs, promotion must be idempotent, and
provenance-as-a-write-gate means every field must trace to a specific call. A loop that re-decides its own
order each week makes all three harder, costs several times more per run, and is far more awkward to test
against fixtures. The judgment that genuinely exists — *is this a national rollup?*, *who actually owns this
business?* — is isolated at two nodes where it can be evaluated, cached and cited. This keeps the whole
stack, including the Agent SDK; it changes only who decides the order.

**The run works a backlog, not the whole registry** *(revised 2026-10-09, after the freight v3 review)*. The
original plan assumed ~600 census records per run. Measured against the real Company Census File, freight
v3's free predicates leave **4,449** active DFW broker candidates. Every stage over every candidate every week
would mean ~8,800 judgment calls (~$700) and a Places cap that trips on every run. So the free filters run
over the whole pool and persist it as a **backlog**. Each weekly run then takes a **fixed batch, best first**,
through the paid steps. The batch size, not the pool size, sets the bill (~$25–30 a week at a starting batch of
150). Disqualified candidates are suppressed (T7), so the pool works down and new registrations top it up.
Inside a batch the order is **free before paid, and cheaper paid before dearer paid**:

1. **Free:** census predicates, then QCMobile `allowToOperate` and the revocations join, done in code, never by a model.
2. **Cheapest paid:** Places verification (~$0.03 a candidate). It is also the evidence the next step needs: website, phone, business type.
3. **Dearest paid:** **one** `classify_rollup` call per candidate covering every judgment rule (~$0.08, measured
   from the live `propose` run at ~$0.02 a turn).
4. **Owner:** census `company_officer_1` first (present for 67%, free). `resolve_owner` runs only where it is absent.

**Where the agent does earn its keep: authoring manifests.** The open-ended work is not the weekly run, it is
*"what is the authoritative registry for collision centers, and what disqualifies one?"* — a research task
performed once per vertical. `app/manifests/` carries an agent that researches a brief and proposes a DRAFT
manifest row with every field cited; a human activates it on the CLI, recording the per-source terms-of-use
decision as they do. That makes M9 a real test rather than an assertion: vertical #2 is *review a proposal*,
not *write a row*.

The system's job ends the moment a qualified prospect and its scheduled first touch exist in HubSpot. It does
not call, does not knock, and (for now) does not send.

---

## Key decisions

### Stack & libraries

Inherited wholesale from `base-research-agent`, because a stack the team already enforces beats a better one
they don't: **FastAPI · Claude Agent SDK · Python 3.12 · Pydantic v2 · Supabase/Postgres · structlog
(`domain.component.action_state`) · MyPy + Pyright strict, zero suppressions · uv · pytest · ruff · Docker
Compose · vertical slice architecture.**

The Agent SDK is scoped to the two judgment nodes and the manifest-authoring agent — it is not the weekly
run's control flow. See *Recommended approach*.

Deliberately **not** carried over for the MVP:

- **The Vite/React frontend.** See *Review surface* below.
- **Redis.** The template requires it for caching, sessions and semantic indexing. This service has one user,
  no sessions and no semantic index. **Decided 2026-09-26: stays out.** One weekly run by one user cannot
  race itself; revisit only if a run-lock genuinely becomes necessary.
- **Langfuse.** Open question below; tracing a single-user weekly job may not earn its keep.
- **A Postgres container.** Supabase is **hosted**, two free projects (dev and prod), so Compose runs the
  application alone. A local test run writing into real sourcing state is a cheap mistake to prevent.

Added: an HTTP client for the source registries. No new framework.

**Runtime.** Local Mac first, a small VPS when the weekly run becomes something worth missing. Config is
strict 12-factor so that move is env vars only. Scheduling stays external — launchd now, cron later, against
the trigger endpoint.

### Data model — the shape

**Split store, with one rule: a candidate graduates to HubSpot only when it qualifies and a human accepts it.**
That keeps M4 honest — there is no shadow CRM — while giving the system a memory of what it already rejected.

**Supabase (the sourcing workbench — machine state, never human-edited):**

- `vertical_manifest` — the M9 lever. Declares the authoritative source(s), disqualifier rules, qualifying
  signals, ICP band and vocabulary for one vertical. Versioned, and **lifecycled `DRAFT → ACTIVE`**: the
  authoring agent writes drafts, a human activates them on the CLI while recording the terms-of-use decision
  per source. A source without that decision cannot be marked active.
- `sourcing_run` — one brief execution: vertical, geography, ICP band, timings, counts.
- `candidate` — a sourced business pre-qualification, with every field carrying its own provenance.
- `disqualification` — candidate + the rule that fired + when. **This is what stops the engine re-sourcing the
  same national rollups every week**, which a HubSpot-only model cannot do.
- `promotion` — the link from a candidate to the HubSpot company/contact ids it became.
- `cadence_state` — **the schedule only**: which touch is due, which cycle we're in, parked or live. Not
  outcomes. Added because our HubSpot tier has no sequences to hand the state machine to (see *Cadence*).

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
  filings. **QCMobile is a *lookup* API keyed on USDOT/MC number, not a search-by-geography API**, so the
  shape is: pull the bulk Company Census File, filter to DFW counties and active broker authority, then
  QCMobile per record for detail. Free, and licensed **public domain (CC PDM 1.0)** — the terms-of-use
  decision for this source is close to a formality, but still gets recorded at `manifest activate`.
- **Google Places** — paid, per-request. Needed for address and phone verification and for the review-name
  signal that often surfaces an owner. **Runs after the free filters and before the judgment call**
  (revised 2026-10-09). Nothing paid touches a candidate a free filter would drop, and the cheaper paid step
  feeds the dearer one its evidence. Text Search Pro is $32 / 1,000 after 5,000 free a month. Phone and
  website may need a higher SKU, so confirm the SKU when planning T6.
  Capped at 500 calls per run as a circuit breaker against a runaway loop; that is a safety valve, not a
  budget target (see *Cost*).
- **Secrets** — `.env`, never committed. The template's existing posture; nothing new.
- **Auth** — single internal user. **No multi-tenant auth, no Supabase RLS for the MVP.** Building either now
  would be scaffolding for the "product later" path the PRD put in Non-goals.
- **Outbound email** — deliberately unbound until Spike 4. Nothing sends from `compumatrice.com`.

### Other architectural calls

**Vertical as a declarative manifest, not code.** M9 only holds if adding a vertical means writing a manifest,
not a feature slice. Freight's manifest names FMCSA, asset-based-carrier exclusion, "which TMS and does it have
an API", and freight vocabulary. Fire's names the Texas Fire Marshal registry, rollup exclusion, and inspection
language. Same tools, different data.

**Five generic stages, not one per source** — following the template's "fewer, smarter tools" principle.
Search a declared registry · verify a business identity · resolve the owner · classify rollup-vs-local ·
cluster routes. Adding freight after fire adds a manifest, not a pipeline. One module per stage, registration
a one-line append — which is also what keeps parallel tickets mergeable, since each adds a file rather than
editing a shared one.

**Cost: a circuit breaker, not a ceiling.** No monthly budget is set; the first runs measure themselves, with
per-run cost logged from the scaffold onward rather than bolted on at the enrichment ticket. The 500-call
Places cap exists so a bug cannot run up a bill overnight. ~~Expected shape: ~600 census records in, ~200 after
free filters, ~120 after disqualification, ~80 verified~~ (superseded 2026-10-09). **Measured shape (freight
v3, census file):** TX 377,926 → active 194,663 → DFW counties 45,855 → broker code 4,518 → ≤10 power units
**4,449 in the backlog**. A weekly batch of **150** (tune from the first runs) goes through Places and one
judgment call each, for ~20 qualified names out and ~$25–30 a run. At that rate the backlog lasts ~30 weeks.

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

**Cadence: we own the schedule, HubSpot owns the outcomes.** *(revised 2026-09-26 — this reverses "cadence
stays HubSpot's", on evidence.)* Spike 3 is answered: the portal offers seats `core`, `sales-starter`,
`service-starter`, `view-only` — no professional seat, therefore **no sequences**, and Starter workflows are
capped at roughly 10 actions with one workflow per trigger and no branching. There is no HubSpot scheduler to
hand a three-touch state machine to, so `app/cadence/` holds it.

The original worry — tasks in two places, the exact failure M4 exists to prevent — is answered by splitting
on *fact*, not on *record*: **Supabase holds which touch is due, which cycle we're in, and whether the
prospect is parked; HubSpot holds whether it happened and what was said.** Each fact has one home. Tasks
remain the human surface; outcomes are read, never mirrored.

A touch is done when its Task is complete **or** a matching activity was logged on the contact after that
task was created, ties breaking toward done. Cycle position is reconstructed from logged activity rather than
reset, so a prospect already touched twice by hand resumes at touch three.

Because `cadence/` depends only on `core/` and the HubSpot client, it is a domain of its own rather than
promotion's back half — and it builds in parallel with the entire sourcing line instead of waiting behind
qualification and routing.

**Scheduling: boring on purpose.** One weekly run plus ad-hoc. A scheduled trigger against a FastAPI endpoint.
No Celery, no queue framework — YAGNI at one user, one run a week.
**The cadence sync is a second, daily trigger** (decided 2026-10-08, T11 review): `lpe cadence sync` from its own
launchd job, independent of the weekly run — same-day touches cannot wait a week. Overlapping syncs are kept
apart by a Postgres advisory lock, not Redis.

---

## Missing pieces

Things this approach depends on that do not exist yet:

- **The vertical manifest schema** — the central abstraction; nothing like it exists today.
- **A rollup-vs-local classifier.** E7's rule currently lives in a founder's head and a hand-written skip list.
- **Owner resolution.** E18: 41% of sourced contacts have no person identified at all. This is the hardest
  single problem in the system and the one M6 measures.
- **HubSpot custom properties** for provenance, route cluster and priority score.
- **DFW geographic route clustering** — done by hand today (Olympic Drive; the 75238 pair).
- **A three-touch cadence state machine.** Newly ours, since the tier has no sequences to hand it to. Nothing
  like it exists today — the 22 open tasks were created by hand and none has advanced.
- **An adoption path for prospects we did not source**, so the 22 can enter the machine without provenance
  they never had.
- **A `$999 assessment` deal pipeline in HubSpot.** It does not exist — 9 deals, all from January, none at that
  value. **Decided 2026-09-26: not creating it yet.** M3 stays unmeasurable and the Friday report renders it
  *not configured*, never `0` — an uninstrumented zero and a real zero are different facts.
- **A rewritten opener.** Not engineering, but it blocks the email leg. E16: three AI-based rejections against
  two notes where the opener was *"local AI expert"*, contradicting the playbook's own rule.

---

## Spikes & experiments

**Spike 1 — Is the constraint sourcing, or discipline?** *(runs alongside Waves 1–2; no longer blocks a slice)*
- **Question:** would a sourcing engine help, or does the queue simply not get worked?
- **Spike:** work the 22 existing HubSpot prospects through the three-touch cadence. **Timebox: 2 weeks.**
- **Decision rule:** ≥3 decision-maker conversations/week → sourcing is the bottleneck, build the engine as
  specified. Tasks still uncompleted → cadence enforcement carries more weight than sourcing, and the wave
  order shifts to put `cadence/` ahead of the sourcing line.
- **It ends in adoption either way.** Surviving prospects of the 22 are adopted into the cadence machine when
  the spike closes, so the finding arrives attached to a lever instead of as an observation. T1, T2 and T3
  are built during the two weeks because they are needed under either outcome.

**Spike 2 — Is FMCSA enough on its own?** *(widened 2026-09-26 — it now tests two fields, not one)*
- **Question:** can we get from an MC number to a named principal *and* a headcount band for DFW non-asset
  brokerages?
- **Spike:** filter the bulk Company Census File to DFW brokerages and sample 50 through QCMobile. **Timebox:
  1 day.** Running it against the census file rather than live lookups makes it cheaper, repeatable, and
  possible before the webkey exists.
- **Expect the headcount half to fail.** FMCSA's headcount-adjacent fields — power units, drivers — describe
  *carriers*. A non-asset brokerage shows near-zero regardless of how many people sit in its office, and the
  ICP band is *20–200 employees with an office function*. If that holds, headcount comes from Places and web
  signals, and M8's "populated **or absent**" clause will be exercised often and honestly.
- **Decision rule:** ≥50% yield a named principal → FMCSA is the spine for identity. Below that → the freight
  manifest needs a second source (Texas SOS or Google Places) from day one, and M6's 70% target needs
  re-basing. Headcount yield is recorded separately and is expected to drive the ICP filter to Places.

**Spike 3 — What does our HubSpot tier actually support?** — **ANSWERED 2026-09-26.**
- **Finding:** portal 244766495 offers the seats `core`, `sales-starter`, `service-starter`, `view-only`. No
  professional seat, so **no sequences**; Starter workflows are capped at roughly 10 actions, one workflow per
  trigger, no branching. *(Read from the portal's seat list — worth reconfirming in Settings → Account &
  Billing before anyone spends money on the strength of it.)*
- **Consequence:** the three-touch state machine has nowhere to live in HubSpot, so we own it. See *Cadence*.
  The alternative — upgrading to Sales Hub Professional — was considered and declined for now: a recurring
  per-seat bill on an internal tool with no revenue yet.

**Spike 4 — Sending domain.** *(the one-way door)*
- **Question:** can we send autonomously without risking `compumatrice.com`?
- **Spike:** stand up a separate warmed subdomain with volume caps; send manually first at low volume.
- **Decision rule:** no autonomous sending from any domain until reply and complaint rates are observed
  manually. Domain reputation takes months to repair and would damage client and delivery mail, not just
  outreach.

---

## Open questions

- [x] **Does Spike 1 change the first slice?** **Decided 2026-09-26: no — it changes the wave order, not the
      starting point.** T1/T2/T3 are needed under either outcome and are built while it runs; the spike ends
      in adoption regardless.
- [x] **Redis — needed at all?** **Decided 2026-09-26: no.** One weekly run by one user cannot race itself.
- [ ] **Langfuse — keep or drop?** Tracing is valuable for an agent loop, but this is one user and one weekly
      run — and with the run now a deterministic pipeline, structlog plus per-run cost logging covers most of
      what tracing would have given us. Leaning drop; revisit if the judgment nodes prove hard to debug.
- [x] **Terms of use per source.** **Decided 2026-09-26:** recorded in the manifest at
      `lpe manifest activate <id> --accept-terms <sources>`; a source without the decision cannot be marked
      active. FMCSA/QCMobile is public domain (CC PDM 1.0); Google Places still needs a read of its terms.
- [x] **Google Places cost ceiling per run** — **Decided 2026-09-26:** no ceiling, a circuit breaker. 500
      calls per run, per-run cost logged from T1, the ceiling set from observation rather than guess.
- [x] **Funnel size and order** — **Decided 2026-10-09:** a backlog worked in weekly batches (start 150), not
      a full pass. Free before paid, cheaper paid before dearer paid: Places before `classify_rollup`, which
      revises D8. One judgment call per candidate. Census `company_officer_1` before `resolve_owner`. See
      *Recommended approach*.
- [ ] **Who reviews manifest *quality*, beyond terms of use?** New, and raised by the authoring agent: nothing
      currently catches a plausible-but-wrong disqualifier rule or a mis-chosen authoritative source. That
      failure is silent and produces a credible list of the wrong companies — which is E10 exactly.
- [ ] **Who rewrites the opener, and by when?** Blocks the email leg regardless of architecture.
- [x] **New repo, or a slice inside an existing `base-*` project?** **Decided 2026-09-26: new repo.** The
      service is built in `my-example-app/` under `app/`, not as a slice inside a `base-*` project —
      different domain, different lifecycle. Recorded in `CLAUDE.md` → *Architecture map*.

---

*Intent: [`local-prospect-engine.prd.md`](./local-prospect-engine.prd.md) · Next: `piv-slice-epic` to break this into tickets.*
