# `app/runs/` — the weekly run, and the Friday report

T10. Before it, a founder ran the pipeline and promotion by hand (`lpe sourcing run`, then
`lpe promotion promote --run <id>`, copying the id between them), and nothing measured how long
the run took. This slice turns that into **one weekly run, triggered externally**. It goes from a
brief to a pending-review list in HubSpot unattended, records how long that took (M7) and what it
cost, and on Friday reports what the week amounted to.

## Key flows

```
POST /runs | lpe runs start
  └─ start():   reap stale weekly runs → ACTIVE manifest → configure stages → preflight
                → INSERT weekly_run (the partial unique index: one running at a time) → commit
  └─ execute(): SourcingPipeline.run(brief, on_started = link the sourcing run)   ── raises → FAILED
                → sourced_at
                → PromotionService.promote_run(sourcing run, dry_run = the row's)  ── raises → FAILED
                → COMPLETED | DEGRADED
```

- **`POST /runs` returns 202 and works in the background** (DECISION 2, 2026-10-17). Every refusal
  happens in the request: 404 when the vertical has no ACTIVE manifest; 409 when a declared source
  cannot run, unless `allow_degraded`; 409 when a weekly run is in progress (the message names it);
  422 for a bad body. The work then runs as a FastAPI background task in the same process.
  `GET /runs/{id}` shows progress and the result.
- **`lpe runs start` runs the same service in-process.** This is what launchd calls weekly.
- **Real promotion by default; `dry_run` on both the route and the CLI** (DECISION 3). A dry run
  sources for real and promotes as a dry run: the same reads, no HubSpot write. It still counts
  against the guard, and it still records M7.
- **Outcome.**
  - `degraded`: sourcing degraded, the promote pass stopped early (auth, daily quota), or promotion
    had per-candidate failures. The detail names every reason.
  - `failed`: sourcing failed (nothing is promoted), promotion raised, or the run was reaped. A
    background failure is written to the row and swallowed, because a background task has nobody
    to raise to. Cancellation is written down, then re-raised.
- **The daily syncs are not called here.** `lpe promotion sync` and `lpe cadence sync` stay in the
  daily launchd job.

## The `weekly_run` table

One row per weekly run. It holds the options (`dry_run`, `batch_size`, `allow_degraded`) and the
`sourcing_run_id` that `on_started` writes before the first stage runs. It snapshots promotion's
counts, and records the four timestamps M7 is measured from: `requested_at` → `sourced_at` →
`promoted_at` → `finished_at`. Every timestamp is the **database's** `now()`, as on
`sourcing_run`. `brief_to_hubspot_minutes` is `promoted_at - requested_at`, computed by the
response model rather than stored.

Why a table of its own rather than columns on `sourcing_run`: M7 ends when promotion finishes,
and recording that there would mean promotion writing to sourcing's table. This slice reads
sourcing and promotion **through their services** and never writes their tables.

## The guard and the reapers

- **One running weekly run, globally** (one user, one metro). The guard is the partial unique index
  `uq_weekly_run_one_running`, not an advisory lock. The work outlives the request, so a lock taken
  in the request would have to be handed to a background task on a connection nobody's context
  manager owns. A `running` row already *is* "a run is in progress"; the index makes it unique.
- **A killed run** (the Mac asleep, a restart mid-task) stays `running`. The next `start` fails it
  after `WEEKLY_RUN_STALE_HOURS` (2h, against a 60-minute M7 target), which lifts the guard. Its
  sourcing run is reaped by sourcing, per vertical, at the next pipeline start, after
  `SOURCING_STALE_RUN_HOURS` (6h). A fresh run between 2h and 6h is fine: `start_run` has no
  running-run guard, and the old run's batch is not handed back until it is reaped.
- **Not guarded:** a hand-run `lpe sourcing run` beside a weekly run. That is the human's choice.

## The Friday report

`lpe runs report [--week YYYY-MM-DD]` and `GET /runs/weekly-report?week=…`. The week runs Monday
00:00 to the next Monday 00:00, **Dallas time** (`CADENCE_TZ`), built from local midnights, so a
DST week is 167 or 169 hours. It has three parts:

1. **The playbook's four numbers.** These come from HubSpot **notes** in the week, classified by
   prefix (DECISION 1). A read of the portal on 2026-10-17 found every real touch logged as a
   free-text note; the portal's calls and meetings are HubSpot sample data. So a note whose text
   **starts with** `Door knock`, `Owner conversation` or `Assessment booked` counts toward that
   number. Leading emoji and punctuation are ignored, case is ignored, and `-`/`_` read as spaces
   (`touches.py`). Every other note in the week is an **unclassified touch**, and the report shows
   them, so whether the convention is being used is visible. **Paid assessments sold is
   `not configured`** (GATE-A, D10). `PlaybookNumber` cannot hold a 0 for it.
2. **Runs.** Each weekly run requested in the week, with:
   - M7 against `WEEKLY_RUN_TARGET_MINUTES`, and what sourcing spent.
   - The funnel and verification counters (`FUNNEL_COUNTERS`); `None` means a stage never reported
     the key.
   - Promotion's counters (`PROMOTION_COUNTERS`, including `promoted_unverified` and
     `refused_contradicted`).
3. **Review (M6).** Accepted, rejected, pending and withdrawn per run, from the `promotion` ledger
   (`PromotionService.review_tally`, read-only), so as of the last daily sync. A row counts under
   the run that claimed it.

The report reads HubSpot live each time and stores nothing. HubSpot's search index lags writes,
so a note logged moments before the report may be missed. It refuses, rather than truncates, past
4,000 notes in a week. No LLM classifies notes: a wrong count is fixed by the founder's label.

## Integration points

- `app/sourcing/pipeline.py`: `configured_stages`, `preflight`, `DEFAULT_GEOGRAPHY` and
  `SourcingPipeline.run(on_started=…)` are shared with `lpe sourcing run`.
- `app/promotion/service.py`: `promote_run` (imported from the module, never the package) and
  `review_tally`.
- `app/promotion/client.py`: notes search with `GTE`/`LT` on `hs_timestamp` (epoch ms) and `after`
  paging.
- The route's service takes the **session factory**, never the request session: FastAPI closes a
  request's yield-dependencies before background tasks run. Only the CLI closes the engine and the
  HubSpot client; the route path shares the process's.

## launchd (a human step)

A weekly job (suggested Monday 06:00) runs `uv run lpe runs start --vertical freight`. A Friday job
(suggested 07:30) runs `uv run lpe runs report`. Both write to a log file. Once on the VPS, the
weekly job hits `POST /runs` instead.
