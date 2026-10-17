# Implementation Report — T10: the weekly run, its trigger, and the Friday report

**Plan**: `.claude/plans/t10-weekly-run.md`   **Branch**: `feature/t10-weekly-run`   **Status**: COMPLETE
(Level 4 steps 5-8 are the human's, by design)

## Summary
A new slice, `app/runs/`, chains the existing pipeline and promotion into one weekly run. `POST /runs`
preflights and refuses in the request, returns 202, and does the work in a background task. `lpe runs
start` runs the same service in-process for launchd. The `weekly_run` table records M7's four
timestamps on the database clock, with promotion's counts. A partial unique index keeps at most one run
`running`, and a 2h reaper clears killed runs. The Friday report (`lpe runs report`,
`GET /runs/weekly-report`) counts prefixed HubSpot notes over the Dallas week, shows paid sold as
`not configured`, and lists each run's M7, cost, funnel, promotion and M6 review tally.

## Tasks completed
- Task 1: settings → `app/core/config.py`, `.env.example` (UPDATE)
- Task 2: `GTE`/`LT`, `after`, `paging` → `app/promotion/schemas.py` (UPDATE)
- Task 3: `configured_stages`, `preflight`, `DEFAULT_GEOGRAPHY`, `run(on_started=)` →
  `app/sourcing/pipeline.py`; CLI pointed at them → `app/sourcing/cli.py` (UPDATE)
- Task 4: `WeeklyRun` → `app/runs/models.py`, `alembic/versions/0011_weekly_run.py` (CREATE);
  `alembic/env.py` (UPDATE)
- Task 5: `app/runs/schemas.py`, `app/runs/exceptions.py` (CREATE)
- Task 6: `app/runs/repository.py` (CREATE)
- Task 7: `ReviewTally` → `app/promotion/schemas.py`; `status_counts_by_run` →
  `app/promotion/repository.py`; `review_tally` → `app/promotion/service.py` (UPDATE)
- Task 8: `app/runs/service.py` (CREATE)
- Task 9: `app/runs/touches.py` (CREATE)
- Task 10: `app/runs/report.py` (CREATE)
- Task 11: `app/runs/routes.py` (CREATE); `app/main.py` (UPDATE)
- Task 12: `app/runs/cli.py` (CREATE); `app/cli.py` (UPDATE)
- Task 13: `tests/runs/*` (CREATE); additions to `tests/sourcing/test_pipeline.py`,
  `tests/promotion/test_client.py`, `tests/core/test_config.py`; `session_factory` moved to
  `tests/conftest.py`
- Task 14 (docs):
  - `app/runs/README.md` (CREATE)
  - `CLAUDE.md`: architecture map, commands, test-DB port 5434
  - `.claude/references/hubspot-integration.md`: five `RecordMeta` properties, a *Friday report*
    section
  - The ticket's T10 section: the decisions, routes-after-qualification, the file list
  - The sourcing, promotion and routing READMEs
  - Stale "T10 will…" comments in `verify_business.py` and `core/dependencies.py`

## Tests added
103 new tests. All pass.
- `tests/runs/test_touches.py` (25): real portal notes, variants, prefix-not-search, empty input; week
  windows (Wednesday, Monday, Sunday, the 169-hour DST week, UTC-aware); Monday-03:00-UTC is last week;
  epoch ms.
- `tests/runs/test_models.py` (15): status matches the CHECK; request refusals; `PlaybookNumber` cannot
  fake a 0; M7 derived, never input; every funnel/promotion counter is one a stage or `PROMOTE_COUNTS`
  reports.
- `tests/runs/test_service.py` (23, DB tier):
  - Outcomes: happy path, dry run (no writes), empty batch, `get`.
  - Degraded: degraded sourcing still promotes; promotion stopped; promotion failures, with every
    reason kept.
  - Failed: failed sourcing promotes nothing (spy) and is linked; promotion raises; manifest
    deactivated after start; cancellation recorded then re-raised; a finished run is not re-executed.
  - The guard (409, and global rather than per vertical), a restart after finish, and the stale reap.
  - Refusals that write no row: no ACTIVE manifest, and the preflight.
  - Repository: the compare-and-set finish, and the half-open window.
- `tests/runs/test_report.py` (12, DB tier):
  - Notes: counted across two pages with `after` sent; the epoch-ms filter body for the Dallas week;
    the default week; paid sold is `null` in the JSON; a zero week; the page cap raises; no note text
    in the logs.
  - Runs: window exclusion, M7 against the target, funnel `None`, promotion counters, a run that never
    sourced.
  - Review tally: per-status counts, a run with no rows gets zeros, and the tally is carried into the
    report.
- `tests/runs/test_routes.py` (8): 202 then completed; 409; 404 manifest; 404 run; 422 body; 422 id;
  the report route with `week`; `/weekly-report` is not taken for an id.
- `tests/runs/test_cli.py` (11): start lines, the options reach the request, `DRY RUN`, degraded 0 with a
  warning, failed 1, refusal 1; `show`, including an unknown id; the report's numbers, paid sold `not
  configured` and the unclassified hint; per-run target and review lines; bad `--week` exits 2.
- `tests/sourcing/test_pipeline.py` (+3): `on_started` before the first stage; a raising callback fails
  the run; no callback changes nothing.
- `tests/promotion/test_client.py` (+4), `tests/core/test_config.py` (+4).

## Validation results
- `ruff check .` / `ruff format --check .`: pass
- `mypy .`: no issues in 233 files · `pyright`: 0 errors
- `pytest` (DB tier, `lpe-test-pg` on 5434): **1855 passed, 4 skipped** (baseline 1752 / 4)
- `alembic upgrade head && alembic check` on the test DB: one head `0011_weekly_run`, no drift
- Suppressions: none (the only match is the structure guard's own string)
- **Level 4, against dev:**
  1. `0011` applied to dev Supabase.
  2. `lpe runs report --week 2026-09-28` matched the plan's expectation exactly: 2 door knocks, 0, 0,
     paid `not configured`, 7 unclassified of 9 notes. HubSpot accepted the epoch-ms `hs_timestamp`
     range filter (risk 3 settled).
  3. `lpe runs start --vertical freight --batch-size 5 --dry-run`: completed in 29 s for $0.03, with
     1 would-create (unverified). HubSpot calls were company searches only.
  4. Via uvicorn:
     - `POST /runs` returned 202 `running`, and a second POST got a 409 naming the run.
     - `GET /runs/{id}` reached `completed` at 0.43 min.
     - `GET /runs/weekly-report` returned 200 and listed both dry runs.
     - The server's HubSpot requests were searches only.

## Deviations from the plan
- **`on_started` runs straight after `start_run` commits; the manifest read moved inside the `try`.**
  The plan put the callback inside the `try`, which then began after the manifest read. As first built,
  the run's session had already opened its next transaction when the link committed. A failing stage's
  rollback then undid the link: the test infrastructure's shared connection showed it, and it is the
  wrong order regardless. Side benefit: a failed manifest read now finishes the sourcing run `failed`
  instead of leaving it `running` for the reaper.
- **Reused `Paging`/`PagingNext`** (already defined for associations, moved above `SearchResponse`)
  instead of new `SearchPaging*` models. Added `SearchResponse.next_after`, mirroring `AssociationsPage`.
- **The one-running `IntegrityError` is caught in `WeeklyRunService.start`, not the repository.**
  Catching it means rolling back, and the transaction is the service's (house rule; `CadenceService`
  does the same with `UNIQUE_CONTACT`). The repository's `create` just flushes.
- **`brief_to_hubspot_minutes` is a field an after-validator fills**, not `computed_field`: the latter's
  property decorator needs a `# type: ignore`, which the project forbids. It is derived on every
  validation and never taken from input (tested).
- **`session_factory` moved to `tests/conftest.py`.** Re-importing a fixture into `tests/runs/conftest.py`
  trips F401, and no suite does it. The plan allowed lifting it. `tests/sourcing/conftest.py` keeps the
  `SessionFactory` alias.
- **The DST test uses the week of 2026-10-26**, not 2026-11-02: US DST ends Sunday 2026-11-01, so that is
  the week that is 169 hours long.
- **`app/core/dependencies.py`'s `get_run_cost` docstring** promised T10 would change it. The pipeline
  already scopes cost per run, so the docstring now says that; no code change.
- **`requires_db`'s hint and CLAUDE.md now say port 5434** (and `docker start lpe-test-pg ||`), per the
  todo log's finding that 5433 is Langfuse's.

## Issues encountered
- **Funnel gap (not changed; for the founder's call).** `FUNNEL_COUNTERS` (the plan's AC 7 list) omits
  `places_not_found` and `routing_unverified_address`. In the dev dry run, the one survivor appeared in
  neither, so the Friday funnel shows batched 5 and disqualified 4, then zeros, with only
  `promoted_unverified 1` hinting where it went. Adding the two names is a two-line change, covered by
  the existing counter-name test.
- `.claude/skills/piv-validate/SKILL.md` still recommends port 5433. It is outside this plan, and
  `.claude/skills` is write-protected in this session.
- M7 shows `0 min` for a sub-minute run (`:.0f`). This is honest at the 60-minute target's scale; the
  JSON carries the fraction.
- **Dev side effects of Level 4:** two dry weekly runs used 10 candidates from the freight backlog
  (expected; a dry run sources for real). Two `weekly_run` rows and two `sourcing_run` rows were
  written to dev. No HubSpot write.

### Ready for the next step
Every task is done and validation is green. Next: `piv-commit`, `piv-create-pr` (this report is the
body), then `piv-review-pr`. Human steps, Level 4 steps 5–8:
- Read M5 on the adopted 22 before the first real run.
- Make the first real run: `lpe runs start --vertical freight`.
- Add the launchd jobs: weekly `lpe runs start`, Friday `lpe runs report`.
- Tell the founders the note-prefix convention.
