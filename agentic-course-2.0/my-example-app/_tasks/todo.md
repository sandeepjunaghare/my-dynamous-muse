# Docs realignment — Local Prospect Engine

**Date:** 2026-09-26 · **Branch:** `docs/local-prospect-engine-tickets` · **Status:** done 2026-09-26 — docs realigned, committed
**Scope:** docs only — no application code. The specs now disagree with decisions taken in session; this
brings them back into line and re-slices the tickets that moved.

---

## Decisions taken this session

These were open (or decided differently) in the docs. Recorded here so the next session plans against them.

| # | Decision | Replaces |
|---|---|---|
| D1 | **Deterministic weekly pipeline**, LLM only at `resolve_owner` and `classify_rollup` | architecture → *Recommended approach* ("a Claude Agent SDK loop calls five generic MCP tools") |
| D2 | **Agent SDK used for the two judgment nodes + manifest authoring**, not run-time orchestration | CLAUDE.md stack line |
| D3 | **Manifest-authoring agent is in the MVP** — proposes a DRAFT `vertical_manifest` row, founder activates | new scope, not in T1–T10 |
| D4 | **Our service owns cadence schedule; HubSpot owns outcomes.** Tasks remain the human surface | architecture → *Cadence stays HubSpot's*; "task state is not mirrored into Supabase" |
| D5 | **Done signal = Task complete OR matching activity logged after task creation** (ties break toward done) | new — no rule existed |
| D6 | **`app/cadence/` becomes its own slice** (state machine · scheduling · outcome sync · adoption). T9 narrows to write-gate + Company/Contact create | CLAUDE.md architecture map; T9 scope |
| D7 | **Vertical #1 = Freight & 3PL** | PRD §9 open question ("Founder's call") |
| D8 | **Spike 1 runs as specified; T1/T2/T3 built alongside.** After it ends, surviving prospects of the 22 are **adopted** into the cadence machine | ticket doc → *The Spike 1 fork* |
| D9 | **No budget ceiling — measure first**, with a hard circuit breaker of 500 Places calls/run. Cost instrumentation moves to T1 | PRD "Budget: none stated"; architecture open question |
| D10 | **Qualification runs before verification** — spend nothing until the free filters have run | new — pipeline ordering |
| D11 | **Hosted Supabase, two free projects (dev + prod). Local first, small VPS later.** Compose drops its Postgres service; scheduling stays external | undecided |
| D12 | **Draft manifests reviewed via CLI** (`propose` / `show` / `activate --accept-terms`); `activate` is where terms-of-use is recorded | undecided |
| D13 | **GATE-A left as-is** — the `$999 assessment` pipeline is not created; T10 reports M3 as *not configured*, never as `0` | ticket doc → *Gates & open decisions* |
| D14 | **Review surface stays the HubSpot list view**, revisited if it hurts | unchanged — confirmed |

**Evidence gathered:** SPIKE-3 is answered — portal 244766495 offers seats `core`, `sales-starter`,
`service-starter`, `view-only`; no professional seat, so **no sequences**, and Starter workflows are capped
(~10 actions, one workflow per trigger, no branching). This is what forced D4. FMCSA QCMobile is free, keyed,
and public domain (CC PDM 1.0); it is a **lookup** API, not a search API — geography comes from the bulk
Company Census File.

---

## Tasks

### 1. `CLAUDE.md`
- [x] Stack line: Agent SDK scoped to judgment nodes + manifest authoring, not run-time orchestration (D1, D2)
- [x] Replace the *"cadence is HubSpot's"* ground rule with the schedule/outcome split (D4) and the done-signal rule (D5)
- [x] Architecture map: add `app/cadence/`; restate `app/tools/` as pipeline stages; narrow `promotion/` (D6)
- [x] Note hosted Supabase, dev/prod projects, local-then-VPS, external scheduling (D11)
- [x] Keep the "deliberately left out" list intact — Redis, Langfuse, Celery, frontend, auth, RLS all still out

### 2. `docs/local-prospect-engine.architecture.md`
- [x] Rewrite *Recommended approach* for the deterministic pipeline (D1, D10), keeping the manifest abstraction
- [x] Rewrite *Cadence stays HubSpot's* → the split (D4), with the Starter-tier evidence as the reason
- [x] Record **SPIKE-3 as answered** with the seat-list evidence; state the consequence
- [x] Widen **SPIKE-2** to test headcount as well as named principal — FMCSA's power-unit/driver fields describe carriers, not brokers, so a non-asset brokerage shows near-zero regardless of office size; expect the headcount half to fail
- [x] Rewrite the FMCSA contract: bulk census file for geography, QCMobile for per-record detail
- [x] Close settled open questions: Redis (out), terms-of-use (recorded at manifest `activate`, D12), Places cost (circuit breaker not ceiling, D9)
- [x] Add the new open question raised by D3: who owns manifest quality review beyond terms-of-use

### 3. `docs/tickets/local-prospect-engine.md`
- [x] Assumptions table → decisions table; A1/A2 resolved by D8/D7
- [x] **T1** — add per-run cost instrumentation; drop Postgres from compose; point at hosted Supabase (D9, D11)
- [x] **T2** — add draft lifecycle (DRAFT → ACTIVE), write path, and the CLI (D12); keep the no-`if vertical ==` test and the terms-of-use activation gate
- [x] **T5** — rescope: pipeline stages, not an agent loop; census-file-first FMCSA adapter (D1)
- [x] **T6** — ordering after qualification; 500-call circuit breaker replaces the unstated budget (D9, D10)
- [x] **NEW ticket** — manifest-authoring agent slice (D3)
- [x] **NEW ticket** — `app/cadence/` slice: state machine, scheduling, outcome sync (D6); depends only on T1 + T3
- [x] **T9** — narrow to write-gate + Company/Contact create; note the gate governs prospect field writes, not task creation
- [x] **T10** — M3 renders *not configured*, never `0` (D13)
- [x] **T-ALT1** — rescope as post-Spike-1 **adoption**: adopted candidates carry honest provenance (`retrieval_method = "manual_hubspot_entry"`, source = HubSpot record URL, `retrieved_at` = record create date); cycle position reconstructed from logged activity, not reset (D8)
- [x] Redraw the dependency graph and the wave table for the new slices

### 4. Ship
- [x] Re-read all three files together for internal consistency
- [x] Commit per `.claude/references/conventions.md` — conventional tag, no AI attribution

---

## Deferred / still open

- **FMCSA webkey** — free, ~2 min; not needed if SPIKE-2 runs against the census file
- **Google Places key** — needs a GCP project with billing; not needed until T6 (fixtures until then)
- **Places pricing** — verify actual per-call cost when planning T6; the ~$20–30/month estimate is unverified
- **Who rewrites the opener** (E16) — blocks the email leg, not any ticket; nothing sends until Spike 4
- **PRD §9 questions left untouched** — conversation-target conflict, niche-selection framing, whether the
  $999 offer converts, sending domain, compliance posture, dormant network, door-to-conversation rate

---

## Review

**What worked.** Checking the HubSpot portal directly answered SPIKE-3 in one read-only call instead of the
budgeted hour — and the answer (Starter, no sequences) reversed an architecture decision before any code was
written against it. Cheap evidence beat a planned spike.

**What we added mid-pass.** Two files were not on the original list and turned out to contradict the
decisions outright: `.claude/references/hubspot-integration.md` (said "the cadence is HubSpot's") and
`.claude/references/adding-a-vertical.md` (said "the agent loop calls five generic tools"). Both are
on-demand context a future session loads by name, so stale text there is worse than stale text in a spec.
Two PRD §9 open questions were also marked decided. Lesson: when reversing a decision, grep the whole AI
layer for the phrase, not just the spec that owns it.

**What to improve.** The ticket file needed a wholesale rewrite rather than edits, because the wave order,
dependency graph and ticket numbering all moved together. Re-slicing is cheap while nothing is built; it
would not have been cheap two waves in. Worth re-running `/rules-check-drift` once `app/` actually exists.

**Still unresolved and deliberately so.** Who reviews manifest *quality* beyond terms of use (T12 can propose
a plausible-but-wrong source — E10 all over again); Google Places terms and real pricing; whether to keep
Langfuse. All recorded in the architecture doc's open questions rather than guessed at.

---

# T1 — Project scaffold, core infrastructure, provenance primitive

**Date:** 2026-09-26 · **Branch:** `docs/local-prospect-engine-tickets` · **Status:** done — all gates green
**Plan:** `.claude/plans/t1-scaffold-core-provenance.md` · **Report:** `.claude/reports/t1-scaffold-core-provenance-report.md`

## Tasks

- [x] `pyproject.toml` + `.python-version` — uv project, ruff · MyPy strict · Pyright strict · pytest
- [x] `app/shared/provenance.py` — `ProvenancedValue[T]`, `RetrievalMethod`, `is_promotable()`
- [x] `app/core/config.py` — pydantic-settings, strict 12-factor, required fields undefaulted
- [x] `app/core/logging.py` — structlog JSON, correlation id, typed (no `Any`)
- [x] `app/core/exceptions.py` — `LocalProspectEngineError` + three derivations
- [x] `app/core/database.py` — async engine/session, `DeclarativeBase`, lazily built
- [x] `app/core/cost.py` — `RunCost`, `BillableKind`, `check_cap`
- [x] `app/core/middleware.py` + `app/core/dependencies.py`
- [x] `app/main.py` — lifespan, middleware, centralized handlers, `GET /health`
- [x] `alembic/` async `env.py` + `0001_baseline` (no domain tables)
- [x] `docker-compose.yml` (app only) + `Dockerfile` + `.env.example`
- [x] Tests: config · logging · cost · provenance · health · structure — 68 passing
- [x] Full validation: ruff, ruff format, mypy, pyright, pytest — all green, zero suppressions

## Review

**What worked.** Building the toolchain first and proving it green on an empty project caught the
one configuration error that would otherwise have surfaced as ~50 bogus type errors at the end:
pyright was resolving against the system interpreter rather than `.venv`, so every third-party
import read as unknown. Five minutes at Phase 1; an afternoon at Phase 5.

**Where the plan met reality.** Two of its prescriptions needed checking rather than copying, and
both were checked empirically before writing code:

- `Settings.model_validate({})` **does** load the environment (verified), so the plan's
  suppression-free workaround is correct — worth confirming, since `model_validate` bypasses
  `__init__` and it was not obvious that pydantic-settings' sources still run.
- `extra="forbid"` does **not** reject unknown *process* env vars — only unknown keys in `.env`.
  AC3's "fails loudly on an unknown one" holds for the surface a person actually edits. The test
  and `.env.example` now say precisely that instead of implying more.

**What to improve.** The plan's `Any` guard was specified as a grep for `: Any` / `-> Any`; it
fired on this project's own docstrings, which discuss the rule. Parsing the AST is barely more code
and has no false positives. Worth being the default for "assert an absence" tests — a guard that
cries wolf gets muted, which is worse than not having it.

**Left undone, and why.** The Alembic round-trip was validated against a throwaway local Postgres
container rather than Supabase: assumption #1 of the plan (a reachable Supabase project) is still
unmet. The tooling is proven; the dev and prod projects still need creating before T2.
