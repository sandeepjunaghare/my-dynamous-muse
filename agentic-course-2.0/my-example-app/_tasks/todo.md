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

---

## PR #2 review findings — deferred

Fixed in the review-fix pass: #1 (shallow `frozen`), #3 (handler wiring untested), #5 (engine globals
not reset). Full review: `.claude/code-reviews/pr-2-review.md`. The rest are real but deliberately not
in that PR — logged here rather than as GitHub issues, matching how this file already tracks deferred work.

- [ ] **#2 · `source_url` has no URL-shape validation** (`app/shared/provenance.py`). `"n/a"` passes the
      gate. The T1 plan's IMPORTS line named `HttpUrl`; bare `str` was chosen without recording why.
      **Do before T5** — that is the first ticket writing real sourced URLs.
- [ ] **#4 · 422 responses carry no field-level detail** (`app/main.py`). `backend-api-best-practices.md`
      requires it. Every future route inherits the current generic message. **Do before T10** adds the
      first endpoint taking a request body.
- [ ] **#6 · `RunCost.record()` and `check_cap()` are decoupled**, so nothing structurally prevents
      spending without counting. Consider `guard_and_record(...)`. **Do before T6** copies the pattern.
- [ ] **#7 · the `Any` guard misses `cast(Any, ...)` and string annotations**
      (`tests/test_structure.py`). It is the only backstop, since deviation #10 declines mypy's
      `disallow_any_explicit`. Low risk; extend the AST walk when convenient.
- [x] **#8 · `os.environ.setdefault("DATABASE_URL", ...)`** in `tests/conftest.py` won't override an
      exported value, so a developer with a real hosted URL could run tests against real infrastructure.
      ~~Do before T4~~ — **done in the Wave 2 prep pass below**: forking into worktrees copies `.env`
      into each one, which turned this from theoretical into live a ticket early.

---

## D15 — Supabase connection: session pooler, not direct (2026-09-27)

| # | Decision | Replaces |
|---|---|---|
| D15 | **Connect via the pooler in SESSION mode** (`aws-<n>-<region>.pooler.supabase.com:5432`, user `postgres.<project-ref>`). Transaction mode (6543) stays out. | T1 plan assumption #2, "direct connection (5432), not the pooler" |

**Why it changed.** Not a preference — the direct host is unreachable. `db.<ref>.supabase.co` has an AAAA
record and no A record: Supabase made direct connections IPv6-only for projects created after early 2024,
with IPv4 sold as an add-on. On a network without IPv6 it fails at DNS with
`socket.gaierror: nodename nor servname provided`, which looks like a typo and is not one.

**Why session mode specifically.** The original decision existed to protect asyncpg's prepared statements
from PgBouncer. Session mode holds one dedicated server connection for the life of the client session, so
prepared statements survive — the property is preserved, only the hostname and username change. Transaction
mode (6543) is the one that breaks them, and that warning stands unchanged.

**Cost of the change:** documentation only. No application code moved; session mode needs no
`statement_cache_size` workaround.

**Recorded in:** `.env.example`, `app/core/config.py`, `alembic/env.py`, and the T1 plan's AMENDMENTS.

---

# Wave 2 prep — make the repo safe to fork (2026-09-27)

**Branches:** `docs/d15-supabase-session-pooler` (D15 — PR #3, `effd0e2`) · `chore/wave-2-prep`
(the guards — PR #4, `8c2d84b`) · **Status:** done — both merged 2026-09-27, B2 closed 2026-10-01
**Why now:** Wave 2 is T2 ∥ T3 in two worktrees. T1 left four things that fail *identically in both
branches*, which is the worst shape for a merge — each branch fixes them differently and the conflict is
in the fix, not the feature. Cheaper to fix once on the trunk than twice in parallel.

## Tasks

- [x] **B1 · the structure test encoded a snapshot, not its intent.** `test_slices_are_not_scaffolded_
      ahead_of_their_tickets` asserted `present == {"core", "shared"}`. T2 adds `manifests/` and T3 adds
      `promotion/`, so both branches went red on the same line and conflicted on merge. Inverted to assert
      the rule it always meant — *no package under `app/` is empty* — which slices satisfy by having code
      in them. Never needs editing again as slices land. Split the "core and shared exist" half into its
      own test so the two assertions fail for distinguishable reasons.
- [x] **B3 · Alembic single-head guard added** (`test_migrations_have_exactly_one_head`, via
      `ScriptDirectory.get_heads()`). Two branches adding migrations both set
      `down_revision = "0001_baseline"`; git merges that cleanly because the files do not overlap, and the
      break only appears at `alembic upgrade head` — deploy-time damage from a merge-time mistake. Added
      now, while it costs nothing, because Wave 3 (T4 ∥ T11) is where it actually bites.
- [x] **Both guards verified by making them fail**, not by assuming: an empty `app/manifests/` trips B1
      and passes once it holds a module; two probe migrations off `0001_baseline` trip B3. Probes removed.
- [x] **Review finding #8 fixed** — `conftest.py` now assigns `DATABASE_URL` unconditionally instead of
      `setdefault`. Pulled forward from T4 because the worktree setup copies `.env` into every worktree.
- [x] **B4 · lockfile and migration merge policy recorded** in `.claude/references/conventions.md` (new
      `## merge` section): `uv.lock` is regenerated on conflict, never hand-merged; a forked migration
      rebases its `down_revision` rather than reaching for `alembic merge`.
- [x] **`.env` driver prefix fixed** — it read `postgresql://`, which resolves to the sync psycopg
      dialect and makes `create_async_engine` raise *"The asyncio extension requires an async driver"*.
      Now `postgresql+asyncpg://`, matching `.env.example`. Unrelated to the rotation; found while
      checking it.
- [x] **B2 · rotate the password in `.env`** — **done 2026-10-01 by the human.** Verified by connecting:
      `uv run alembic current` reaches hosted dev and reports `0001_baseline`. The URL shape is what D15
      requires — `postgresql+asyncpg`, the pooler host, port 5432 (session mode), user `postgres.<ref>`.
      Wave 3's `/worktree-create` can now copy a live `.env` into each worktree.

- [x] **Dev brought up to head — 2026-10-01.** It sat at `0001_baseline`; `0002_vertical_manifest` and
      `0003_seed_freight_and_fire` are now applied. Verified on dev: revision is `0003` (head),
      `alembic check` reports no pending operations, the `ck_vertical_manifest_status` check constraint
      and all four indexes exist — including `uq_vertical_manifest_one_active_per_vertical` — and
      `lpe manifest list` renders freight and fire, both DRAFT.

      **The amend window is now closed.** Amending `0002` in place during the T2 review (PR #6, round 2)
      was safe because no environment had applied it. That is no longer true. **Any future change to
      `vertical_manifest`'s shape is a new revision, never an amend** — including anything T4 or T11
      discovers it wants.

## Deliberately not done

- **Per-worktree database.** One hosted dev project cannot serve two worktrees running
  `alembic upgrade head` — they fight over `alembic_version`. Decided: a throwaway local Postgres per
  worktree while iterating on migrations, hosted dev reserved for a pre-merge check, one branch at a time.
  No code change; it is a working rule, recorded here so Wave 3 does not rediscover it.
- **Wave 3 held to two worktrees, not three.** The doc's table says T4 ∥ T11 ∥ T12. T4 and T11 both
  migrate and are the first real exercise of the B3 guard; three-way parallelism should not be the
  experiment that also tests an unproven mechanism. Revisit once the guard has caught something real.


# T2 — Vertical manifest slice, the M9 lever (2026-09-26)

**Branch:** `feat/t2-vertical-manifest` · **Plan:** `.claude/plans/t2-vertical-manifest.md` ·
**Report:** `.claude/reports/t2-vertical-manifest-report.md` · **Status:** merged 2026-09-30 —
PR #6 (`74b1e17`), two review rounds, findings fixed first

Vertical became data. `vertical_manifest` carries identity and lifecycle in columns and the cited
content in one JSONB `body`; a manifest is born DRAFT and only a human moves it to ACTIVE on the CLI,
where the per-source terms-of-use decision is recorded.

- [x] `app/manifests/` — `schemas` · `models` · `repository` · `exceptions` · `service` · `routes` ·
      `cli` · README.
- [x] `0002_vertical_manifest` (hand-written — autogenerate omits `postgresql_where`) and `0003`,
      seeding freight and fire fully cited and **both DRAFT**.
- [x] Two guards, deliberately doubled: the terms gate in the service (the good error, naming what is
      missing) and the partial unique index in the database (still true when a future slice writes by a
      path nobody anticipated).
- [x] `lpe manifest list | show | activate --accept-terms`, with `app/cli.py` as the one place a
      deliberate failure becomes an exit code. `propose` is T12's and deliberately absent.
- [x] `RetrievalMethod.manual_research` added — the seeds are hand-read sources, and `web_lookup`
      (defined as a *per-request* lookup) would have been the small lie the primitive exists to prevent.
- [x] No-`if vertical ==` AST guard, proven by making it fail on both `if` and `match`.
- [x] `tests/manifests/` — 6 modules plus shared builders and CLI fixtures: 126 passed / 43 skipped
      with no database, 169 passed against a throwaway Postgres.

## Deviations from the plan

Nine, all recorded with their measurements in the report. The four that outlive the ticket:

| # | Deviation | Why |
|---|---|---|
| 1 | **The partial unique index is declared in the model *and* the migration**, against the plan's instruction. | The plan's two requirements contradicted: with the index only in the migration, `alembic check` reports it as *removed*, so the next `--autogenerate` would silently emit a `drop_index` for the one invariant this slice exists to guarantee. |
| 2 | **`register(parser)`, not `register(subparsers)`.** | Annotating a subparsers action means naming `argparse._SubParsersAction` — private usage under Pyright strict, and no suppression is allowed. The CLI surface is identical. |
| 3 | **The CLI logs to stderr**, not in the plan. | A service's stdout is its log; a CLI's stdout *is* the review surface, and JSON lines interleaved into `manifest show` corrupt the output the command exists to produce. |
| 4 | **`activate(..., licenses=...)` takes a mapping, and the CLI has no `--license` flag.** | Nothing on `ManifestSource` holds a licence. Flagged rather than inventing a flag the plan did not specify — see *Still open* below. |

## Still open after T2

- [ ] **A CLI activation records `license=None`**, so FMCSA's CC PDM 1.0 is not captured on the row
      today. A two-line `--license` flag closes it; it was left out rather than invented.
- **Fire cannot be activated** while Google Places' terms of use remain unread — which is the gate
  working, not a defect. Freight can.
- **Who reviews manifest *quality* beyond terms of use** — `activate` catches the legal question, not
  the correctness one. Bites at T12, not here. If a `reviewed_note` recorded at activation is wanted it
  is a schema change, and cheaper before a second manifest exists than after.
- **Two Starlette deprecation warnings** (`HTTP_422_UNPROCESSABLE_ENTITY`, T1 code) surfaced because T2
  is the first slice to produce a 422. Lands naturally with deferred finding #4, already queued before T10.

## Review passes (PR #6, two rounds, 2026-09-30)

Round 1 (`.claude/code-reviews/pr-6-review.md`) raised two Mediums, no Critical or High: `status` was a
plain string column, so `'Active'` inserted cleanly — exempt from the one-ACTIVE invariant *and* invisible
to every reader filtering on `'active'`; and the CLI let an `IntegrityError` escape as a traceback against
a contract that says "never a traceback" without a qualifier. Both fixed. Round 2
(`.claude/code-reviews/pr-6-review-round-2.md`) confirmed them closed and raised one more: the refusal
message named *activation* while living in the file every slice's CLI shares — so T12's `propose`, which
reaches the second unique constraint through a racy select-max-then-insert, would have been told an
activation landed first and pointed at an id it does not have. Fixed in `82b0c63`.

**What worked.** Declining two of round 1's suggestions with reasons — the argparse `Namespace` soundness
note (inherent to typeshed; no fix without a suppression) and a defensive terms-gate `assert` in the
repository (duplicating the rule across two layers is how two layers drift apart). A review is evidence,
not an instruction list.

**What didn't.** The `status` hole was reachable from the first line of the model and survived authoring,
self-review and a full green suite. A column constrained in prose and nowhere else is the same shape as
the `if vertical ==` problem this slice exists to solve, and the slice's own README argued the general
case — *a check can be forgotten by a new slice; a constraint cannot be written around* — while the table
it described left one unconstrained.

**To improve.** When a column's legal values are named anywhere in prose, they belong in a CHECK
constraint in the same commit. Worth a line in the plan template beside the retry-table note T3 earned.

---

# T3 — HubSpot gateway (2026-09-27)

**Branch:** `feat/t3-hubspot-gateway` · **Plan:** `.claude/plans/t3-hubspot-gateway.md` ·
**Report:** `.claude/reports/t3-hubspot-gateway-report.md` · **Status:** merged 2026-09-30 —
PR #5 (`a78967e`), review findings fixed first

The gateway with no caller: client, the five custom properties, dedupe, and the write-gate enforced
at the boundary. T9 and T11 are its consumers.

- [x] `httpx` moved to runtime deps; `uv.lock` regenerated.
- [x] `app/promotion/` — `exceptions` · `schemas` · `client` · `properties` · `dedupe` · README.
- [x] Write-gate by signature: `to_property_payload` takes `ProvenancedValue`s, collects every
      uncitable field and raises once, before serializing anything.
- [x] `create_task` ungated, with a test — the T13 guarantee.
- [x] 63 new tests, all against fixtures through an injected `MockTransport`; unrouted requests raise.
- [x] Lifespan closes the client next to the engine; `.env.example` carries the seven scopes.

## Decisions taken while implementing

| # | Decision | Why |
|---|---|---|
| D16 | **Task enums read from the live portal, not the docs.** `hs_task_status` has five members (`NOT_STARTED`, `IN_PROGRESS`, `WAITING`, `COMPLETED`, `DEFERRED`), `hs_task_priority` four (incl. `NONE`). | HubSpot's docs contradict themselves; the plan flagged it as open. Settled read-only against portal 244766495. |
| D17 | **`TaskType` excludes `LINKED_IN_CONNECT` / `LINKED_IN_MESSAGE`**, which the live property does offer. | PRD §8: LinkedIn automation is a non-goal — it violates Sales Navigator's terms and risks the one seat. The write vocabulary should not offer them. |
| D18 | **A blank `HUBSPOT_PRIVATE_APP_TOKEN` counts as missing.** | `.env.example` ships the key uncommented and empty, so the empty string is what a fresh clone produces. A client built on it sends `Bearer ` and 401s three layers from the cause. Same reasoning as provenance's blank-`source_url` rejection. |
| D19 | **Status codes are spelled as module constants, not taken from httpx's enum.** | Its members carry a `(value, phrase)` pair through a custom `__new__`; Pyright strict reads that as a tuple and calls every comparison permanently false. |
| D20 | **A 423 Locked is never retried on a create** — only on a read, and then exactly once (`_LOCKED_MAX_ATTEMPTS`). | The plan's retry table said "423 Locked — once, after ≥2 s" without distinguishing creates from reads. A lock says nothing about whether our write landed, so it carries the same ambiguity as a 5xx, and the search index lag means a duplicate made this way is invisible until a human finds it. Found by the PR #5 review. |

## Still open after T3

- **Who owns `lpe_sourced_at`** — the caller (as built) or derived by the client from a designated
  record-defining field. **Decide when planning T9**, per the plan.
- **The 409 body for a duplicate property** is still community-reported, not documented. Handled by
  status, never by message string — so the live shape does not matter until someone reads it.
- **Google Places terms of use and pricing** — unchanged, blocks nothing in T3.
- [ ] **The live-portal provisioning run — blocked, needs the human.** Writes five custom properties
      to portal 244766495, and property internal names are permanent in HubSpot: no rename, no clean
      undo. AC2's "a second run no-ops" is proven by test only until this runs. One-way door.

## Review pass (PR #5, 2026-09-30)

Reviewed by the `code-reviewer` agent in a clean context — report at `.claude/code-reviews/pr-5-review.md`.
One Critical, one Medium, one Low, all in `_request`; all three fixed in `d604fa2`, suite back green at
156 passed.

**What worked.** Dispatching a separate reviewer rather than self-reviewing. The Critical was a branch
the authoring session had read several times without seeing it — every other retry path gated on
`retry_on_server_error` and this one silently did not.

**What didn't.** A green suite proved nothing here: the 423 test only covered a read, so the create path
had no coverage at all. The 5xx and timeout cases each had an explicit "a create is never retried" test
and 423 did not — the asymmetry was the tell, and it was visible in the test names alone.

**To improve.** When a policy flag like `retry_on_server_error` governs several branches, the branches
should be checked as a set, not reviewed one at a time — and each should get the paired test the others
have. Worth a line in the plan template for anything with a retry table.


---

# Wave 2 closed — where `main` stands (2026-09-30)

Both Wave 2 tickets are merged and `main` is clean at `9bbf26c`. T1, T2 and T3 have shipped: the service
boots, validates clean, the provenance primitive exists, and two slices are built — `manifests/`, and the
gateway half of `promotion/`.

| | |
|---|---|
| Suite on `main` | **191 passed, 43 skipped** with no database (verified 2026-09-30); the skips name the exact `docker run` |
| Merged | PR #3 `effd0e2` · PR #4 `8c2d84b` · PR #5 `a78967e` (T3) · PR #6 `74b1e17` (T2) |
| Dev Supabase | at `0003_seed_freight_and_fire` (head); freight and fire both DRAFT |
| Prod Supabase | not created yet |

**The amend window is closed** — recorded above under Wave 2 prep and repeated here because it is the
rule most likely to be broken by someone reading only the newest section. Dev has applied `0002` and
`0003`. Any future change to `vertical_manifest`'s shape is a new revision, never an amend, including
anything T4 or T11 discovers it wants.

## Blocked on the human

- [ ] **The live-portal provisioning run** (T3) — writes five custom properties to portal 244766495.
      Internal names are permanent in HubSpot: no rename, no clean undo. AC2's "a second run no-ops" is
      proven by test only until this runs. The one outstanding one-way door.

## Carried into Wave 3

Deferred review findings, unchanged and still due at the tickets named above: **#2** `source_url` URL-shape
(before T5) · **#4** 422 field-level detail (before T10) · **#6** `RunCost` record/check coupling (before
T6) · **#7** the `Any` guard's blind spots (when convenient). T2's `--license` flag joins them, due
whenever a licence first needs to be on a row.

**Next: Wave 3 — T4 ∥ T11**, held to two worktrees rather than the doc's three (reason under *Deliberately
not done* above: T4 and T11 both migrate and are the first real exercise of the B3 single-head guard;
three-way parallelism should not be the experiment that also tests an unproven mechanism). T12 follows.
Plan just-in-time — a dependent ticket waits until its dependency is *implemented*, not merely sliced.

---

# Wave 3 — T4 ∥ T11 ∥ T12 (2026-10-08 → 2026-10-09)

**Base:** `main` @ `bbed27a` · **Landed:** PR #7 `bd691c5` (T4) · PR #9 `d46e147` (T11) · PR #8 `dee4cdf` (T12)

**Three worktrees, not the two Wave 2 closed on.** The note above held Wave 3 to T4 ∥ T11 with T12 after, so
the single-head guard's first real test would not share a wave with a third branch. The human asked for all
three in parallel, and that overrode the note. It was not raised at the time because the note was missed (this
file was overwritten from its first 80 lines and later restored — see *Review*). The guard held: one linear
chain, verified on the integration branch before anything landed.

| Ticket | PR | Branch | Slice | Migration |
|---|---|---|---|---|
| **T4** sourcing data model | #7 | `feat/t4-sourcing-model` | `app/sourcing/` | `0004_sourcing` → `0003` |
| **T11** cadence state machine | #9 | `feat/t11-cadence` | `app/cadence/` (+ reads in `promotion/`) | `0005_cadence` → `0004` (rebased at merge) |
| **T12** manifest-authoring agent | #8 | `feat/t12-manifest-agent` | `app/manifests/{agent,prompts,proposal}.py` | none |

## Tasks

- [x] Three worktrees under `my-dynamous-muse/worktrees/`, `.env` copied, 234 passed in each. DB tests ran on a
      throwaway Postgres (`lpe-wave3-pg`, port **5434** — 5433 was taken), one database per worktree
- [x] Plan → implement → validate → report → PR, one background agent per ticket, autonomous to PR (human's call)
- [x] Fresh-eyes review per PR (`piv-review-pr`, posted as comments) → `.claude/code-reviews/pr-{7,8,9}-review.md`
- [x] Findings fixed per PR (`piv-fix-review-findings`), each fix with a test that failed first
- [x] T11 due-date floor folded in at merge (`336cb21`)
- [x] Integration branch `integration/wave-3`: T4 → T11 → T12, full suite green, then each PR landed on GitHub in
      that order with `main` merged into the next branch; final `main` tree identical to the validated one
- [x] `CLAUDE.md` architecture map and commands updated

## Review findings and how they closed

| PR | Review | After fixes |
|---|---|---|
| #7 T4 | approve-after: 0 Crit · 0 High · 4 Med · 10 Low | all 4 Med fixed; Lows fixed or deferred to T5 |
| #8 T12 | request changes: 1 Crit · 1 High · 2 Med · ~13 Low | all fixed; the replay harness now drives the SDK's real `query()` |
| #9 T11 | request changes: 0 Crit · 2 High · 5 Med · 4 Low | all High/Med fixed; L1, L3 deferred |

The two that would have hurt in production: **T12** cited pages whose fetch had returned 403/404 or
redirected off-host; **T11** could create a duplicate HubSpot task after an ambiguous 5xx, and paired a ticked
task with activity meant for the next touch. Both were invisible to their own suites — T12's replay modelled the
SDK wrongly, and T11's failure test failed the create *before* HubSpot recorded it.

## Decided (human, 2026-10-08)

- **T4 field ownership:** each candidate field has one owning stage (`app/sourcing/stages.py`); a non-owner may
  fill an empty field, never overwrite. Owners: `registry_id`/`legal_name`/`dba_name` → `search_registry`;
  `address`/`phone`/`website` → `verify_business`.
- **T4:** keep the unrequested `manifest_id` FK (a run needs an ACTIVE manifest). Merge-never-clears,
  cost-at-finish and the `(run, registry id)` key stand; their follow-ups are T5's.
- **T11 sync:** **daily, by an external launchd job**, separate from the weekly sourcing run. Due dates are
  floored at the end of the sync's own day, so a touch found the next morning is never born overdue.
- **T11:** notes count as a matching activity; no enrol route/CLI (T9/T13 call `enrol`); no exit on a reached
  conversation; superseded tasks reported, never completed; a default task-owner setting added.
- **M5** counts touches the cadence machine closed, not HubSpot task completion (T10 reads it).
- **T12:** quality review stays an **open question** — no gate, `propose` prints a note. Quote not stored;
  terms questions printed, not persisted; caps 40 turns / $5.00 until a real run logs cost.

---

# Wave 3 closed — where `main` stands (2026-10-09)

`main` is at `dee4cdf`, clean. T1, T2, T3, T4, T11 and T12 have shipped.

| | |
|---|---|
| Suite on `main` | **538 passed** with a database; **376 passed, 162 skipped** without one |
| Types / lint | mypy 99 files, pyright 0, ruff clean; zero suppressions |
| Migrations | one head, `0005_cadence`; empty → head → `alembic check` → base → head all clean |
| Dev Supabase | at **`0005_cadence` (head)** since 2026-10-09; `alembic check` clean |
| Prod Supabase | not created yet |

**The amend window for `0004` and `0005` is closed** (2026-10-09). Both were edited in place during review —
legitimately, nothing real had run them — and dev has now applied them. Any change to `sourcing_run`,
`candidate` or `cadence_state` is a new revision, never an amend.

## Blocked on the human

- [x] **Apply `0004` and `0005` to dev Supabase** — **done 2026-10-09.** Dev was unreachable at first: the pooler
      answered `tenant/user … not found` because the free project had paused; restored by the human. Applied
      `0003 → 0004 → 0005`; verified `alembic current` = `0005_cadence (head)`, `alembic check` clean, all three
      tables present and empty with every check/unique/FK constraint, both manifests still DRAFT.
- [x] **`.env` driver prefix had regressed** to `postgresql://` (file edited 2026-10-05), which selects psycopg and
      fails at startup. **Fixed by the human 2026-10-09**; verified — `alembic current` reaches dev with `.env`
      as-is and reports `0005_cadence (head)`. A URL pasted from the Supabase dashboard always loses the
      `+asyncpg`, so check the prefix after any rotation
- [x] **Dev pauses when idle** (free tier, ~7 days). **Accepted 2026-10-09 (human):** dev is temporary and is
      replaced by a real project once the service runs. Meanwhile the daily 07:00 sync touches dev every day,
      which should keep it from pausing. That holds only while the Mac is awake; after a week away, expect
      `tenant/user … not found` and restore from the dashboard
- [x] **`HUBSPOT_DEFAULT_OWNER_ID` in `.env`** — done 2026-10-09 (Sandeep, owner `86826215`; owners read via the
      HubSpot connector). The HubSpot credential is now a **Service Key** (legacy private apps are being phased
      out), still read from `HUBSPOT_PRIVATE_APP_TOKEN`; same `Bearer` header, no code change
- [x] **Daily launchd job installed** — 2026-10-09. `~/Library/LaunchAgents/com.compumatrice.lpe.cadence-sync.plist`,
      07:00 daily, log at `~/Library/Logs/lpe/cadence-sync.log`. A `launchctl kickstart` run exited 0 (0 live)
- [x] **Read-only HubSpot check** — 2026-10-09, through the app's own client: tasks search, tasks→contact and
      contact→tasks/calls/emails/notes/meetings associations, batch reads; every call 200, the association
      body parsed (no `associations_unrecognised`), `hs_timestamp` is ISO-8601. **Still unconfirmed:** that
      the `lpe-cadence:` key survives in a task body — no cadence task exists yet; check on the first one
- [x] **Advisory lock through the session pooler** — 2026-10-09: with the lock held on dev, a second
      `lpe cadence sync` skipped and exited 0
- [x] **One live `propose` run** — 2026-10-09, `propose --vertical freight "freight brokerages and non-asset 3PLs,
      DFW"` → **freight v2 DRAFT** `866929c2-df6f-49ab-9588-aadd32efda67` on dev. **$0.78, 36 of 40 turns.**
      Acceptance holds live: FMCSA named (census, QCMobile, authority history, revocations) and an
      `asset_based_carrier` exclusion. 18 fetches counted as reads; **6 refused** (four `http_403`, two
      `http_302`) and none of them cited — PR #8's Critical fix confirmed against the real CLI. A same-host
      redirect ending in 200 did not occur, so that path is still unexercised.
  - [ ] **Turn cap is tight**: 36 of 40. Raise `MANIFEST_AGENT_MAX_TURNS` (e.g. 60) before vertical #2; budget is ample
  - [ ] **Quality, before anyone activates v2** (the open question, now concrete): `no_broker_entity_type` lists the
        `carship` values to *exclude*, so any unlisted combination without a `B` passes; the DFW metro has no
        cited boundary; the ICP band (6–50) rests on one benchmark survey; three sources' terms point at
        `project-open-data.cio.gov/unknown-license/`
  - [x] **Disqualifier review → freight v3 DRAFT** `ecdc1c8e-f6d3-4eec-81a7-6a80cacb70d3` (2026-10-09). Checked against
        the census file's real values (FMCSA publishes no column definitions). Rule language gained
        `not_contains` (PR #10) and `not_in_set` (PR #11): both rules v2 needed meant "exclude by absence".
        v3 = v2 with three rules rewritten, cited `manual_research`: `outside_dfw_metro` → `phy_cnty not_in_set`
        the 11 counties of OMB's 2023 CBSA 19100 (codes checked against Census's FIPS file); `no_broker_entity_type`
        → `carship not_contains 'B'`; `asset_based_carrier` → `power_units greater_than 10` (ATA's small-fleet line,
        91.5% of carriers run ≤10). DFW boundary: the human chose the 11 counties for now, more later.
        **Census funnel with v3's free predicates:** TX 377,926 → active 194,663 → DFW 45,855 → broker code 4,518
        → ≤10 units **4,449**. That is ~7× the ticket doc's "~600", before QCMobile and the two judgment rules
  - [ ] **For T7's evaluator:** `power_units` is **text** in the census dataset (a numeric compare 400s without a
        cast); predicates need a source per field (`allowToOperate` is QCMobile, not census); `authority_revoked`
        is a join, kept as judgment only because the rule shape cannot say "join"
  - [ ] **For T5–T7 sizing:** two judgment rules × ~4,400 candidates is ~8,800 `classify_rollup` calls a run unless
        something cheaper runs first — re-plan the funnel order before T7
  - [ ] Never activate freight v1 (its `carrier_operation equals 'asset_based'` matches nothing; real values are
        A/B/C) or v2 (superseded by v3)
  - [ ] **Not yet a fixture**: the CLI does not save its transcript, so the live run cannot replace T12's synthetic
        fixture without a small recording hook
  - [x] Cosmetic: cost prints as `$0.7817127400000001`. **Done 2026-10-09:** `core.cost.format_usd` rounds
        to the cent, half up, for display only; logs keep the exact value
- [x] **The live-portal provisioning run** (T3) — **done 2026-10-09.** Run 1 created the `lpe` group on companies
      and contacts, five properties on companies and three on contacts; run 2 issued no writes (all
      `property_present` / `group_present`, no drift). AC2's "a second run no-ops" is now proven live

## Carried into Wave 4

- **Due before T5:** #2 `source_url` URL-shape (from Wave 2) · crash path for a run (cost and `failed` status on
  a fresh session; DB-clock `finished_at`; conditional finish) · source-prefixed registry ids before the first
  real write · T5 callers must pass `stage`
- **Due before T8:** "owned ≠ verified" — a verified address is told apart by `retrieval_method`, not presence
- **Due before T13:** `lpe cadence park <contact>`; `lpe cadence sync --dry-run` for the first run on the 22
- **Deferred, when convenient:** the per-touch "closed by a note" line · T11 L1 (enrol race — reuse H1's key) ·
  T12's deferred Lows (listed in `pr-8-review.md`) · #4 (before T10), #6 (before T6), #7 from Wave 2
- ~~**Fix the AI layer:** `piv-validate` is an unfilled template~~ — **done 2026-10-09 (`515c020`)**: wired to
  ruff · mypy · pyright · pytest · alembic check; the verdict names its tier (full / offline + skip count).
  Verified green on `main` (538 passed, no drift) and verified to fail on a deliberate lint/type break

**Next: Wave 4 — T7 ∥ T8 ∥ T13** per the ticket doc, T13 still gated on Spike 1 closing.

## Review

**Worked.** One background agent per ticket, each in its own worktree with its own database, ran plan-to-PR
without collisions. Fixing collision points up front (migration names, merge order) made the merge mechanical:
two one-file conflicts, both resolved once on the integration branch and replayed onto the PR branches with
`git checkout integration/wave-3 -- <file>`. Fresh-eyes review earned its cost — all three PRs passed their own
suites and two still carried a High or worse.

**Didn't.** This file was overwritten at the start of the wave from a read of its first 80 lines, which hid
the Wave 2 close-out — including the decision to run two worktrees, not three. It was restored before commit.
Both serious bugs passed their authors' tests because the test doubles encoded the author's assumption about
the external system (the SDK's stream; when HubSpot records a create).

**Next time.** Read the whole work log before planning a wave, and append — never replace. For anything behind
an external boundary, the review should ask first whether the fake behaves like the real thing.

---

# Funnel re-plan — before T7 (2026-10-09)

**Why:** the ticket doc's funnel assumes ~600 census records → ~120 after disqualification → ~80 verified. Freight
v3's free predicates leave **4,449** DFW broker candidates. Planned as written, T7's two judgment rules are ~8,800
`classify_rollup` calls a run, and T6's paid verification sees thousands, not ~80.
**Scope:** docs only — architecture *Recommended approach* / funnel, tickets T5–T7 (+ T6 sizing), no code.

- [x] Read the current funnel: architecture doc, tickets T5–T7, PRD metrics and cost lines
- [x] Measured free census signals on the 4,449 pool: active MC authority 95% (cuts ~200) · `prior_revoke_flag`
      and `business_org_desc` ~always empty (useless) · MCS-150 filed within 2y only 17% · **`company_officer_1`
      present 67%** · phone 97% · email 67%. Active MC + current MCS-150 = 680
- [x] Priced the paid steps: Places Text Search Pro $32/1,000 after 5,000 free a month (phone/website may need a
      higher SKU, so confirm at T6); a judgment call ~$0.08 (live `propose` measured ~$0.02/turn × ~4 turns)
- [x] Proposed A (as written, ~$700/wk, ✗) · B (narrow to 680 on recency, drops 85% on a guess) · **C (backlog
      queue, weekly batch)** → **human chose C**, Places before the judgment call (D8 revised), one judgment call
      per candidate, census officer before `resolve_owner`, free data before any model call
- [x] Updated: architecture *Recommended approach* (backlog paragraph), *Boundaries* (Places), *Cost* (measured
      shape), *Open questions* (decided); tickets D8 (revised) + **D12** (new), measured funnel, T5 (backlog,
      batch, free checks, text casts), T6 (before judgment, census owner first, SKU, no longer depends on T7),
      T7 (evaluator needs, one call per candidate), graph (T7→T6 removed), wave 6 note, SPIKE-2 principal half;
      `CLAUDE.md` working principle

## Review

**Worked.** Measuring before deciding. The census query API answered in minutes what the docs had guessed,
and the guess was ~7× low. Reading the tickets before costing changed the frame. Suppression (T7) already made
the pool a backlog, so the fix was batching, not a smaller pool. Using the live `propose` run's cost as the
per-call anchor beat a token estimate.
**Watch.** The batch size (150) and the ~20/week yield are estimates until T10's first runs. The judgment-call
price is per agentic call with fetches. A bare classification without fetches would be cheaper. The census
officer is a named principal, not proven to be the decision-maker, so M6 still needs checking.
**Next.** Wave 4 (T7 ∥ T8, T13 if Spike 1 is closed) can be planned against these docs.

---

# Spike 1 closed — discipline, not sourcing (2026-10-09)

**Why:** T13 is gated on Spike 1 closing. The timebox (2026-09-26 → 10-10) ends tomorrow, and nothing a day
could add would reach the decision rule's bar. **Human closed it today.**
**Scope:** docs only. No code.

**Evidence, read-only from HubSpot (2026-10-09):**
- **Tasks: 1 of 22 completed.** The only completion is the GFS Fire Pros follow-up call, ticked on 10-09. The
  other 21 are NOT_STARTED, and 20 of them are past due. Only the Crisp-LaDew re-approach (11-23) is still in
  date. The portal holds 26 tasks: the 22, two HubSpot sample tasks, and two created during the spike (the
  Kodiak call and the Sentinel follow-up, both 09-29 and both unworked).
- **Activity: 10 notes, 0 logged calls, 0 meetings, 0 emails** since 09-26. All of it happened 09-27 → 09-29:
  one website teardown, a morning of calls on 09-28 and two door knocks on 09-29. **Nothing has been logged
  since 09-29.**
- **Decision-maker conversations: 0** (human, 2026-10-09). The three 09-28 call outcomes did not reach a
  decision-maker: a "not interested" from Justin, "not interested right now", and "no more new customers, do
  not call them again". At the Sentinel door knock the front desk (Beth) took collateral for Brian, the
  decision-maker.
- **Rule:** ≥3 decision-maker conversations a week → sourcing is the bottleneck. Measured: 0 over two weeks,
  with work stopping after day 4 → **discipline. Cadence enforcement carries more weight than sourcing.**

**What the data says about the machine (T11/T13):**
- When the work was done, it was logged as a **note**, not a call, and the task was **not ticked**. Example:
  Jorge was called twice on 09-28 and both of his tasks are still open. T11's *task done OR matching activity
  after creation, notes count* rule is what makes these touches visible. Without it, the machine would nag
  about work already done.
- For T13, "surviving prospects" has to exclude explicit refusals, above all the "do not call them again"
  company. They need parking or exiting, not enrolment.

## Tasks

- [x] Architecture doc → *Spike 1*: **ANSWERED 2026-10-09**, with finding, consequence and what it showed about
      the machine
- [x] PRD → *"Is the bottleneck sourcing at all, or discipline?"* checked and answered
- [x] Tickets → D6, the SPIKE-1 gate row, the graph label, T13 (gate cleared, refusals excluded, notes count),
      the Wave 4 note and T10 (M5 checkpoint before the first real run)
- [x] Further reorder? **Decided (human): no.** T7 ∥ T8 ∥ T13 stands. I proposed T13 first and alone, and the
      human pushed back: lapses are a fact of life, and the machine exists to absorb them. Running alone would
      not ship T13 sooner, and new prospects cannot reach HubSpot before T9/T10 anyway. Guard: a **checkpoint**
      (M5 on the adopted prospects before T10's first real run), not a pause

## Review

**Worked.** The spike's evidence was already in HubSpot. One read-only pull gave the verdict, so no
self-report was needed. Asking the human only the question the data could not answer (were the 09-28 calls
with decision-makers?) kept the verdict honest.
**Didn't.** My first reorder proposal (T13 alone) treated the finding as a reason to slow sourcing. It
optimised for focus, which doesn't need serialising when the slices are disjoint. The human's framing was
right: the spike justifies the machine, not a pause.
**Watch.** The 22 is reconstructed from E15's counts (20 from September plus 2 from February). Recheck the
exact set when T13 is planned. Two tasks created during the spike (Kodiak, Sentinel) are adoption candidates
too.

---

# Manifest agent: turn cap and quality review (2026-10-09)

**Why:** two open items from the live `propose` run. Turns were the binding cap (36 of 40, $0.78 of $5.00),
and a capped run fails whole, writing nothing. Activation checks terms of use, not correctness, and freight
v1 and v2 were both wrong in ways only a check against real data caught.

- [x] Turn cap default **40 → 60** (`app/core/config.py`, `.env.example`, the cap-message test). `.env` does not
      set it, so the default applies. The $5.00 budget stays as the runaway guard
- [x] Quality review **decided (human): a mechanical dry-run, built in T7.** `lpe manifest dry-run <id>`
      reports step-by-step pool counts and sample rows from the free rules against real data; `activate`
      requires a recorded one. Optional: a check at proposal time that rule values exist in the source data.
      Recorded in T7, the ticket doc's *Still open* and the architecture doc's open questions
- [ ] Still open for **freight v3** before anyone activates it: the ICP band (6–50) rests on one survey
      (human's call), and three sources' terms point at `unknown-license` (answered at `activate`)

## Review

**Worked.** Both decisions came out of evidence already on hand: the live run's turn count, and the two
manifests the hand check caught. The quality gate copies the check that actually worked, rather than
inventing a review step.
**Watch.** The dry-run makes T7 bigger, by a CLI command plus an activation precondition (likely a column, so
a new migration). Re-size T7 when it's planned.

---

# Google Places: terms and pricing → D13 (2026-10-09)

**Why:** open before T6. The terms were unread and the SKU was unconfirmed.
**Finding:** the Maps Platform Terms §3.2.3(a) forbid us to "pre-fetch, index, store, reshare, or rehost" Places
content, or to "copy and save business names, addresses, or user reviews". The only storage allowed is place IDs
(indefinitely) and lat/lng (30 days), and Places lat/lng is barred from point-in-polygon analysis. T6, T8 and T9
as written would have stored Places address, phone and website and sent them to HubSpot. Pricing was never the
problem: phone and website are Enterprise ($35 per 1,000, 1,000 free), and the Pro tier is ~$0 at a batch of 150.

- [x] **Decided (human): option A.** Places is a check, never a source. Three rules: look up within the run,
      never ahead; use and discard, keeping only the place ID and our verdict; Places never discovers prospects.
      Census for address and phone, the email domain or web search for website, the US Census Geocoder for
      coordinates. The review-name owner signal is dropped
- [x] Recorded: architecture *Recommended approach* step 2, *Boundaries* (Places rewritten, Census Geocoder
      added), the terms open question; tickets **D13**, T6, T7, T8, the fire seed line, *Still open*;
      `adding-a-vertical.md`; a `CLAUDE.md` ground rule
- [ ] **T6 planning:** store the verdict, or only the place ID and a timestamp (the zero-ambiguity option)
- [ ] **T12 follow-up:** the authoring agent's prompt should know rule 3, so it never proposes Places as a
      vertical's discovery source. Check `app/manifests/prompts.py` and the seeded fire manifest's source list
      before vertical #2
- [ ] At `activate` for freight v3: if Places is declared, accept its terms on this basis

## Review

**Worked.** Reading the primary text (`curl` on the terms page) rather than a summary. The page-summariser
returned noise twice; the extracted clause was unambiguous. Pricing and terms answered different questions,
and only the terms changed the design.
**Watch.** "Pre-fetch" for an unattended batch is a grey area. Rule 1 is the defensible reading, not a ruling.

---

# Before T13: `lpe cadence park` and `lpe cadence sync --dry-run` (2026-10-09)

**Why:** both are deferred in `app/cadence/README.md` and wanted before T13 adopts the 22. `park` lets a human
stop a prospect, for example after a conversation is reached. `--dry-run` shows what a sync would do before it
does it. **Scope:** `app/cadence/` only, CLI only. No migration, no route.

**Proposed behaviour (confirm before building):**
- `park <contact>`
  - **Final.** `uq_cadence_state_contact` already makes a parked contact un-enrollable, and there is no unpark.
  - **Leaves the open HubSpot task alone** and prints its id ("close it in HubSpot"). The slice never writes to
    the founder's tasks.
  - **Stores no reason.** Outcomes live in HubSpot, so it prints a reminder to log a note there.
  - **Takes the sync lock** and refuses while a sync runs. Otherwise a sync that already loaded the row could
    advance a parked prospect.
  - Errors: contact not enrolled; already parked. A row with a pending create is parked, its pending key is
    cleared, and the CLI warns that a task may exist in HubSpot.
- `sync --dry-run`
  - **Reads only.** HubSpot reads as a real sync does; no task creates, no database writes, no lock.
  - Per prospect: the touches that would close and **by what** (task, call, note…, which also covers the
    deferred "closed by a note" line), then the next task and its due date, or "would park". A pending row says
    whether the interrupted task was found.
  - Exit 1 when any prospect's reads fail, as `sync` does.

## Tasks

**Decided (human):** park is final (no unpark), and the open task is left alone with its id printed. The
plan was approved as written.

- [x] `service.py`: `_sync_one` split into `_decide` (reads, then `plan_advance`) and apply. `sync(dry_run=True)`
      stops after deciding, takes no lock and logs no `touch_done`. `park(contact_id)` runs under the sync lock.
      The sync loop also skips a row that is no longer live
- [x] `schemas.py`: `ProspectPlan`, `PendingTask`, `ParkResult`; `SyncReport.dry_run` and `.plans`
- [x] `exceptions.py`: `NotEnrolledError` (404), `AlreadyParkedError` (409), `SyncRunningError` (409)
- [x] `cli.py`: `sync --dry-run` prints per prospect what closes each touch ("task ticked", "note 880…"), then
      the next task and its due date. `park <contact>` prints the open task to close and asks for a HubSpot note
- [x] Tests first, each failing first: 13 service, 5 CLI
- [x] README (*Dry run*, *Parking by hand*; *Deferred* trimmed), `CLAUDE.md` commands, package exports
- [x] Validation: ruff, mypy, pyright clean; **562 passed with the database**, 382 passed / 180 skipped without;
      `alembic check` clean (no migration)

**Found on the way:** `b0d1e3c` (the cost rounding) broke a database-tier assertion in `test_cli_propose.py`.
It was validated without a database, so the test skipped. Fixed on `main` in `029da00`, and this branch is
rebased on it.

## Review

**Worked.** Splitting decide from apply made the dry run honest by construction: it is the real sync minus the
writes, not a second code path that could drift. Every new test failed before its code existed.
**Didn't.** I validated the earlier cost-rounding fix offline only, and a database-tier test caught it a
commit later. **Run the database tier before any push that changes output text.** The skip count is the
signal: 162 skipped means 162 tests did not look.
**Watch.** A dry run plans with "no task" for an interrupted create it would make. That equals a fresh open
task only because an open task closes nothing, which `find_signal` guarantees today.

**PR #12 review (2026-10-09):** fresh-eyes review by the `code-reviewer` agent, report in
`.claude/code-reviews/pr-12-review.md`. 0 Critical, 0 High, 1 Medium, 6 Low. All fixed except one CLI test,
each with a test that failed first. The Medium was a dry run racing a real sync and calling a moved task
"deleted". **Lesson for test doubles:** an ORM `update()` also refreshes the session's copy, so a test
simulating "another process changed the row" needs `synchronize_session=False`. The first L4 test passed on the
broken code.

---

# Before T8: "owned ≠ verified", redone for D13 (2026-10-09)

**Why:** the T4 review's item had T8 tell a verified address by `retrieval_method == web_lookup`, because
`verify_business` would overwrite the census address with the Places one. D13 bans storing Places content, so
the address is always the census copy and `retrieval_method` can no longer tell verified from not. Verification
is now a separate fact. **Decided (human): option A.** We store the place ID and when it was checked, nothing
else. "Verified" means Places found this business from the census name and address. This closes the T6 open
item from D13.

## Tasks

- [x] `stages.py`: `address` and `phone` → `search_registry`; `website` stays `verify_business`;
      `business_check` → `verify_business`; module docstring says owning ≠ verifying
- [x] `schemas.py`: `PlaceCheck` (frozen, non-blank `place_id`); `CandidateFields.business_check`;
      `has_verified_address()`; the **D13 guard** refuses any other field cited to `places.googleapis.com`,
      `maps.googleapis.com`, `maps.google.com`, `maps.app.goo.gl` or `google.com/maps` (Google search is allowed)
- [x] Tests: 15 new (business check, truth table, guard, ownership). The pre-D13 tests that stored a Places phone
      are rewritten around a web-searched website. **Mutation-checked:** handing phone back to `verify_business`
      fails 3 tests, and disabling the guard fails 5
- [x] `unprovenanced_fields()` now also lists `business_check` when absent: honest, and T9 decides what is required
- [x] Docs: sourcing README (ownership table, *Verified is a field, not a source*, the guard); T8 and T6 tickets;
      the architecture doc's open T6 item closed
- [x] Validation: ruff, mypy, pyright clean; **587 passed with the database**, 400 / 187 skipped without;
      `alembic check` clean (no migration: JSONB)
- [x] PR #13 + fresh-eyes review (`.claude/code-reviews/pr-13-review.md`): 0 Critical, 0 High, 1 Medium, 4 Low.
      M1 (a `model_copy` bypassed the D13 guard; reproduced) fixed by re-validating in `upsert_candidate`. L1
      (4 host forms missed) fixed. L3 tests and L4 docs added. L2 (tie the check to the address) deferred to
      T6 with a note. 604 passed with the database
