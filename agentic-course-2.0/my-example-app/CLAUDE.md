# CLAUDE.md — Local Prospect Engine

## What this is
An internal GTM tool for Compumatrice: take a brief (vertical · ICP band · geography) and produce a
qualified, route-clustered prospect list in HubSpot with the three-touch cadence already scheduled — so the
founders' ~4 hrs/week of new-business time goes into conversations instead of Google Maps and a spreadsheet.
One user, one metro (DFW), one weekly run plus ad-hoc. Stack, inherited wholesale from the `base-research-agent`
template: **FastAPI · Claude Agent SDK · Python 3.12 · Pydantic v2 · Supabase/Postgres (hosted) ·
structlog · uv · pytest · ruff · MyPy + Pyright strict · Docker Compose · vertical slice architecture.**
Internal tool, never a product — PRD §8 Non-goals (multi-tenancy, inbound, multi-metro, autonomous
calling/knocking) are closed, not "later".

**The weekly run is a deterministic pipeline, not an agent loop.** The Agent SDK is called at exactly two
judgment nodes — `resolve_owner` and `classify_rollup` — and by the manifest-authoring agent. Everything
else is ordinary async Python: same input, same output, every step testable against fixtures. Three of the
five stages (registry search, business verification, route clustering) never needed judgment in the first
place, and clustering and promotion are required to be reproducible.

## Architecture map
**Today** — no application code yet; this repo is the AI layer plus the specs.
```
docs/local-prospect-engine.prd.md           # intent: problem, evidence E1–E20, MVP, metrics M1–M9
docs/local-prospect-engine.architecture.md  # the how: decisions, spikes, missing pieces, open questions
docs/tickets/local-prospect-engine.md       # the MVP sliced into tickets, with waves and gates
tooling/mcp/codebase_search.py              # codebase-search MCP server, wired in .mcp.json
```
**Planned** — decided, not built. **This folder (`my-example-app/`) is the root**: the service is built here
under `app/`, not as a slice inside a `base-*` project. (Git's toplevel is the parent `my-dynamous-muse` and
nothing here is tracked yet.) Slices map 1:1 to the decided data model and tool set; confirm names at the
first slice.
```
app/
  main.py          # FastAPI app + lifespan; routers mounted here; the weekly-run trigger endpoint
  core/            # config · logging · database · exceptions · dependencies — infra that predates any feature
  shared/          # only what 3+ slices need; until the third consumer, duplicate
  manifests/       # vertical_manifest (DRAFT → ACTIVE) + the authoring agent — the M9 lever
  sourcing/        # brief → sourcing_run → candidate, every field carrying its own provenance
  qualification/   # disqualifier rules + Intensity×Automatable score; owns `disqualification`
  routing/         # DFW geographic route clustering
  promotion/       # the HubSpot write-gate: dedupe, create Company/Contact; owns `promotion`
  cadence/         # the three-touch state machine, task scheduling, outcome sync, adoption
  tools/           # pipeline stages, all manifest-parameterized
```
`cadence/` is a domain, not promotion's back half: our HubSpot tier has no sequences (see *Ground rules*), and
adoption gives it a consumer with nothing to do with sourcing. It depends only on `core/` and the HubSpot
client, so it builds in parallel with the whole sourcing line.

## Ground rules
- **Provenance is a write-gate.** Every prospect field carries source URL, retrieval timestamp and method;
  a field without provenance cannot be written to HubSpot (E18 → M6/M8). **The gate governs prospect field
  writes, not task creation** — otherwise adopting the 22 existing hand-typed prospects would be refused
  outright. Adopted records get honest provenance (`retrieval_method = "manual_hubspot_entry"`), not an
  exemption: marking data unverifiable is as much the primitive's job as certifying it.
- **Vertical is data, not code** — it lives in a versioned `vertical_manifest` row, never in a slice or an
  `if vertical ==`. Adding one → `.claude/references/adding-a-vertical.md`.
- **Split store:** Supabase is machine state and never human-edited; HubSpot holds everything a person reads
  or edits, and a candidate gets there only when it qualifies **and** a human accepts it — no shadow CRM, no
  spreadsheet (M4). Boundary details → `.claude/references/hubspot-integration.md`.
- **We own the cadence schedule; HubSpot owns outcomes.** The portal is Sales Hub **Starter** — no sequences,
  and workflows are capped at ~10 actions with no branching — so there is no HubSpot scheduler to hand the
  three-touch state machine to. Supabase holds *which touch is due, which cycle we're in, parked or live*;
  HubSpot holds *whether it happened and what was said*. One source of truth per fact, never two copies of
  one fact. Tasks remain the human surface; outcomes are never stored on our side.
- **A touch is done when its Task is complete OR a matching activity was logged after the task was created.**
  Ties break toward done — the machine must never nag about something already handled. Cycle position is
  reconstructed from logged activity, never reset.
- **Nothing sends autonomously** from any domain until Spike 4 closes — the spike is manual, low-volume
  sending from a warmed subdomain while reply and complaint rates are observed (one-way door: domain
  reputation). No autonomous calling or knocking — ever; those two touches are human by design.
- **Types:** MyPy **and** Pyright strict, zero suppressions — no `# type: ignore`, no `Any` escape hatch.
- **Logging:** structlog, event names `domain.component.action_state`.
- **No auth and no Supabase RLS for the MVP** — single internal user; building either now is scaffolding for
  the "product later" path the PRD put in Non-goals.
- **Don't add back what was deliberately left out:** Redis (only if a run-lock is needed), Langfuse,
  Celery/queues, the template's Vite/React frontend — review happens in HubSpot at a `pending review` stage.
- **No AI attribution in commits or PR bodies** — this overrides the tool's default. Full commit / PR /
  review conventions: `.claude/references/conventions.md`.
- **Done = ruff, mypy, pyright and pytest all green.**

## Working principles
- **Spike 1 runs alongside Waves 1–2, and ends in adoption.** E15: 22 HubSpot follow-up tasks, 0 completed,
  11 past due. The spike works those 22 by hand for two weeks; T1, T2 and T3 are built in parallel because
  they are needed under either outcome. When it ends, surviving prospects are **adopted** into the cadence
  machine — so the finding arrives with a lever attached rather than as an observation.
- **Spend nothing until the free filters have run.** Qualification precedes verification: registry pull and
  disqualifier rules are free, Google Places is not. Paid calls are capped per run (500 Places calls) as a
  circuit breaker against a runaway loop — not as a budget target. Per-run cost is logged from T1 so the
  first real run sets the ceiling instead of a guess.
- **Never silently answer an open question.** Architecture doc → *Open questions*, PRD §9. If a decision
  you're about to make sits on those lists unanswered, ask.

## Commands
Nothing is scaffolded yet — no `pyproject.toml`. Decided toolchain, to be wired at the first slice:
`uv sync` · `uv run pytest` · `uv run mypy . && uv run pyright` · `uv run ruff check .` ·
`uv run uvicorn app.main:app --reload` · everything at once: `/piv-validate`

Manifests are reviewed on the CLI — no frontend, no second login:
`uv run lpe manifest propose "<brief>"` · `uv run lpe manifest show <id>` ·
`uv run lpe manifest activate <id> --accept-terms <sources>` (where the terms-of-use decision is recorded).

## Runtime
Local Mac first, small VPS when the weekly run is something you'd miss — so config is strict 12-factor and
the move is env vars only. **Hosted Supabase, two free projects (dev and prod)**; Compose runs the app alone
and carries no Postgres service. Scheduling stays external (launchd now, cron later) against the trigger
endpoint — no in-process scheduler, no Celery.

## On-demand context
- Adding a vertical, source or tool → `.claude/references/adding-a-vertical.md`
- HubSpot writes, the Supabase↔HubSpot boundary, the cadence → `.claude/references/hubspot-integration.md`
- Generating outreach copy → `.claude/references/outreach-messaging.md` (never lead with AI, E16)
- Slice layout in detail → `.claude/references/vertical-slice-architecture.md`
