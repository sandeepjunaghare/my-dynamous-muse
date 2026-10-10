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
**Today** — Waves 1–3 have shipped (T1, T2, T3, T4, T11, T12), and T13 from Wave 4: the service boots,
validates clean, the provenance primitive exists, and five slices are built — `manifests/` with its authoring
agent, the gateway half of `promotion/`, the `sourcing/` data model, and the `cadence/` state machine with
adoption.
```
app/main.py                                 # FastAPI + lifespan, middleware, error handlers, GET /health
app/core/                                   # config · logging · database · exceptions · cost · middleware · dependencies
app/shared/provenance.py                    # ProvenancedValue[T] + is_promotable() — the write-gate as a type
app/manifests/                              # vertical_manifest DRAFT→ACTIVE, the terms gate, read-only routes, the CLI, the authoring agent (T12)
app/promotion/                              # the HubSpot gateway: client, dedupe, idempotent custom properties, task/activity reads
app/sourcing/                               # sourcing_run + candidate, field-level provenance, one owning stage per field (stages.py)
app/cadence/                                # the three-touch schedule (no outcomes), D5 done-signal sync, overdue view, adoption (T13), routes + CLI
app/tools/registry.py                       # the Stage contract; a stage registers by being app/tools/<PipelineStage>.py exporting STAGE
app/cli.py                                  # the `lpe` entry point; each slice registers its command group
alembic/                                    # async env.py + 0001_baseline · 0002 vertical_manifest · 0003 freight/fire seeds · 0004 sourcing · 0005 cadence
tests/                                      # mirrors app/, plus the structure guards that keep decisions decided
docs/local-prospect-engine.prd.md           # intent: problem, evidence E1–E20, MVP, metrics M1–M9
docs/local-prospect-engine.architecture.md  # the how: decisions, spikes, missing pieces, open questions
docs/tickets/local-prospect-engine.md       # the MVP sliced into tickets, with waves and gates
tooling/mcp/codebase_search.py              # codebase-search MCP server, wired in .mcp.json
```
**Planned** — decided, not built. **This folder (`my-example-app/`) is the root**: the service is built here
under `app/`, not as a slice inside a `base-*` project. (Git's toplevel is the parent `my-dynamous-muse`;
this folder is tracked inside it.) Slices map 1:1 to the decided data model and tool set; confirm names as
each one lands. `core/` and `shared/` above are their built counterparts — the rules on them still hold:
infra that predates any feature, and only what 3+ slices need, duplicating until the third consumer.
```
app/
  sourcing/        # the pipeline runner and registry search (T5) — the data model above is built
  qualification/   # disqualifier rules + Intensity×Automatable score; owns `disqualification`
  routing/         # DFW geographic route clustering
  promotion/       # create Company/Contact, own the `promotion` ledger (T9) — the gateway above is built
  tools/           # the stage modules themselves (T5–T8), one file per PipelineStage — the registry above is built
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
- **Google Places is a check, never a source** (D13, its terms forbid storage). Look up only within the run;
  use the response and discard it; keep only the place ID. Places never discovers prospects. Stored address
  and phone come from the registry, and coordinates from the US Census Geocoder.
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
- **Free before paid; cheaper paid before dearer paid** (D8). The registry pull, disqualifier rules,
  QCMobile and the revocations join are free and run first. Then Places (~$0.03 a candidate), then **one**
  judgment call per candidate (~$0.08). **The run works a backlog in weekly batches** (D12): the free filters
  persist the whole pool (4,449 for freight v3), and each run takes a fixed batch (start 150) through the paid
  steps, so the batch size sets the bill. Paid calls are capped per run (500 Places calls) as a circuit breaker
  against a runaway loop, not as a budget target. Per-run cost is logged from T1.
- **Never silently answer an open question.** Architecture doc → *Open questions*, PRD §9. If a decision
  you're about to make sits on those lists unanswered, ask.

## Commands
Wired at T1 and green:
`uv sync` · `uv run pytest` · `uv run mypy . && uv run pyright` · `uv run ruff check .` ·
`uv run uvicorn app.main:app --reload` · everything at once: `/piv-validate`

Manifests are reviewed on the CLI — no frontend, no second login. Built at T2 and working:
`uv run lpe manifest list` · `uv run lpe manifest show <id>` ·
`uv run lpe manifest activate <id> --accept-terms <sources>` (where the terms-of-use decision is recorded).
`uv run lpe manifest propose "<brief>" [--vertical <slug>]` (T12) writes a cited **DRAFT** only; it never
activates and never answers terms of use. Manifest *quality* review is still an open question — no gate.

The cadence is synced **daily by an external launchd job**, not by the weekly run (T11):
`uv run lpe cadence sync [--dry-run]` · `uv run lpe cadence park <contact>` · `uv run lpe cadence overdue`
(also `POST /cadence/sync`, `GET /cadence/overdue`). `park` is final, and leaves the open task for you to close.
`uv run lpe cadence adopt --dry-run` · `uv run lpe cadence adopt --roster <file> [--dry-run]` (T13; no
plain enrol command by design — adoption reconstructs, and T9 enrols in code).

Database-backed tests need a throwaway Postgres and skip without one:
`docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16` ·
`export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres`

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
