# Local Prospect Engine — PRD

**Status:** Draft for challenge · **Date:** 2026-09-26 · **Author:** Sandeep Junaghare (with Claude)
**Type:** Internal tool for Compumatrice GTM. Not a product for sale (see Non-goals).
**Architecture:** [`local-prospect-engine.architecture.md`](./local-prospect-engine.architecture.md) — the *how* this PRD deliberately leaves open.

---

## 1. Problem Statement

Compumatrice's founders have roughly **four hours a week** for new business, and nearly all of it is
spent being the glue between tools: building next week's target list by hand, looking up addresses and
headcounts one company at a time, clustering them into drive routes, and re-typing outcomes into a
spreadsheet that only *sometimes* makes it into HubSpot.

That leaves almost nothing for the work that actually creates revenue: deciding who is worth a visit,
saying something an owner will answer, and following up more than once. The designed sales cadence —
60 dials and 20+ doors per week, three touches then rest — **is not being executed**, because executing
it requires a list that does not exist until a founder sits down and builds it.

**Cost of not solving it:** pipeline arrives in bursts and dies the moment delivery work spikes. Two
weeks into the motion there is **one logged discovery conversation, zero paid assessments, and zero
revenue**. At the current rate, new business is capped by founder calendar availability, which is the
scarcest resource in the company and the one least likely to increase.

> **Note on framing.** This PRD deliberately does not say "build a lead-generation agent." More than
> one solution fits the problem above: an agent, a part-time VA or fractional SDR, an off-the-shelf
> stack wired together, or simply disciplined use of HubSpot's native sequences. The engineering
> answer belongs in the architecture spec, not here.

---

## 2. Evidence

Drawn from `Dropbox/data/CM/AIAsAService/AgentOps/salesandbd/`, the founder interview, and a read-only
pull of HubSpot account 244766495 — all as of 2026-09-26.

| # | Evidence | Source | Strength |
|---|---|---|---|
| E1 | **One** discovery conversation logged (Ramirez Injury Law, 2026-09-15). Intensity 5, Priority Score 20, $62k/yr impact cited, $1,500 audit scoped and sent 2026-09-19 — no reply recorded. **This record does not exist in HubSpot** (see E14). | `DFW-Discovery-TrackerSept-2026.xlsx` → Conversation Log, Pipeline | **Hard** |
| E2 | **Audits sold: 0 · Audit revenue: $0 · Builds won: 0 · Live MRR: $0.** | Same, Pipeline → Totals | **Hard** |
| E3 | The weekly cadence is designed for **60 dials + 20+ doors/week**, predicting "3–4 owner conversations" per door outing across two outings — i.e. **6–8 conversations/week by design**. Actual: ~0.5/week. The design is not the constraint; execution is. | `prepdocs/weeklysalescadence.html` | **Hard** |
| E4 | List building is an explicit manual weekly task: *"Monday PM — Build the week's door list. Two geographic clusters, 20 names."* | Same | **Hard** |
| E5 | Enrichment is manual and incomplete. 14 companies sit under *"Candidates needing address lookup"* and 12 under *"Still to verify"*, with the next action *"Verify addresses and employee counts — Google Maps and LinkedIn headcount."* | `prepdocs/targetlist-DFWfireprotectiondoorknocks.html` | **Hard** |
| E6 | Double entry is codified in the playbook itself: track in a spreadsheet, *"Log in HubSpot immediately after the call."* Founder reports it reaches HubSpot only *"sometimes."* Confirmed measurably in E14. | `evernote-prospect.html` + interview | **Hard** |
| E7 | Disqualification rules already exist and are applied by hand: *"Do NOT knock — national rollups, no local owner"* (Impact Fire, Summit Fire, Century Fire, Control Systems); *"Avoid hourly-billing practices"*; several targets flagged *"likely too big."* | Target list + `discovery-guide-sept2026.md` | **Hard** |
| E8 | A messaging failure is already documented: Eagle One Fire Systems, 2026-09-18 — *"They do not do AI."* Encoded lesson: **never lead with AI.** | `door-script-sprinkler.html` | **Hard** |
| E9 | A qualification model already exists: **Intensity (1–5) × Automatable (1–5) = Priority Score (1–25)**; ≥16 is live, <9 is dead. | Discovery tracker → Start Here | **Hard** |
| E10 | Sourcing for the top vertical has already misfired: the freight folder's prospect file (`boc.csv`, 74 rows) is a list of **BOC-3 blanket process agents**, not freight brokers. | `FreightBrokerage-3pl/boc.csv` | **Hard** |
| E11 | Founder time available: ~4 hrs/week; no additional headcount. Recovery from a dry spell: ~2 weeks. | Interview | Self-reported |
| E12 | Whether the $999 assessment converts at all. | — | **Assumption — validate via the MVP itself; zero sold to date (E2)** |
| E13 | That buyers in these verticals will respond to cold email at a rate worth automating. | — | **Assumption — no email outreach data exists yet; confirmed in E17, the email leg has never run** |
| E14 | **The two systems are disjoint, each holding what the other lacks.** HubSpot holds 22 outreach touches from 2026-09-18 to 09-24 across the fire-protection vertical — none of which appear in the discovery tracker. The tracker holds the Ramirez conversation and the only live $1,500 audit — which does not appear in HubSpot. | HubSpot NOTE objects + tracker | **Hard** |
| E15 | **Follow-through has a measured 0% completion rate.** 22 real follow-up tasks exist in HubSpot; **every one is NOT_STARTED**, and 11 are already past due (3 due 09-23, 8 due 09-25). Zero completed tasks, zero logged meetings. | HubSpot TASK objects | **Hard** |
| E16 | **The AI opener is a repeating, self-inflicted failure, not a one-off.** Three separate rejections cite AI: Eagle One *"they do not do AI"* (09-18); *"They already have AI and they are happy with it"* (09-22); Crisp-LaDew *"they have their own AI and do not need help"* (09-22). Yet two HubSpot notes record the founder introducing himself as *"local AI expert for Fire and Alarms company"* — directly against the playbook's own rule. | HubSpot NOTE objects vs. `evernote-prospect.html` | **Hard** |
| E17 | **The three-touch cadence is really two touches.** Of ~22 dials and 3 door knocks, there are zero logged emails and zero meetings. The only CALL and MEETING records in the portal are HubSpot's seeded sample data. | HubSpot CALL / EMAIL / MEETING_EVENT | **Hard** |
| E18 | **Enrichment quality is measurably poor at the point of list creation.** Of 34 fire-vertical contacts created 09-22 to 09-25: 14 carry the company name in the first-name field (no person identified at all), 11 reach owner/principal level, and **0 of 23 companies have a headcount** — the single field the ICP filter depends on. City/state is populated on 7 of 23. | HubSpot CONTACT / COMPANY | **Hard** |
| E19 | **100% of the last week's effort went into the vertical the playbook calls practice-only.** Every touch 09-18 to 09-24 was fire and sprinkler — which `weeklysalescadence.html` designates *"practice reps only… good for fluency, bad for volume."* Freight, ranked top, has zero activity. | HubSpot + playbook | **Hard** |
| E20 | **Four verticals of material already exist, at two levels of readiness.** *Discovery-ready* (vocabulary, questions, disqualifiers): PI/Immigration Law, Freight & 3PL, Medical Billing/RCM. *Delivery-ready* (assessment handbook, build playbook, concierge playbook, one-pager): Freight & 3PL and Fire Protection. **Freight is the only vertical complete at both levels — and has had zero outreach.** | `discovery-guide-sept2026.md`, `FreightBrokerage-3pl/`, `FireAlarmSprinkers/` | **Hard** |

---

## 3. Thesis — why build it, and why now

**Why this.** Compumatrice's buyers are *owner-operated DFW businesses of 20–200 people* — fire and
sprinkler shops on Olympic Drive, freight brokers, collision centers, specialty contractors, medical
billing firms. These people are largely invisible on LinkedIn and thinly and unreliably covered by
Apollo-style B2B databases. The authoritative records for them are public but scattered: state
licensing registries, FMCSA/SAFER authority records, county and Secretary of State filings, Google
Maps, trade associations. **Nobody has assembled those into a qualified, route-clustered door list,
because doing it by hand is exactly the four hours a week the founders don't have.**

**Why it beats how we cope today.** Today's cope is a founder with Google Maps, LinkedIn headcount
lookups, and a spreadsheet. Buying Apollo does not replace that — it is one more login to search,
copy out of, and paste into HubSpot, and its coverage of a 12-person sprinkler shop in Sunnyvale,
Texas is poor. The differentiated claim is not *more contacts*. It is **a weekly, decision-ready,
locally-verified list that already knows the rollups are excluded and the route is clustered** — the
one artifact that turns a designed cadence (E3) into an executed one.

**Why now.** The motion is two weeks old and already stalling at the list-building step (E4, E5).
Encoding it now, while the playbook is fresh and the disqualification rules are explicit (E7, E8, E9),
is far cheaper than retrofitting it onto habits that have hardened. Waiting means either the cadence
quietly dies or a founder's calendar becomes the permanent ceiling on revenue.

---

## 4. Hypothesis

> We believe that **automatically producing a weekly, verified, route-clustered prospect list for a
> chosen DFW vertical — owner named, headcount confirmed, national rollups excluded, landed directly in
> HubSpot with the three-touch cadence already scheduled** — will cause **Compumatrice's founders** to
> **actually run the cadence they designed**, resulting in **6 decision-maker conversations per week and
> 2 paid $999 assessments per month**.
>
> **We'll know we're RIGHT if,** within **60 days**: founder time spent on list building, enrichment and
> logging drops below **1 hour/week**, *and* decision-maker conversations reach **≥4/week for three
> consecutive weeks**, *and* every touched prospect exists in HubSpot with no parallel spreadsheet.
>
> **We'll know we're WRONG if,** within **60 days**, any of these hold:
> - Lists are delivered on time but conversations stay **below 3/week** → the constraint was never
>   sourcing, and we automated the wrong step.
> - The founder rejects **≥30%** of agent-sourced prospects as bad targets → qualification judgment
>   (E7, E9) cannot be encoded, and the list creates review work instead of removing it.
> - **Guardrail:** spam-complaint rate exceeds **0.1%**, or sending-domain reputation degrades, or any
>   compliance complaint is received → the autonomous leg is doing damage that outruns its benefit.
> - **Guardrail:** paid assessments remain at **0** while conversations rise → the offer is broken, not
>   the pipeline, and more volume is waste.

---

## 5. Target User & Jobs-to-be-Done

**Primary user:** A Compumatrice founder (Sandeep) running sales part-time between delivery work.
~4 hrs/week. Based in Allen, TX; works the DFW metro. Operates by phone block and door knock, not by
LinkedIn. HubSpot is the system of record; a spreadsheet is the shadow system to be eliminated.

**Trigger:** Monday, when next week's door list has to exist — and the recurring moment pipeline runs
dry and recovery takes two weeks.

**JTBD (primary)**
> When **I have four hours this week for new business and no list to work**, I want to **be handed a
> verified, clustered, owner-named set of prospects already loaded into HubSpot with the next touch
> scheduled**, so I can **spend my four hours in conversations instead of in Google Maps and a
> spreadsheet**.

**JTBD (secondary)**
> When **I've touched a prospect once and delivery work swallows the next three days**, I want **the
> cadence to keep its own state — call, voicemail, same-day email, wait four days, repeat twice, park
> after three cycles** — so that **nobody is contacted once and silently dropped.**

**Non-users — explicitly not for:**
- Enterprise accounts requiring relationship selling
- Inbound leads
- Existing clients and active accounts
- National rollups with no local owner (E7) — actively excluded, not merely deprioritized
- Hourly-billing law practices, where time savings reduce the buyer's own revenue
- Any user outside Compumatrice (see Non-goals)

**Constraints**
- ~4 founder-hours/week; no additional headcount
- HubSpot is the system of record — non-negotiable
- One LinkedIn Sales Navigator seat (a bottleneck, and the wrong channel for this ICP anyway)
- Geography: DFW metro
- **Multiple verticals, pursued in sequence — vertical is an input to the tool, not a fixed assumption
  inside it.** Named so far: **Freight brokers & 3PLs** (ranked first by the playbook, delivery-ready,
  zero outreach to date), **Fire Protection** (delivery-ready, all current activity, designated
  practice-only for volume), **PI/Immigration Law** (discovery-ready, holds the only live audit),
  **Medical Billing/RCM** (discovery-ready). Playbook also names collision centers, specialty
  contractors, staffing agencies and property management as later candidates
- Fire and sprinkler shops of 6–10 people are explicitly *practice reps only* — good for script
  fluency, bad for volume, because $999 is a real decision at that size
- Qualification filter, verbatim from the playbook: *"enough money that $999 is nothing, enough pain to
  sit for an hour — 20–200 employees with an office function"*
- Budget: none stated (open question)

---

## 6. MVP — the thinnest line that proves the hypothesis end to end

**One vertical. One metro. One full cycle, every week.** The founder chose an end-to-end build over a
single-step fix; this is that build kept as thin as it can be while still touching every stage.

1. **Brief in** — founder specifies vertical, ICP band, and geography (e.g. *freight brokers & 3PLs,
   20–200 employees, DFW*).
2. **Source** — the agent assembles candidates from public, authoritative records appropriate to the
   vertical rather than from a single B2B database. Apollo-style sources are one input, not the spine.
3. **Verify & enrich** — address, phone, headcount band, owner or principal name, years in business,
   the operating system they run on where discoverable (the TMS/PM question the discovery guide calls
   critical in the first 15 minutes).
4. **Qualify** — apply the existing rules: rollup exclusion (E7), size band, owner-accessibility;
   surface the signals that feed Intensity × Automatable (E9). The agent proposes; the founder is the
   final judge.
5. **Cluster** — group into geographic drive routes, as the target list already does by hand (Route
   Cluster A: Olympic Drive; Route Cluster B: 75238).
6. **Land in HubSpot** — every prospect created there directly, with the three-touch cadence scheduled.
   **No spreadsheet.** This is what turns "sometimes" into "always."
7. **Run the cadence** — call and door touches become founder tasks (they are physically human); the
   email touch is automated. State is kept: three touches, wait four days, repeat twice, park after
   three cycles.
8. **Report** — the four Friday numbers, unchanged from today's playbook: *doors knocked · owner
   conversations · assessments booked · paid assessments sold.*

**Deliberately thin:** one vertical *first*, one metro, one cadence template, no multi-user support, no
productization, and the qualification model stays the existing 1–25 score rather than anything learned.

**Vertical portability — a requirement, not a later phase.** Compumatrice will work several verticals in
sequence (E20), so the MVP proves *one* end to end but must treat vertical as an **input**. Adding the
second vertical should be a configuration exercise, not a rebuild. Four things change per vertical and
must therefore be declarable rather than baked in:

| What changes | Fire Protection | Freight & 3PL |
|---|---|---|
| Authoritative source | Texas State Fire Marshal licensed-contractor registry; Google Maps | FMCSA/SAFER authority records — MC numbers, authority status, fleet size, BOC-3 filings |
| Disqualifier | National rollups with no local owner | Asset-based carriers when the target is non-asset brokerages; double-brokering risk flags |
| Qualifying signal | Owner-accessible; 20–200 with an office function | TMS in use and whether it has an API — the discovery guide calls this critical in the first 15 minutes |
| Opener and vocabulary | Inspection renewals, permits, AHJ follow-ups | Loads, lanes, carrier packets, COIs, check calls, detention |

Note the freight row answers E10 directly: the reason `boc.csv` ended up holding BOC-3 process agents
instead of brokers is that nobody had established FMCSA as the authoritative source. That is precisely
the sourcing judgment the tool is meant to encode.

**Door check**
- **Two-way (reversible — just build):** source selection, scoring rules, HubSpot property shape, the
  briefing interface, cadence templates, clustering logic.
- **One-way (expensive to undo — de-risk first):** sending cold email autonomously from the
  `compumatrice.com` domain. Domain reputation takes months to repair and would damage client and
  delivery mail, not just outreach. Also one-way: any annual data contract, and the compliance posture
  for each source. **Recommend a spike:** separate sending domain, warmed, with volume caps, before any
  autonomous leg goes live.

---

## 7. Success Metrics

| # | Metric | Baseline (2026-09-26) | Target | How measured |
|---|---|---|---|---|
| M1 | Decision-maker conversations per week | **~1.5/week** — 2 real conversations in 4 active days (Jorge Rodriguez, a sales rep; Paul, who declined), from ~22 dials and 3 door knocks | **6/week** | HubSpot logged activity, contact role = owner/principal |
| M2 | Founder hours/week on list building, enrichment and logging | ~4 (the whole allocation, E11) | **< 1/hr week** | Weekly self-log, reviewed Friday |
| M3 | Paid $999 assessments sold per month | **0** (E2) | **2/month by day 90** | HubSpot deal stage = *Assessment sold* |
| M4 | Prospects touched that exist in HubSpot with no shadow spreadsheet | **Two disjoint systems** — 22 touches only in HubSpot, the one live audit only in the tracker (E14) | **100%** | HubSpot record count vs. touches logged |
| M5 | Cadence completion — prospects receiving all three touches in the designed window | **0%** — 22 tasks open, 0 completed, 11 past due (E15) | **≥ 80%** | HubSpot sequence/task completion |
| M6 | Qualification precision — sourced prospects with a named decision-maker | **32%** reach owner/principal; 41% have no person at all (E18) | **≥ 70%** named; **< 5%** rollup false positives | Founder accept/reject at review |
| M8 | Companies with a headcount populated — the field the ICP filter depends on | **0 of 23** (E18) | **100%** | HubSpot COMPANY property fill rate |
| M9 | Effort to add vertical #2 (the portability test) | n/a — fire protection built by hand | **< 1 founder-day**, no rebuild | Time from "target freight" to first usable freight list |
| M7 | Time from brief to usable list | ~1 evening of founder work (E4) | **< 1 hour, unattended** | Timestamp, brief → HubSpot |

M1 and M3 are the outcome metrics. M2 and M7 are leverage metrics. M6 is the early warning: if it
falls, the WRONG condition in §4 is triggering.

---

## 8. Non-goals

- **Not a product for sale.** Internal tool only. Multi-tenancy, billing, onboarding and support are
  out of scope until the internal hypothesis proves out. Revisit only after M1 and M3 are met.
- **Not LinkedIn automation.** Scraping or automating Sales Navigator violates its terms and risks the
  one seat Compumatrice has — and this ICP is not meaningfully on LinkedIn regardless.
- **Not inbound.** No website capture, forms, ads or content.
- **Not existing clients or enterprise accounts.**
- **Not a HubSpot replacement.** HubSpot stays the system of record; this feeds it.
- **Not autonomous calling or door knocking.** Two of the three touches are inherently human; the
  agent schedules and prepares them, it does not perform them.
- **Not automated meeting booking or proposal generation.**
- **Not multi-metro.** DFW only for the MVP.
- **Not a replacement for founder judgment on qualification.** The agent proposes and filters; the
  founder decides.

---

## 9. Open Questions

- [ ] **Conversation target conflicts across our own documents.** `weeklysalescadence.html` states *"30
      decision-maker conversations per week"*; the discovery tracker states *30 conversations total*
      across three verticals for niche selection; the founder stated *6/week*. This PRD uses **6/week**.
      Which is the real commitment?
- [ ] **Which vertical does the MVP run *first*?** Several will be pursued; the question is sequence,
      not exclusivity. **Recommendation: Freight & 3PL** — it is ranked first by the playbook, it is the
      only vertical complete at both discovery and delivery level (E20), FMCSA gives it the strongest
      public data source of the four, and it has had zero outreach so there is no sunk effort to abandon.
      Against that: all current momentum and all 34 live contacts are in fire protection, and the only
      live audit is in Law. Fire's own designation as *practice-only* argues it should not be the
      volume vertical. **Founder's call.**
- [ ] **Does the niche still need choosing at all?** The discovery tracker's stated purpose is to pick
      one niche from 30 conversations and it is 1 conversation in. Pursuing several verticals in
      sequence is a different strategy from choosing one. Which is it — and if it's the former, does the
      tracker's "choose a niche" framing get retired?
- [ ] **Is the bottleneck sourcing at all, or discipline?** E15 is the uncomfortable finding: 22 follow-up
      tasks sit open in HubSpot with 0 completed and 11 past due. An agent that produces more prospects
      adds rows to a queue nobody is working. Does the MVP need to start by closing the loop on tasks
      that already exist, before sourcing new ones?
- [ ] **Fix the opener before scaling it.** E16 shows three AI-based rejections alongside two notes where
      the opener was *"local AI expert"* — against the playbook's own rule. Automating outreach that
      leads with AI would industrialize a known failure. Who rewrites the opener, and by when?
- [ ] **Does the $999 assessment actually convert?** Zero sold (E2). If the offer is broken, more
      conversations produce more nothing. Should the MVP run behind a manual test of the offer first?
- [ ] **Sending domain.** Separate domain, or `compumatrice.com`? This is the one-way door in §6.
- [ ] **Compliance posture.** CAN-SPAM applies to the email leg (identification, physical address,
      functioning opt-out); Texas state law governs the calling leg, including DNC scrubbing. Public-
      record sources each carry their own terms of use. Who owns this decision, and by when?
- [ ] **Autonomy, revisited.** Fully autonomous cadence was chosen, but two of the three touches are
      physically human. Does "autonomous" therefore mean only the email leg, or should the cadence
      escalate differently when a human touch is skipped?
- [ ] **Budget ceiling** for data sources and tooling — none stated.
- [ ] **The dormant network.** `CompuMatrice_Past_Customers_Sept2026.xlsx` holds prior healthcare
      relationships (Texas Health Resources, JPS Health, 2012–2014), mostly marked *Weak* and *Unknown*.
      Warm-but-cold beats cold. Is reactivation in scope, or a separate play?
- [ ] **What is the actual door-to-conversation rate?** The cadence assumes 3–4 owner conversations per
      10–12 doors (E3). That number is a plan, not an observation. M1 depends on it.

---

## Appendix — messaging constraints carried forward from the playbook

These are product constraints, not engineering ones, and any generated outreach must respect them:

- **Never lead with AI.** Three documented rejections, not one: Eagle One (09-18), *"already have AI and
  happy with it"* (09-22), Crisp-LaDew (09-22). The CRM also records the opener *"local AI expert for Fire
  and Alarms company"* being used twice against this very rule (E16). Lead with the friction.
- **Do not pitch during discovery.** *"The moment you pitch, you stop learning and you lose the referral."*
- **Capture their exact words**, not a paraphrase — their phrasing becomes the outreach copy.
- **Always get a number.** Hours, dollars, headcount or percentage. *"Without a number you have an
  anecdote, not an opportunity."*
- **Price is spoken, not written:** *"nine hundred ninety-nine dollars"*, never *"nine ninety-nine."*
