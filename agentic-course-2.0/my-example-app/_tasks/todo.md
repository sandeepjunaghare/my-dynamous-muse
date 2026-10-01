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
