# Feature: T10: the weekly run, its trigger, and the Friday report

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. `SourcingPipeline` lives in
`app.sourcing.pipeline`; `SourcingService`, `SourcingRunResponse`, `SourcingBrief`, `RunStatus`, `RunOutcome`,
`RunCostSummary`, `SLUG_PATTERN` in `app.sourcing.service` / `app.sourcing.schemas`; `SearchRegistryStage` in
`app.tools.search_registry`; `VerifyBusinessStage` in `app.tools.verify_business`; `Stage`, `registered_stages`
in `app.tools.registry`; `ManifestService` in `app.manifests.service`; `PromotionService`, `PromotionReport`
in `app.promotion.service` / `app.promotion.schemas` (**import from the module, never the package**, ticket
T10); `HubSpotClient`, `get_hubspot_client`, `aclose_hubspot_client` in `app.promotion.client`;
`SearchRequest`, `SearchFilter`, `SearchFilterGroup`, `SearchOperator`, `ObjectType`, `HubSpotObject` in
`app.promotion.schemas`; `CADENCE_TZ` in `app.cadence.machine`; `Clock`, `HubSpotFactory`, `utc_now` in
`app.cadence.service`; `format_usd` in `app.core.cost`. Every exception derives from
`app.core.exceptions.LocalProspectEngineError`.

---

## DECISIONS (human, 2026-10-17, asked before planning)

1. **Friday numbers: note prefixes.** Read-only check of the portal (2026-10-17): every real touch is a
   free-text NOTE. The only CALL and MEETING records are HubSpot sample data, and no deals exist at the
   $999 value. The founders already prefix some door knocks (`🚪 Door knock — 9/29/2026`,
   `Door-knock visit, 9/24/26`). The convention adopted: a note whose text **starts with** `door knock`,
   `owner conversation` or `assessment booked` counts toward that number. The match ignores leading emoji
   and punctuation, ignores case, and treats `-` as a space. Notes in the week with no prefix are reported
   as **unclassified touches**, so the gap is visible rather than hidden. **Paid assessments sold renders
   `not configured`** (GATE-A, D10), never `0`.
2. **`POST /runs` is 202 + background.** The preflight, the manifest check and the one-run-at-a-time guard
   run in the request and refuse there. The work then runs as a FastAPI background task, and
   `GET /runs/{id}` shows progress and the result. `lpe runs start` runs the same service in-process for
   launchd.
3. **Real promotion by default, with a `dry_run` option** on both the route and the CLI. The first real
   run provisions `lpe_verification` in the portal (accepted in PR #38).

Settled without a question, from earlier decisions (recorded so nobody reopens them):
- **Routes after qualification** (ticket T10 bullet): **already done**. T9's OQ8-B makes `cluster_routes`
  (stage 5) exclude the run's disqualified candidates, and every disqualification (free predicates in
  stage 1, the judgment node in stage 3) is recorded before stage 5. Nothing changes qualification after
  stage 5 inside a run, so there is nothing to re-route. Recorded in the ticket; no code.
- **The review sync and the cadence sync stay daily** (launchd, `lpe promotion sync` then
  `lpe cadence sync`). The weekly run does not call either.

---

## Feature Description

T10 is the integration ticket. It turns five stages and a promotion step that a founder runs by hand
(`lpe sourcing run`, then `lpe promotion promote --run <id>`) into **one weekly run**, triggered externally,
that goes from brief to a pending-review list in HubSpot unattended. It records how long that took (M7) and
what it cost. On Friday it reports the four playbook numbers from HubSpot, with each of the week's runs
beside them: funnel, verification, promotion and review outcomes.

## User Story

As a Compumatrice founder with ~4 hours a week for new business
I want one trigger to source, verify, qualify, route and land a week's prospects in HubSpot, and a Friday
report that tells me what happened
So that my time goes into conversations, and I can see whether the machine (and I) did the work.

## Problem Statement

- The pipeline and promotion exist but are two manual commands with a run id copied between them. Nothing
  measures brief→HubSpot time (M7).
- Nothing stops two runs overlapping. Two `lpe sourcing run` invocations both pass `start_run` and contend
  for the same pool batch.
- The Friday report does not exist. Its four numbers have no structured source in HubSpot (DECISION 1),
  and M3 must not be shown as a real zero.

## Solution Statement

A new slice, **`app/runs/`**, owns the **`weekly_run`** table: one row per weekly run, holding its options,
the sourcing run it produced, the timestamps M7 needs (`requested_at` → `sourced_at` → `promoted_at`), and
promotion's counts. `WeeklyRunService` chains the existing pieces:

```
POST /runs | lpe runs start
  └─ start():   reap stale weekly runs → ACTIVE manifest → configure stages → preflight
                → INSERT weekly_run (partial unique index = one running at a time) → commit
  └─ execute(): SourcingPipeline.run(brief, on_started=record sourcing_run_id)   ── raises → FAILED
                → mark sourced_at
                → PromotionService.promote_run(sourcing_run_id, dry_run=…)       ── raises → FAILED
                → finish: COMPLETED | DEGRADED (sourcing degraded, promotion stopped or failed > 0)
```

The weekly report (`app/runs/report.py`) has three parts:
1. **Playbook numbers.** HubSpot NOTE search over the week's window, classified by prefix (pure function,
   `app/runs/touches.py`).
2. **Runs.** The week's `weekly_run` rows, joined with their `sourcing_run` (counts, cost) and promotion's
   counts.
3. **Review.** M6 accept/reject per run, read from the `promotion` ledger through a new read-only
   `PromotionService.review_tally`.

## Out of Scope / Non-Goals

- **No scheduler, queue or in-process cron.** launchd is the trigger (a human step, Level 4). No Celery,
  no Redis.
- **No sending.** The report is printed (CLI) and returned (JSON). It is not emailed or posted anywhere.
- **No LLM classification of notes.** Prefixes only. The report stays deterministic, and
  `claude_agent_sdk` stays inside its four allowlisted modules.
- **No `$999 assessment` pipeline and no deal reads.** M3 is `not configured` (GATE-A).
- **No M5 computation.** Reading M5 on the adopted 22 before the first real run is a **human check**
  (ticket T10), not code.
- **Not changing:** the stage order, `promote_run`'s rules, the daily syncs, `lpe sourcing run` and
  `lpe promotion promote` (both kept for hand runs).
- **Not guarding `lpe sourcing run` against overlapping a weekly run.** The guard covers weekly runs only.
  A hand-run sourcing beside a weekly run is the human's choice. Recorded as a follow-up.
- **No `app/sourcing/routes.py`.** The ticket's file list names it, but the route lives in `app/runs/`,
  which owns the orchestration.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: High (integration across sourcing, promotion and HubSpot, plus a background task
and a new table)
**Primary Systems Affected**: new `app/runs/`; `app/sourcing/pipeline.py` + `cli.py` (stage-config
helpers, `on_started`); `app/promotion/schemas.py` + `client.py` (search operators and paging);
`app/promotion/service.py` + `repository.py` (`review_tally`); `app/core/config.py`; `app/main.py`;
`app/cli.py`; `alembic/` (`0011_weekly_run`)
**Dependencies**: none new. FastAPI 0.141 `BackgroundTasks`; stdlib `zoneinfo`.

## Related Work

**Implements**: T10, `docs/tickets/local-prospect-engine.md:517-541` · **Epic**:
`docs/local-prospect-engine.architecture.md` (*Scheduling: boring on purpose*, *Cost*), PRD §6 steps 1-8,
M3/M6/M7.

**Back-references:**
- `.claude/plans/t5-pipeline-fmcsa.md`: the runner and stage contract this chains.
- `.claude/plans/t9-promotion.md`: `promote_run`, the ledger, OQ7 ("T10's `POST /runs` calls
  `PromotionService` directly") and OQ8-B (routes exclude disqualified).
- `.claude/plans/t11-cadence.md`: the advisory-lock precedent and the daily launchd job.
- `_tasks/todo.md`, *Unverified survivors reach review*: the counters the report must show.

**Forward-references:** (none yet)

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `app/sourcing/pipeline.py` (all, 154 lines). Why: `SourcingPipeline.run` (`:65-138`) is what
  `execute` calls. Add `on_started` after `start_run` (`:79`). The failure path is
  `except BaseException` → rollback → `_record_failure` on a fresh session → re-raise (`:132-136`). Mirror
  it in `execute`.
- `app/sourcing/cli.py:89-118`. Why: `_stages` / `_configured` / the preflight loop move into
  `pipeline.py` as public helpers that both CLIs use. `_run` shows the brief built from
  `manifest.body.icp_band.value`.
- `app/tools/search_registry.py:100` (`configured(*, batch_size, allow_degraded)`) and
  `app/tools/verify_business.py:163` (`configured(*, allow_degraded)`), `:181` (`preflight(body)`, raises
  `SourceUnavailableError`, 409). Its docstring says "T10 should call it too".
- `app/sourcing/repository.py:63-132`. Why: `mark_finished` (compare-and-set on `status = 'running'`,
  `finished_at=func.now()`) and `reap_stale_runs` are the patterns for the weekly-run repository: the DB
  clock for every timestamp, and `update … returning`.
- `app/sourcing/models.py:55-111`. Why: the `SourcingRun` model, the CHECK
  `(status='running') = (finished_at is null)`, and the JSONB defaults to mirror on `WeeklyRun`.
- `app/sourcing/schemas.py:39-92, 685-729`. Why: `RunStatus`, `RunOutcome`, `SourcingBrief`,
  `RunCostSummary.total_usd()`, `SourcingRunResponse`.
- `app/sourcing/service.py:52-160`. Why: `start_run`, `get_run`, `finish_run`, `reap_stale_runs`
  signatures. The service commits; the repository flushes.
- `app/promotion/service.py:105, 191-260`. Why: `_PROMOTABLE_RUNS = {completed, degraded}`,
  `PromotionService(session, *, hubspot=…, clock=…)`, `promote_run(run_id, *, dry_run=False) ->
  PromotionReport`. Per-candidate failures are counted; `report.stopped` is set on auth or daily-quota
  stops.
- `app/promotion/schemas.py:34-59` (`ObjectType`, `SearchOperator`: **eq/neq only**), `:226-266`
  (`SearchFilter.value: str`, `SearchRequest` has `filter_groups`, `properties`, `limit`; **no paging**),
  `:556-573` (`PROMOTE_COUNTS`), `:627-642` (`PromotionReport`).
- `app/promotion/client.py:455-475`. Why: `search()` is paced and serializes with
  `model_dump(by_alias=True, exclude_none=True)`, so a `None` `after` drops out of the body.
- `app/promotion/models.py:65-133` + `repository.py`. Why: the ledger. `run_id`, `status`
  (`pending_create, pending_review, enrolling, enrolled, rejected, duplicate, withdrawn`). There is **no
  `accepted` status**: accepted is `enrolling` + `enrolled`.
- `app/promotion/cli.py:93-115`. Why: the CLI pattern. `_with_service` disposes the engine and closes the
  HubSpot client **in the CLI only**; `_emit` sets the exit code.
- `app/cadence/routes.py` (all). Why: the router pattern (`get_x_service` + `Annotated[..., Depends]`, no
  try/except, docstring explaining the trigger).
- `app/cadence/repository.py:22-77`. Why: the advisory-lock precedent. **Not used here** (see NOTES); the
  guard is a partial unique index.
- `app/cadence/machine.py:28`. Why: `CADENCE_TZ = ZoneInfo("America/Chicago")`. The report's week is
  Dallas time. Import it; `app/promotion/cli.py:22` already does.
- `app/core/config.py` (all) + `.env.example`. Why: `extra="forbid"`; every new setting goes in both.
- `app/core/database.py:69` (`get_db`), `get_sessionmaker`, `dispose_engine`. Why: the background task
  uses `get_sessionmaker()`, **never** the request session (FastAPI ≥0.106 closes yield-dependencies
  before background tasks run).
- `app/main.py:15-68`. Why: include the new router; the lifespan already calls `aclose_hubspot_client()`
  at shutdown (`:57`).
- `app/cli.py:19-120`. Why: register a slice's command group (import, `register`, `dispatch` branch).
- `alembic/env.py:22-43` + `alembic/versions/0010_promotion.py`. Why: append the model to
  `_REGISTERED_MODELS`. Hand-write the migration to match the model exactly (`alembic check` must stay
  clean).
- `tests/conftest.py:37-53, 79-180`. Why: `requires_db`, `client`, `error_client`, `migrated_database`,
  `db_session`, `client_with_db`.
- `tests/sourcing/conftest.py:225` (`session_factory`). Why: the pipeline and the weekly service need a
  factory of fresh sessions on one rolled-back connection.
- `tests/sourcing/test_pipeline.py:39-150`. Why: fake stages (`Counts`, `Boom`) and how the runner is
  driven.
- `tests/promotion/scenario.py:46-90`, `tests/promotion/crm.py`, `tests/promotion/conftest.py:86-214`.
  Why: `a_finished_run`, `FakeCrm`, `MockPortal`, `make_client`, the `promotion` fixture.
- `tests/cadence/test_routes.py:18-98`. Why: overriding a service dependency in a route test.
- `tests/promotion/test_cli.py:66-81`. Why: CLI tests stub service methods with `monkeypatch.setattr`.
- `tests/test_structure.py`. Why: the guards a new slice must pass. No empty packages, no `Any`, no
  suppressions, one Alembic head, the `claude_agent_sdk` allowlist, `docker-compose` has only `app`.

### New Files to Create

- `app/runs/__init__.py`: empty marker (the package has code, so the no-empty-package guard passes).
- `app/runs/README.md`: the slice README (key flows, table, rules, integration points).
- `app/runs/models.py`: `WeeklyRun`.
- `alembic/versions/0011_weekly_run.py`.
- `app/runs/schemas.py`: `WeeklyRunStatus`, `WeeklyRunRequest`, `WeeklyRunResponse`, `TouchKind`,
  `PlaybookNumber`, `RunSummary`, `WeeklyReport`.
- `app/runs/exceptions.py`: `WeeklyRunError`, `WeeklyRunInProgressError` (409),
  `WeeklyRunNotFoundError` (404).
- `app/runs/repository.py`: `WeeklyRunRepository`.
- `app/runs/service.py`: `WeeklyRunService`.
- `app/runs/touches.py`: `classify_note`, `week_window`, the pure core of the report.
- `app/runs/report.py`: `WeeklyReportService`.
- `app/runs/routes.py`: `POST /runs`, `GET /runs/weekly-report`, `GET /runs/{run_id}`.
- `app/runs/cli.py`: `lpe runs start | show | report`.
- `tests/runs/__init__.py`, `tests/runs/conftest.py`, `tests/runs/test_touches.py`,
  `tests/runs/test_models.py`, `tests/runs/test_service.py`, `tests/runs/test_report.py`,
  `tests/runs/test_routes.py`, `tests/runs/test_cli.py`.

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [FastAPI: Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/). The task runs
  after the response is sent, in the same process and event loop. The background function must open its
  own DB session.
- [FastAPI: Dependencies with yield, and background tasks](https://fastapi.tiangolo.com/tutorial/dependencies/dependencies-with-yield/#dependencies-with-yield-and-background-tasks).
  Since 0.106 the exit code runs **before** background tasks. This confirms the request session is
  closed by then.
- [HubSpot CRM search API](https://developers.hubspot.com/docs/api/crm/search). Covers the
  `GTE`/`LT` operators and that datetime filter values are **epoch milliseconds**. Paging uses
  `after` in the body and `paging.next.after` in the response. Results cap at 10,000 per query, and the
  limit is 200 per page.
- [HubSpot Notes API](https://developers.hubspot.com/docs/api/crm/notes). `hs_note_body` is HTML;
  `hs_body_preview` is plain text (seen in the live read 2026-10-17); `hs_timestamp` is the activity
  date.
- [PostgreSQL partial unique index](https://www.postgresql.org/docs/16/indexes-partial.html). Used as
  the one-running-at-a-time guard.

### Patterns to Follow

**Naming:** slice `runs`, table `weekly_run`, model `WeeklyRun`, service `WeeklyRunService`, CLI group
`lpe runs`. Log events are `runs.<component>.<action>_<state>`, e.g. `runs.weekly.run_started`,
`runs.weekly.run_finished`, `runs.weekly.run_failed`, `runs.weekly.runs_reaped`,
`runs.report.notes_read`.

**Service owns the transaction; repository flushes** (`app/sourcing/service.py:9`,
`app/cadence/repository.py:3-5`).

**Compare-and-set finish** (mirror `app/sourcing/repository.py:78-90`):
```python
result = await self._session.scalars(
    update(WeeklyRun)
    .where(WeeklyRun.id == run_id, WeeklyRun.status == WeeklyRunStatus.running.value)
    .values(status=status.value, finished_at=func.now(), ...)
    .returning(WeeklyRun),
    execution_options={"populate_existing": True},
)
```

**Errors:** a domain exception with `default_code` and `status_code` ClassVars (see
`RunNotPromotableError`, `app/promotion/exceptions.py:182-191`). Handlers have no try/except.

**Route dependency** (mirror `app/cadence/routes.py:27-32`), except that the weekly-run service takes the
**session factory**, not the request session:
```python
def get_weekly_run_service() -> WeeklyRunService:
    return WeeklyRunService(get_sessionmaker())
WeeklyRunServiceDep = Annotated[WeeklyRunService, Depends(get_weekly_run_service)]
```

**CLI:** `register(parser)` / `dispatch(args) -> int`, `asyncio.run`. Close the engine and the HubSpot
client in `finally` (`app/promotion/cli.py:93-103`). Stdout is for a person; warnings go to stderr.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation (gateway, sourcing helpers, settings)
Search operators and paging on the HubSpot gateway; the stage-configuration helpers and `on_started` in
`app/sourcing/pipeline.py`; two settings.

### Phase 2: The slice core (model, migration, schemas, repository)
**Depends on:** nothing in Phase 1 (disjoint files). It can run in parallel with Phase 1.

### Phase 3: The weekly run (service, routes, CLI)
**Depends on:** Phases 1 and 2.

### Phase 4: The Friday report (touches, review tally, report service, route, CLI)
**Depends on:** Phase 1 (search), Phase 2 (repository). **Independent of:** Phase 3's service, apart from
the shared `routes.py` / `cli.py` files.

### Phase 5: Docs and validation

---

## STEP-BY-STEP TASKS

Write each test first and watch it fail, then implement (house practice).

### Task 1: UPDATE `app/core/config.py` and `.env.example`
- **IMPLEMENT**: Add a `# --- Weekly run (T10) ---` block:
  - `weekly_run_stale_hours: int = Field(default=2, gt=0)`. A weekly run still `running` after this long
    was killed, and the next start marks it failed. 2h, against a 1h M7 target. **Must be less than**
    `sourcing_stale_run_hours` (6), so a reaped weekly run never leaves the next start blocked behind its
    still-`running` sourcing run. That case cannot happen anyway, because sourcing reaps per vertical at
    its own start, but keep the ordering documented.
  - `weekly_run_target_minutes: int = Field(default=60, gt=0)`. The M7 target the report compares against.
  - Mirror both in `.env.example` with a comment each.
- **PATTERN**: `sourcing_stale_run_hours` (`app/core/config.py:55`).
- **VALIDATE**: `uv run pytest tests/core/test_config.py -q`
- **SATISFIES**: AC 3, AC 4

### Task 2: UPDATE `app/promotion/schemas.py` (search operators and paging)
- **IMPLEMENT**:
  - Add `gte = "GTE"` and `lt = "LT"` to `SearchOperator`. Extend its docstring: "`gte`/`lt` for the
    Friday report's week window on `hs_timestamp` (epoch milliseconds)".
  - Add `after: str | None = None` to `SearchRequest` (serialized as `after`; dropped when `None` by
    `exclude_none`).
  - Add `SearchPagingNext(after: str)`, `SearchPaging(next: SearchPagingNext | None = None)`, and
    `paging: SearchPaging | None = None` on `SearchResponse` (all `_RESPONSE_CONFIG`).
- **GOTCHA**: `SearchRequest` is `extra="forbid"`; existing callers pass no `after`, so nothing changes for
  dedupe or adoption. Keep `limit` default 10; the report passes 200.
- **VALIDATE**: `uv run pytest tests/promotion/test_client.py tests/promotion/test_dedupe.py -q`
- **SATISFIES**: AC 6

### Task 3: UPDATE `app/sourcing/pipeline.py` and `app/sourcing/cli.py` (shared stage options, `on_started`)
- **IMPLEMENT**:
  - Move `_stages` and `_configured` from `app/sourcing/cli.py:89-100` into `pipeline.py` as **public**
    `configured_stages(*, batch_size: int | None, allow_degraded: bool) -> tuple[Stage, ...]`. Add
    `preflight(stages: Sequence[Stage], body: ManifestBody) -> None`, which calls
    `VerifyBusinessStage.preflight` on each one, as the CLI's loop at `:108-110` does.
  - Point `app/sourcing/cli.py` at them, so `lpe sourcing run` behaves exactly as before.
  - Add a keyword-only parameter to `SourcingPipeline.run`:
    `on_started: Callable[[UUID], Awaitable[None]] | None = None`. Await it right after `start_run`
    (`:79`), **inside** the `try`, so a failing callback fails the run like a stage would.
- **IMPORTS**: `from app.tools.search_registry import SearchRegistryStage`,
  `from app.tools.verify_business import VerifyBusinessStage`, `from app.manifests.schemas import
  ManifestBody` (check the exact class name `preflight` takes, `verify_business.py:181`).
- **GOTCHA**: `tests/sourcing/test_cli.py` monkeypatches `app.sourcing.cli.registered_stages`. After the
  move, `configured_stages` reads `registered_stages` from `pipeline`'s namespace. Either keep a
  `registered_stages` lookup the test can patch (pass it in: `configured_stages(...,
  stages=registered_stages())` called from the CLI), or update the test's patch target. **Prefer the
  former**, so the CLI test is unchanged.
- **GOTCHA**: watch for import cycles. `app.tools.verify_business` imports `app.sourcing.*` modules; the
  CLI already imports both, so pipeline importing them should be safe. Run the import check below.
- **VALIDATE**: `uv run python -c "import app.sourcing.pipeline, app.sourcing.cli" && uv run pytest tests/sourcing/test_pipeline.py tests/sourcing/test_cli.py -q`
- **SATISFIES**: AC 1, AC 2

### Task 4: CREATE `app/runs/models.py` + `alembic/versions/0011_weekly_run.py`; UPDATE `alembic/env.py`
- **IMPLEMENT** `WeeklyRun(Base)`, table `weekly_run`:
  | column | type | notes |
  |---|---|---|
  | `id` | `PgUUID`, PK, `default=uuid4` | |
  | `vertical` | `String(64)`, not null | |
  | `status` | `String(16)`, not null | CHECK `status in ('running','completed','degraded','failed')` |
  | `status_detail` | `Text`, null | |
  | `dry_run` | `Boolean`, not null, `server_default=false()` | |
  | `batch_size` | `Integer`, null | CHECK `batch_size is null or batch_size > 0` |
  | `allow_degraded` | `Boolean`, not null, `server_default=false()` | |
  | `requested_at` | timestamptz, not null, `server_default=func.now()` | M7 start |
  | `sourcing_run_id` | FK `sourcing_run.id`, null, **unique** | set by `on_started` |
  | `sourced_at` | timestamptz, null | sourcing finished |
  | `promoted_at` | timestamptz, null | promotion finished: M7 end |
  | `finished_at` | timestamptz, null | CHECK `(status = 'running') = (finished_at is null)` |
  | `promotion_counts` | JSONB, not null, default `{}` | `PromotionReport.counts` snapshot |
  | `promotion_stopped` | `Text`, null | `PromotionReport.stopped` |
  - CHECK `promoted_at is null or sourced_at is not null`.
  - **Partial unique index** `uq_weekly_run_one_running` on `(status)` `WHERE status = 'running'`: at most
    one running weekly run, globally.
  - Index `ix_weekly_run_requested_at` on `requested_at` (the report's window query).
  - Module docstring: what the table is for (M7, linking a weekly run to its sourcing run and promotion);
    why the guard is an index (see NOTES); machine state that never reaches HubSpot.
  - Migration `0011_weekly_run`, `down_revision = "0010_promotion"`. Hand-write every constraint and
    index to match the model; `downgrade` drops the table.
  - `alembic/env.py`: import `app.runs.models` and append `runs_models.WeeklyRun` to
    `_REGISTERED_MODELS`.
- **PATTERN**: `app/sourcing/models.py:55-111`, `app/promotion/models.py`, `0010_promotion.py`.
- **GOTCHA**: `postgresql_where=text("status = 'running'")` on the `Index`, in both places. Use the same
  constraint names in the model and the migration, or `alembic check` reports drift.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q` then (DB tier) `uv run alembic upgrade head && uv run alembic check` against the test database (see Level 3)
- **SATISFIES**: AC 3, AC 4

### Task 5: CREATE `app/runs/schemas.py` and `app/runs/exceptions.py`
- **IMPLEMENT** in `schemas.py`:
  - `WeeklyRunStatus(StrEnum)`: `running, completed, degraded, failed`. A test pins it to the CHECK.
  - `WeeklyRunRequest(BaseModel, extra="forbid", frozen)`:
    - `vertical: str = Field(pattern=SLUG_PATTERN, max_length=64)`
    - `batch_size: int | None = Field(default=None, gt=0)`
    - `allow_degraded: bool = False`
    - `dry_run: bool = False`
  - `WeeklyRunResponse(from_attributes)`:
    - `id`, `vertical`, `status`, `status_detail`, `dry_run`, `batch_size`, `allow_degraded`
    - `requested_at`, `sourcing_run_id`, `sourced_at`, `promoted_at`, `finished_at`
    - `promotion_counts: dict[str, int]`, `promotion_stopped`
    - `sourcing: SourcingRunResponse | None = None`, filled by the service, not the ORM
    - computed `brief_to_hubspot_minutes: float | None`, which is
      `(promoted_at - requested_at) / 60s` when `promoted_at` is set
  - `TouchKind(StrEnum)`: `door_knock, owner_conversation, assessment_booked`.
  - `PlaybookNumber(BaseModel, frozen)`:
    - `name: str`
    - `value: int | None`
    - `configured: bool`
    - A validator enforces `configured == (value is not None)`. **There is no way to build "not
      configured" with a 0.**
  - `ReviewTally` is **imported from `app.promotion.schemas`** (Task 7 defines it there, because
    promotion must not import `runs`).
  - `RunSummary(BaseModel, frozen)`:
    - `run: WeeklyRunResponse`
    - `within_target: bool | None`
    - `total_usd: Decimal | None`
    - `funnel: dict[str, int | None]`, where `None` means the stage never reported that key
    - `promotion: dict[str, int]`
    - `review: ReviewTally`
  - `WeeklyReport(BaseModel, frozen)`:
    - `week_start: date`, `week_end: date` (inclusive Sunday)
    - `timezone: str`
    - `playbook: tuple[PlaybookNumber, ...]`
    - `unclassified_touches: int`
    - `runs: tuple[RunSummary, ...]`
    - `target_minutes: int`
  - Constants:
    - `FUNNEL_COUNTERS` (tuple, in report order): `batched, qualification_disqualified,
      places_verified, verify_website_only, places_mail_drop, places_no_listing, places_other_business,
      judgment_disqualified, owner_absent, owner_unverified_skipped, routing_mail_drop,
      routing_geocode_failed, routing_clustered`
    - `PROMOTION_COUNTERS`: `created, promoted_unverified, refused_contradicted, refused_no_principal,
      refused_missing_fields, refused_disqualified, duplicate_in_hubspot, failed`
    - A test asserts each name exists in its stage's `COUNT_KEYS` or in `PROMOTE_COUNTS`, so a renamed
      counter breaks a test, not the report.
- **IMPLEMENT** in `exceptions.py`:
  - `WeeklyRunError(LocalProspectEngineError)`, code `weekly_run_error`.
  - `WeeklyRunInProgressError`: 409, `weekly_run_in_progress`. The message names the running run's id
    and when it started.
  - `WeeklyRunNotFoundError`: 404, `weekly_run_not_found`.
- **PATTERN**: `app/sourcing/schemas.py:78-92, 714-729`; `app/promotion/exceptions.py:182-191`.
- **GOTCHA**: no `Any` anywhere, and `dict[str, int | None]` is fine. `Decimal` is serialized as a string
  in JSON (Pydantic v2 default).
- **VALIDATE**: `uv run pytest tests/runs/test_models.py -q && uv run mypy app/runs && uv run pyright app/runs`
- **SATISFIES**: AC 4, AC 7

### Task 6: CREATE `app/runs/repository.py`
- **IMPLEMENT** `WeeklyRunRepository(session)`:
  - `create(request) -> WeeklyRun`: insert and flush. Catch `IntegrityError` **only** when
    `uq_weekly_run_one_running` is the violated constraint, and return `None` (any other integrity error
    re-raises). Mirror the `UNIQUE_CONTACT` check in `app/cadence/service.py:72-74`.
  - `get(run_id)`.
  - `running() -> WeeklyRun | None`.
  - `record_sourcing_run(run_id, sourcing_run_id)`.
  - `mark_sourced(run_id)`: `sourced_at = func.now()`.
  - `finish(run_id, status, *, detail, promotion_counts, promotion_stopped, promoted)`: compare-and-set
    on `running`; `promoted_at = func.now()` when `promoted`; `finished_at = func.now()`. Returns `None`
    if no longer running.
  - `reap_stale(older_than) -> list[UUID]`: mark `running` rows with `requested_at < now() - older_than`
    as `failed`, detail `abandoned: still running after {h}h`.
  - `requested_between(start, end) -> Sequence[WeeklyRun]`: ordered by `requested_at`.
  - Every timestamp uses the **database clock**, as `sourcing_run` does, so M7 never mixes two clocks.
- **PATTERN**: `app/sourcing/repository.py:63-132`.
- **VALIDATE**: `uv run pytest tests/runs/test_service.py -q -k repository` (DB tier)
- **SATISFIES**: AC 3, AC 4

### Task 7: UPDATE `app/promotion/repository.py` + `service.py`: read-only `review_tally`
- **IMPLEMENT**:
  - `PromotionRepository.status_counts_by_run(run_ids: Collection[UUID]) -> dict[UUID, dict[str, int]]`:
    one `select run_id, status, count(*) … group by`.
  - `PromotionService.review_tally(run_ids) -> dict[UUID, ReviewTally]`:
    - `pending` = `pending_review`
    - `accepted` = `enrolling` + `enrolled`
    - `rejected` = `rejected`
    - `withdrawn` = `withdrawn`
    - `pending_create` and `duplicate` are excluded (not reviewable)
    - Runs with no rows get all zeros
  - Read-only: **no HubSpot call, no commit**.
- **IMPLEMENT** `ReviewTally(BaseModel, frozen)` in `app/promotion/schemas.py`: `pending`, `accepted`,
  `rejected`, `withdrawn` (all `int`). Dependencies point toward `runs`, never from promotion into it.
- **GOTCHA**: a ledger row's `run_id` is the run that **claimed** it. A later run's `already_promoted`
  hit does not move the row (T9). Say so in the docstring: the tally is "decisions on what this run
  created".
- **VALIDATE**: `uv run pytest tests/promotion/test_ledger.py -q` plus the new test in
  `tests/runs/test_report.py`
- **SATISFIES**: AC 7

### Task 8: CREATE `app/runs/service.py`: `WeeklyRunService`
- **IMPLEMENT**:
  ```python
  class WeeklyRunService:
      def __init__(
          self,
          session_factory: SessionFactory,
          *,
          hubspot: HubSpotFactory = get_hubspot_client,
          stages: Callable[[int | None, bool], tuple[Stage, ...]] | None = None,  # tests
      ) -> None: ...
      async def start(self, request: WeeklyRunRequest) -> WeeklyRunResponse: ...
      async def execute(self, run_id: UUID) -> WeeklyRunResponse: ...
      async def run(self, request: WeeklyRunRequest) -> WeeklyRunResponse:  # CLI: start + execute
      async def get(self, run_id: UUID) -> WeeklyRunResponse: ...
  ```
  - **`start`**, on one session:
    1. `reap_stale(settings.weekly_run_stale_hours)`.
    2. `ManifestService(session).get_active(vertical)` (404 `ActiveManifestNotFoundError`).
    3. Build the stages: `configured_stages(...)`, or the injected `stages`.
    4. `preflight(stages, manifest.body)` (409 `SourceUnavailableError`, before anything is written).
    5. `create(request)`; on `None`, raise `WeeklyRunInProgressError` naming `running()`.
    6. Commit and log `runs.weekly.run_started`. Return the response.
  - **`execute`**:
    1. Load the row; it must be `running`.
    2. Re-read the ACTIVE manifest, rebuild the stages from the row's stored options, and build
       `SourcingBrief(vertical, geography=DEFAULT_GEOGRAPHY, icp_band=manifest.body.icp_band.value)`.
       Reuse the CLI's `"dfw"` constant: move it to `pipeline.py` with the helpers.
    3. Call `SourcingPipeline(self._session_factory, stages=stages).run(brief, on_started=…)`. The
       callback writes `sourcing_run_id` on a **fresh** session and commits.
    4. If the pipeline raises: finish `failed` with detail
       `sourcing failed: {Type}: {msg}`[:500] (and no promotion), log `runs.weekly.run_failed`, then
       **re-raise only `BaseException` that is not `Exception`** (cancellation, Ctrl-C). An `Exception`
       is recorded and swallowed, because a background task has nobody to raise to. Return the
       finished response.
    5. `mark_sourced` and commit.
    6. Call `PromotionService(session, hubspot=self._hubspot).promote_run(sourcing.id,
       dry_run=row.dry_run)`. If it raises: finish `failed`, detail `promotion failed: …`, with
       `promoted=False`.
    7. Finish with:
       - `degraded` when `sourcing.status is degraded`, or `report.stopped`, or
         `report.counts["failed"] > 0`
       - else `completed`
       - `promoted=True`, `promotion_counts=dict(report.counts)`, `promotion_stopped=report.stopped`
       - The detail joins `sourcing.status_detail` and `promotion stopped: …` / `{n} promotion(s)
         failed`.
       - Log `runs.weekly.run_finished` with the status, `brief_to_hubspot_minutes` and `total_usd`.
  - Bind `weekly_run_id` with `structlog.contextvars` for the duration of `execute`, and unbind in
    `finally`.
  - **A dry run still sets `promoted_at`.** M7 measures the run's duration. The report marks dry runs.
  - `get`: the row plus `sourcing=SourcingService(session).get_run(id)` when `sourcing_run_id` is set.
- **GOTCHA**: never call `dispose_engine()` or `aclose_hubspot_client()` here. The route path shares the
  process engine and the client singleton; only the CLI closes them.
- **GOTCHA**: `execute` opens its **own** sessions from the factory, because the request session is
  already closed when a background task runs.
- **GOTCHA**: if the process dies mid-run, the weekly row stays `running`, and so does its sourcing run.
  The next `start` reaps the weekly row after 2h. The sourcing reaper (6h, per vertical, at the next
  pipeline start) handles the sourcing row. A fresh start between 2h and 6h is fine, because the new
  pipeline's own reaper only fails runs older than 6h, and `start_run` has no running-run guard (T5).
  Document this in the README.
- **VALIDATE**: `uv run pytest tests/runs/test_service.py -q` (DB tier)
- **SATISFIES**: AC 1, AC 2, AC 3, AC 4, AC 5

### Task 9: CREATE `app/runs/touches.py`: the pure core of the report
- **IMPLEMENT**:
  - `PREFIXES: Mapping[TouchKind, str] = {door_knock: "door knock", owner_conversation: "owner
    conversation", assessment_booked: "assessment booked"}`.
  - `classify_note(text: str | None) -> TouchKind | None`:
    1. Strip leading characters that are not letters or digits (emoji, `—`, spaces, punctuation).
    2. Lowercase.
    3. Replace `-` and `_` with a space and collapse whitespace.
    4. Return the kind whose label the text **starts with**, else `None`.
  - `week_window(week_of: date, tz: ZoneInfo) -> tuple[datetime, datetime, date, date]`: the Monday 00:00
    through next Monday 00:00 containing `week_of`, local time, returned as **aware UTC** datetimes plus
    the local Monday and Sunday dates.
  - `epoch_ms(moment: datetime) -> str`: the HubSpot filter value.
- **GOTCHA**: build the window as `datetime.combine(monday, time(), tzinfo=tz)`, never as fixed UTC
  offsets. This is the DST-safe way; the week of 2026-11-02 crosses the fall-back change, so test it.
- **GOTCHA**: match real notes from the portal, used as test cases. `🚪 Door knock — 9/29/2026` and
  `Door-knock visit, 9/24/26` must be door knocks. `Cold call 9/22/2026 - No answer.`, `connected with
  Paul…` and `Call with Jorge Rodriguez…` are `None` (unclassified; the convention is new).
- **VALIDATE**: `uv run pytest tests/runs/test_touches.py -q`
- **SATISFIES**: AC 6

### Task 10: CREATE `app/runs/report.py`: `WeeklyReportService`
- **IMPLEMENT** `WeeklyReportService(session, *, hubspot: HubSpotFactory = get_hubspot_client, clock:
  Clock = utc_now)` with `build(week_of: date | None = None) -> WeeklyReport`. `None` means the week
  containing `clock()` in `CADENCE_TZ`.
  1. **Notes.**
     - Page through `client.search(ObjectType.notes, SearchRequest(filter_groups=[one group: hs_timestamp
       GTE start_ms, hs_timestamp LT end_ms], properties=["hs_body_preview", "hs_timestamp"], limit=200,
       after=…))` until `paging` is `None`.
     - Cap at 20 pages. Past the cap, raise `HubSpotResponseShapeError`-style `WeeklyRunError("more than
       4,000 notes in one week …")`, never a silent truncation.
     - Classify each note's `hs_body_preview`. Count per kind, plus `unclassified_touches`.
     - Log `runs.report.notes_read` with the totals and **no note text**.
  2. **Playbook.** In this order:
     - `doors knocked`: counted
     - `owner conversations`: counted
     - `assessments booked`: counted
     - `paid assessments sold`: `PlaybookNumber(value=None, configured=False)`, GATE-A. Add a comment
       citing D10.
  3. **Runs.**
     - Read `requested_between(start, end)`, then `SourcingService.get_run` for each `sourcing_run_id`.
     - `funnel` comes from `sourcing.counts.get(name)` (absent stays `None`).
     - `promotion` comes from `run.promotion_counts.get(name, 0)`. These are always all keys when
       promoted; an un-promoted run shows an empty dict.
     - `within_target` is `brief_to_hubspot_minutes <= settings.weekly_run_target_minutes`, or `None`
       if not promoted.
     - `total_usd` is `sourcing.cost.total_usd()`.
     - `review` comes from `PromotionService(session).review_tally([...sourcing ids])`.
- **GOTCHA**: HubSpot's search index lags writes by seconds to minutes. A note logged just before the
  report runs may be missed. That is acceptable on a Friday report; say so in the docstring.
- **GOTCHA**: a 429 or 5xx is retried by the client. An auth error propagates as `HubSpotAuthError`
  (rendered by `app.main`). Let it raise, because a report with a missing section is a wrong report.
- **VALIDATE**: `uv run pytest tests/runs/test_report.py -q`
- **SATISFIES**: AC 6, AC 7

### Task 11: CREATE `app/runs/routes.py`; UPDATE `app/main.py`
- **IMPLEMENT** `router = APIRouter(prefix="/runs", tags=["runs"])`:
  - `POST ""`, `status_code=202`, `response_model=WeeklyRunResponse`. Body is `WeeklyRunRequest`. Call
    `run = await service.start(request)`, then `background_tasks.add_task(service.execute, run.id)`, and
    return `run`. The docstring says who calls it (launchd weekly, or ad hoc), that a second call while
    one runs returns 409, and that progress is at `GET /runs/{id}`.
  - `GET "/weekly-report"`, `response_model=WeeklyReport`, query `week: date | None = None`. Uses
    `WeeklyReportService(db)`. **Declare it before `/{run_id}`.**
  - `GET "/{run_id}"`, `response_model=WeeklyRunResponse`. Returns 404 `WeeklyRunNotFoundError`.
  - Dependencies:
    - `get_weekly_run_service() -> WeeklyRunService(get_sessionmaker())`
    - `get_weekly_report_service(db: DbSession) -> WeeklyReportService(db)`
  - `app/main.py`: `app.include_router(runs_router)`. Update the module docstring's "The weekly-run
    trigger endpoint is T10" line.
- **GOTCHA**: in tests, `httpx.ASGITransport` awaits background tasks before the response returns, so a
  route test sees the finished run on its next `GET`. In production the 202 returns first. Document both
  in the test.
- **VALIDATE**: `uv run pytest tests/runs/test_routes.py -q` (DB tier)
- **SATISFIES**: AC 1, AC 5, AC 6

### Task 12: CREATE `app/runs/cli.py`; UPDATE `app/cli.py`
- **IMPLEMENT** `lpe runs`:
  - `start --vertical V [--batch-size N] [--allow-degraded] [--dry-run]`.
    - Calls `WeeklyRunService(get_sessionmaker()).run(request)` in-process.
    - Prints:
      - The run line: id, vertical, status, plus `DRY RUN` when it was one.
      - `brief → HubSpot  {m:.0f} min (target {t})`.
      - Cost via `format_usd`.
      - The funnel and promotion counters, aligned as `app/sourcing/cli.py:_print` does.
    - Exit codes:
      - 0: completed.
      - 0 with a `warning:` on stderr: degraded.
      - 1: failed.
      - 1: refused (409/404), through `app/cli.py`'s handler.
  - `show <run_id>`: the same rendering from `get`.
  - `report [--week YYYY-MM-DD]`: renders the `WeeklyReport` as sentences a person reads on a Friday.
    - The four playbook numbers first, with paid sold as `not configured (no $999 assessment pipeline,
      GATE-A)`.
    - Then `unclassified touches: N (log as "Door knock — …", "Owner conversation — …", "Assessment booked
      — …" to count them)`.
    - Then one block per run: M7 vs target, cost, funnel, promotion, review accepted/rejected/pending.
  - `finally`: `dispose_engine()` and `aclose_hubspot_client()` (the CLI pattern).
  - `app/cli.py`: import, register (`help="the weekly run and the Friday report"`) and dispatch.
- **PATTERN**: `app/promotion/cli.py:36-115`, `app/sourcing/cli.py:43-127`.
- **VALIDATE**: `uv run pytest tests/runs/test_cli.py -q && uv run lpe runs --help`
- **SATISFIES**: AC 2, AC 6

### Task 13: CREATE the tests (each failing first; list in TESTING STRATEGY)
- `tests/runs/conftest.py`:
  - `weekly_service`: `WeeklyRunService(session_factory, hubspot=lambda: crm_client, stages=lambda b, a:
    (Counts(...),))`. Reuse `session_factory` from `tests/sourcing/conftest.py` and `crm` /
    `crm_client` from `tests/promotion/conftest.py`. Lift them into a shared conftest only if the
    import is awkward. Pytest plugins can be referenced with `pytest_plugins` in `tests/runs/conftest.py`.
  - An active manifest helper that reuses `tests/promotion/scenario.py`'s manifest creation.
- **VALIDATE**: `uv run pytest tests/runs -q`
- **SATISFIES**: all

### Task 14: UPDATE docs
- `app/runs/README.md`:
  - Key flows (start/execute diagram) and the `weekly_run` table.
  - The guard and the reapers (2h weekly, 6h sourcing).
  - The note-prefix convention and the reason for it (live read 2026-10-17).
  - Integration: reads sourcing and promotion through their services, never writes their tables.
- `CLAUDE.md`:
  - Architecture map: add the `app/runs/` line and the `0011` migration.
  - Rewrite the "Today" paragraph: all tickets shipped.
  - Commands: `lpe runs start | show | report`, `POST /runs`, `GET /runs/{id}`,
    `GET /runs/weekly-report`, the launchd note.
  - Remove the "until T10" wording from the sourcing and promotion command lines.
  - **Fix the test-database port** to the one actually used (`lpe-test-pg`, **5434**, per
    `_tasks/todo.md` 2026-10-16).
- `.claude/references/hubspot-integration.md`:
  - A *Friday report* section with the note-prefix convention as the founders' logging rule.
  - `RecordMeta` lists five properties (it says four).
- `docs/tickets/local-prospect-engine.md`, T10:
  - The three decisions (dated 2026-10-17).
  - "Routes after qualification: done by T9 OQ8-B".
  - Files: `app/runs/*` replaces `app/sourcing/routes.py` and `app/reporting/*`.
- `app/sourcing/README.md` / `app/promotion/README.md`: one line each, "called by `app/runs/`".
- `_tasks/todo.md`: tick items as they land; append a *Review* section at the end.
- **VALIDATE**: `grep -rn "until T10" CLAUDE.md app/ docs/` returns nothing stale.
- **SATISFIES**: AC 8

---

## TESTING STRATEGY

Tests live in `tests/runs/` (house layout). DB-tier suites set `pytestmark = requires_db`.

### Unit Tests (pure, no DB)
- `test_touches.py`:
  - Every real portal note from the 2026-10-17 read: the two door-knock styles classify as `door_knock`;
    the cold-call, voicemail and "connected with Paul" notes are `None`.
  - Variants: `DOOR KNOCK`, `door-knock`, `  📅 Assessment booked — Acme`, `Owner conversation:`.
  - `None` and `""` map to `None`.
  - A label mid-sentence (`Called; door knock tomorrow`) is `None`, because the match is a prefix.
  - `week_window`:
    - A Wednesday maps to Monday through Monday.
    - A Monday 00:30 local maps to that week.
    - A Sunday 23:30 local maps to the previous Monday.
    - The DST week (2026-11-02) is 169 hours long.
    - Every returned datetime is UTC-aware.
  - `epoch_ms` is correct for a known instant.
- `test_models.py`:
  - `WeeklyRunStatus` matches the CHECK set.
  - `WeeklyRunRequest` refuses a bad slug, `batch_size=0` and extra keys.
  - `PlaybookNumber(value=0, configured=False)` raises, as does `(value=None, configured=True)`.
  - `brief_to_hubspot_minutes` is `None` until promoted, and correct after.
  - Every name in `FUNNEL_COUNTERS` is in some stage's `COUNT_KEYS` / `search_registry` / routing keys,
    and every name in `PROMOTION_COUNTERS` is in `PROMOTE_COUNTS`.
- `tests/promotion/test_client.py` additions:
  - `GTE`/`LT` serialize as `"GTE"`/`"LT"`.
  - `after` is present when set and absent when `None`.
  - `paging.next.after` parses, and a response without `paging` gives `None`.

### Integration Tests (DB tier + fakes)
- `test_service.py`:
  - **Happy path**: fake stages report counts. `promote_run` runs against `FakeCrm`.
    - The row ends `completed` with `sourcing_run_id` set.
    - `requested_at <= sourced_at <= promoted_at <= finished_at`.
    - `promotion_counts == report.counts`.
  - **Dry run**: `FakeCrm` records no writes, and `promoted_at` is set.
  - **Sourcing degraded**: a stage reports `degraded_reason`, so the weekly status is `degraded` with the
    reason in the detail, and promotion **still ran**.
  - **Sourcing fails** (`Boom` stage):
    - The weekly row is `failed`, with detail `sourcing failed: …`.
    - `sourcing_run_id` is set (the `on_started` callback ran) and that sourcing run is `failed`.
    - `promote_run` is never called (assert with a spy).
    - `execute` returns and does not raise.
  - **Promotion raises** (monkeypatch `promote_run` to raise `HubSpotAuthError`): the row is `failed`,
    `sourced_at` is set, and `promoted_at` is null.
  - **Promotion stopped** (`report.stopped` set) or `failed > 0`: the row is `degraded`.
  - **Guard**:
    - A second `start` while one is running raises `WeeklyRunInProgressError` (409) and names the first
      run.
    - After `finish`, a new `start` succeeds.
  - **Stale reap**: backdate a running row's `requested_at` by 3h with raw SQL. `start` reaps it to
    `failed` with the `abandoned` detail, then starts.
  - **Preflight**: a manifest declaring Places with no key and `allow_degraded=False` gives
    `SourceUnavailableError`, and **no `weekly_run` row is written**.
  - **No ACTIVE manifest**: `ActiveManifestNotFoundError` and no row.
  - **Cancellation**: `execute` with a stage raising `asyncio.CancelledError` records `failed`, then
    re-raises.
  - `tests/sourcing/test_pipeline.py` additions:
    - `on_started` receives the run id before the first stage.
    - A raising callback fails the run like a stage.
    - No callback means unchanged behaviour.
- `test_report.py` (DB + `MockPortal` for the notes search):
  - Two pages of notes (`paging.next.after`) are counted across both pages, and the second request
    carries `after`.
  - The request body has the two filters with epoch-ms values for the right week.
  - Mixed notes produce the correct playbook counts and `unclassified_touches`.
  - Paid sold is `configured=False, value=None`, and the JSON shows `null`, never `0`.
  - Runs outside the window are excluded.
  - A run with a missing funnel key shows `None`.
  - `within_target` is true or false against `weekly_run_target_minutes`.
  - `review` tally:
    - Ledger rows `pending_review`, `enrolling`, `enrolled`, `rejected` and `withdrawn` count correctly.
    - `pending_create` and `duplicate` are excluded.
    - A run with no rows gets zeros.
  - The page cap raises rather than truncating.
- `test_routes.py`:
  - `POST /runs` returns 202 with `status: running`, then `GET /runs/{id}` shows `completed` (the
    background task ran under `ASGITransport`).
  - `POST` twice with the first still running (seed a running row directly) returns 409 with
    `code == "weekly_run_in_progress"`.
  - `POST` with an unknown vertical returns 404 `active_manifest_not_found`.
  - A bad body returns 422.
  - `GET /runs/{uuid4()}` returns 404 `weekly_run_not_found`.
  - `GET /runs/weekly-report?week=2026-10-12` returns 200 with the four numbers (report service
    overridden).
  - `GET /runs/not-a-uuid` returns 422.
- `test_cli.py` (stub the service methods, as `tests/promotion/test_cli.py:66-81` does):
  - `start` prints the M7 line, the cost and the counters, with exit codes 0, 0 (degraded, plus a stderr
    warning) and 1 (failed).
  - `report` prints `not configured` for paid sold and the unclassified hint.
  - `show` on an unknown id gives exit 1 with `error:` on stderr.

### Edge Cases
- Two `POST /runs` racing: the partial unique index lets exactly one insert succeed (also covered by the
  guard test).
- The process dies mid-run: the stale reap test.
- The manifest is deactivated between `start` and `execute`: `execute` records `failed` with the
  manifest error and does not raise.
- A sourcing run that batches 0 (empty pool): promotion considers 0, the run is `completed`, and the
  report shows zeros, not `None`, for keys the stages did report.
- A week with no notes and no runs: all three counted numbers are `0` (a real, instrumented zero), paid
  sold is `not configured`, and the runs list is empty.
- A note with an empty preview: unclassified.
- The week boundary in Dallas time versus UTC: a note at Monday 03:00 UTC (Sunday evening in Dallas)
  belongs to the previous week.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style
`uv run ruff check . && uv run ruff format --check .` · `uv run mypy . && uv run pyright`

### Level 2: Unit Tests
`uv run pytest tests/runs/test_touches.py tests/runs/test_models.py tests/promotion/test_client.py tests/test_structure.py -q`

### Level 3: Integration Tests (database tier)
```bash
docker start lpe-test-pg 2>/dev/null || docker run --name lpe-test-pg -d -p 5434:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/postgres
uv run pytest -q                       # full suite, DB tier included; expect 0 failures
DATABASE_URL=$TEST_DATABASE_URL uv run alembic upgrade head && DATABASE_URL=$TEST_DATABASE_URL uv run alembic check
```
Or everything at once: `/piv-validate`. (`uv` needs the sandbox disabled, per memory.)

### Level 4: Manual Validation (human-gated; no HubSpot writes outside `lpe`)
1. **Before anything**: apply `0011` to **dev** Supabase (`uv run alembic upgrade head`).
2. **Read-only report**: run `uv run lpe runs report --week 2026-09-28`. Expect 2 door knocks (the
   9/29 notes); owner conversations and assessments booked at 0; paid sold `not configured`; the
   prose call notes from 9/28 as unclassified. Cross-check one count in HubSpot by hand.
3. **Dry run end to end**: run `uv run lpe runs start --vertical freight --batch-size 5 --dry-run`.
   Expect a `completed` or `degraded` run, the M7 line, cost under ~$1, and no HubSpot writes
   (promotion dry run).
4. **Route**: run `uv run uvicorn app.main:app`, then
   `curl -s -XPOST localhost:8000/runs -H 'content-type: application/json' -d '{"vertical":"freight","batch_size":5,"dry_run":true}'`.
   Expect a 202 and a run id. A second POST returns 409. Poll `GET /runs/{id}` until finished.
5. **Human check (ticket T10, Spike 1 checkpoint)**: read M5 on the 22 adopted prospects before the
   **first real** run. If touches are still not being done, fix that first.
6. **First real run** (human decides when): `uv run lpe runs start --vertical freight`.
7. **launchd (human edit)**: add a weekly job (suggest Monday 06:00) that runs
   `uv run lpe runs start --vertical freight`, and a Friday job (suggest 07:30) that runs
   `uv run lpe runs report`, writing to a log file. Hit the endpoint instead once on the VPS.
8. **Tell the founders the logging convention**: start a note with `Door knock`, `Owner conversation`
   or `Assessment booked`.

### Level 5: Additional Validation
`/piv-review-changes` before the PR; `rules-check-drift` on CLAUDE.md after the doc edits.

---

## ACCEPTANCE CRITERIA

1. [ ] `POST /runs` validates and preflights synchronously (404 no active manifest, 409 source
   unavailable, 409 run in progress, 422 bad body). It returns 202 with the run, then runs source →
   qualify → verify/enrich → cluster → promote in the background.
2. [ ] `lpe runs start` runs the same service in-process, with `--batch-size`, `--allow-degraded` and
   `--dry-run`. `lpe sourcing run` and `lpe promotion promote` behave exactly as before.
3. [ ] At most one weekly run is `running` at a time (DB-enforced). A killed run is reaped after
   `WEEKLY_RUN_STALE_HOURS`.
4. [ ] Each weekly run records `requested_at`, `sourced_at`, `promoted_at` and `finished_at` on the DB
   clock. `brief_to_hubspot_minutes` is reported against `WEEKLY_RUN_TARGET_MINUTES` (M7). Per-run cost
   is reported beside it.
5. [ ] Failure semantics: a degraded sourcing run still promotes, and the weekly run is `degraded` with
   every reason. A failed sourcing run promotes nothing and is `failed`. A promotion that stops or has
   failures is `degraded`. A background failure is recorded, never lost.
6. [ ] The Friday report (`lpe runs report`, `GET /runs/weekly-report`) shows the four numbers:
   - Doors knocked, owner conversations and assessments booked come from prefixed HubSpot notes in the
     Dallas-time week.
   - Unclassified touches are shown.
   - **Paid assessments sold renders `not configured`, never `0`.**
7. [ ] Per run, the report shows the funnel and verification counters (`places_verified`,
   `verify_website_only`, `places_{mail_drop,no_listing,other_business}`, `routing_mail_drop`,
   `routing_geocode_failed`, `owner_absent`, `owner_unverified_skipped`). It also shows promotion's
   `created`, `promoted_unverified` and `refused_contradicted`, and M6 accept/reject/pending from the
   ledger.
8. [ ] Docs updated: slice README, CLAUDE.md (map, commands, test-DB port), the HubSpot reference, and
   ticket T10.
9. [ ] `/piv-validate` PASS: ruff, mypy, pyright, pytest (DB tier) and alembic with no drift. Zero
   suppressions and no `Any`.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully
- [ ] Full test suite passes (unit + integration)
- [ ] No linting or type checking errors
- [ ] Level 4 steps 1-4 done (dry); steps 5-8 handed to the human
- [ ] Acceptance criteria all met
- [ ] `_tasks/todo.md` ticked, with a review section appended

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decided:** see DECISIONS at the top.

**Assumptions (flag if wrong):**
1. **The week is Monday-Sunday, Dallas time** (`CADENCE_TZ`). The Friday report covers the week so far,
   and `--week` picks any date inside another week.
2. **Every note in the portal counts**, not only notes on engine-promoted contacts. The four numbers
   describe the founders' new-business week, and the 22 adopted prospects and hand-sourced ones count
   too.
3. **"Owner conversation" means whatever the founder labels it.** The prefix is the founder's judgment
   that they spoke to the decision-maker (M1's "contact role = owner/principal"). No contact-role read.
4. **The weekly run's geography is `dfw`** (the CLI default). The manifest sets the real area, and
   `geography` is only recorded.
5. **One running weekly run globally**, not per vertical: one user, one metro.
6. **A dry run is a full run with no HubSpot writes.** It counts against the guard and appears in the
   report, marked `DRY RUN`.
7. **The report reads HubSpot live each time.** Nothing is cached or stored (split store: outcomes live in
   HubSpot).

## NOTES (open canvas)

**Why a partial unique index, not an advisory lock.** The cadence lock (`app/cadence/repository.py:54`)
is held on a dedicated connection for the life of an in-request block. Here the work outlives the
request (202 + background), so the lock would have to be taken in the request and handed to a background
task on a connection opened outside a context manager: a leak waiting to happen. A row in `running` state
is already the fact "a run is in progress", so the index makes that fact unique, and the reaper handles
the crash case. Sourcing's run lifecycle uses the same reaping idea. Cost: a crash blocks new weekly runs
for up to `WEEKLY_RUN_STALE_HOURS` (2h). That is tolerable for a weekly job, and the 409 message names the
blocking run.

**Why a new slice and table, not columns on `sourcing_run`.** M7 ends when promotion finishes, which is a
fact `sourcing_run` cannot hold without the promotion slice writing to sourcing's table (forbidden:
"never write another slice's tables"). `weekly_run` is the orchestration's own state. It references
sourcing's run, and promotion's counts are snapshotted onto it. Reading sourcing and promotion goes
through their services.

**Why the report and the run share a slice.** The report's run section is the weekly runs, and splitting
`reporting/` out now would give it one consumer and a cross-slice dependency on `runs`. The ticket named
`app/reporting/*`; one slice is simpler, and the README says why.

**Why prefixes and not native call or meeting logging.** Decided by the human (DECISION 1). Native logging
would change every touch the founders log, and discipline is the measured constraint (Spike 1).
Unclassified touches are shown so the convention's adoption is itself visible on Friday.

**Rejected:** an `asyncio.create_task` fire-and-forget instead of `BackgroundTasks`. It needs a
strong-reference set, and tests cannot await it. `BackgroundTasks` is awaited by `ASGITransport`, so
route tests are deterministic.

**Size estimate.** ~1,500-1,800 lines, ~55% tests. Production ~750:
- service ~200
- report ~130
- touches ~70
- repository ~120
- models + migration ~120
- schemas ~140
- routes ~60
- CLI ~160
- gateway, pipeline and promotion deltas ~80

**Risks.**
1. The `configured_stages` move and the `tests/sourcing/test_cli.py` patch target (Task 3 GOTCHA).
2. The fixture plumbing that combines `session_factory` with `FakeCrm` (Task 13).
3. HubSpot notes search filtering on `hs_timestamp` with epoch-ms strings. Settle it with one read-only
   call in Level 4 step 2.
4. Partial-index drift in `alembic check`.

**Confidence: 8/10** for one pass.

## AMENDMENTS

<!-- Append-only. Newest at the bottom. -->

**2026-10-17, during implementation:**
- `on_started` runs straight after `start_run` commits, and the manifest read moved inside the `try`. As
  first placed (after the read), a failing stage's rollback could undo the weekly run's link to its
  sourcing run. Caught by `test_a_failed_sourcing_run_promotes_nothing_and_is_recorded`.
- Search paging reuses the existing `Paging`/`PagingNext`; no `SearchPaging*` models.
- `WeeklyRunRepository.create` flushes and lets the `IntegrityError` rise; `start` rolls back and maps
  `uq_weekly_run_one_running` to the 409, as `CadenceService` does with `UNIQUE_CONTACT`.
- `brief_to_hubspot_minutes` is a field an after-validator fills: `computed_field` needs a type suppression.
- The DST case is the week of 2026-10-26: US DST ends Sunday 2026-11-01, inside that week.
