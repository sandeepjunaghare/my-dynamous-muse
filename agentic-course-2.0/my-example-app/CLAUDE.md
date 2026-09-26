# CLAUDE.md — Local Prospect Engine

## What this is
An internal GTM tool for Compumatrice: take a brief (vertical · ICP band · geography) and produce a
qualified, route-clustered prospect list in HubSpot with the three-touch cadence already scheduled — so the
founders' ~4 hrs/week of new-business time goes into conversations instead of Google Maps and a spreadsheet.
One user, one metro (DFW), one weekly run plus ad-hoc. Stack, inherited wholesale from the `base-research-agent`
template: **FastAPI · Claude Agent SDK (`ClaudeSDKClient`) · Python 3.12 · Pydantic v2 · Supabase/Postgres ·
structlog · uv · pytest · ruff · MyPy + Pyright strict · Docker Compose · vertical slice architecture.**
Internal tool, never a product — PRD §8 Non-goals (multi-tenancy, inbound, multi-metro, autonomous
calling/knocking) are closed, not "later".

## Architecture map
**Today** — no application code yet; this repo is the AI layer plus the specs.
```
docs/local-prospect-engine.prd.md           # intent: problem, evidence E1–E20, MVP, metrics M1–M9
docs/local-prospect-engine.architecture.md  # the how: decisions, spikes, missing pieces, open questions
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
  manifests/       # vertical_manifest — the M9 lever
  sourcing/        # brief → sourcing_run → candidate, every field carrying its own provenance
  qualification/   # disqualifier rules + Intensity×Automatable score; owns `disqualification`
  routing/         # DFW geographic route clustering
  promotion/       # the HubSpot write-gate: dedupe, create, schedule cadence; owns `promotion`
  tools/           # the five generic MCP tools, all manifest-parameterized
```

## Ground rules
- **Provenance is a write-gate.** Every prospect field carries source URL, retrieval timestamp and method;
  a field without provenance cannot be written to HubSpot (E18 → M6/M8).
- **Vertical is data, not code** — it lives in a versioned `vertical_manifest` row, never in a slice or an
  `if vertical ==`. Adding one → `.claude/references/adding-a-vertical.md`.
- **Split store:** Supabase is machine state and never human-edited; HubSpot holds everything a person reads
  or edits, and a candidate gets there only when it qualifies **and** a human accepts it — no shadow CRM, no
  spreadsheet (M4). Boundary details → `.claude/references/hubspot-integration.md`.
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
- **Spike 1 gates the first slice.** E15: 22 HubSpot follow-up tasks, 0 completed, 11 past due. Until that
  spike resolves, do not assume sourcing is the bottleneck — if the queue isn't worked, the first slice is
  cadence enforcement, not sourcing.
- **Never silently answer an open question.** Architecture doc → *Open questions*, PRD §9. If a decision
  you're about to make sits on those lists unanswered, ask.

## Commands
Nothing is scaffolded yet — no `pyproject.toml`. Decided toolchain, to be wired at the first slice:
`uv sync` · `uv run pytest` · `uv run mypy . && uv run pyright` · `uv run ruff check .` ·
`uv run uvicorn app.main:app --reload` · everything at once: `/piv-validate`

## On-demand context
- Adding a vertical, source or tool → `.claude/references/adding-a-vertical.md`
- HubSpot writes, the Supabase↔HubSpot boundary, the cadence → `.claude/references/hubspot-integration.md`
- Generating outreach copy → `.claude/references/outreach-messaging.md` (never lead with AI, E16)
- Slice layout in detail → `.claude/references/vertical-slice-architecture.md`
