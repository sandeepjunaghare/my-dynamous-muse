# Implementation Report — T5: sourcing pipeline, `search_registry`, FMCSA adapter

**Plan**: `.claude/plans/t5-pipeline-fmcsa.md`   **Branch**: `feat/t5-pipeline-fmcsa`   **Status**: COMPLETE

## Summary

A brief now turns into a batch of cited candidates. `SourcingPipeline` is a deterministic runner:
1. it reaps stale runs and opens a run under the ACTIVE manifest;
2. it runs `registered_stages()` in order;
3. it finishes the run `completed`, or `degraded` when a stage reports a reason;
4. on any failure it rolls back, finishes the run `failed` with its counts and cost on a fresh session, and
   re-raises.

The one stage that exists, `search_registry`, does four things:
- it binds the manifest's sources **by `base_url`**;
- it pulls the FMCSA census with the manifest's census predicates pushed down to SoQL;
- it refreshes the new `sourcing_pool` backlog, then takes a fixed batch best-first (D12);
- it gathers QCMobile and Motus revocations as cited `source_records` for T7 to evaluate, and records the
  candidates.

`lpe sourcing run` drives it by hand until T10.

## Tasks completed

- Deferred finding #2, the URL shape of `source_url` → `app/shared/provenance.py` (UPDATE)
- `SourceRow`, `SourceRecord`, `CandidateFields.source_records`, `to_stored()`, `PoolEntry`, `PoolSelection` → `app/sourcing/schemas.py` (UPDATE)
- Ownership row for `source_records` → `app/sourcing/stages.py` (UPDATE)
- `StageResult.degraded_reason` → `app/tools/registry.py` (UPDATE; additive only)
- `sourcing_batch_size`, `sourcing_stale_run_hours`, `fmcsa_webkey`, `socrata_app_token` → `app/core/config.py`, `.env.example` (UPDATE)
- `NoRegistrySourceError`, `SourceRequestError`, `SourceAuthError` → `app/sourcing/exceptions.py` (UPDATE)
- Adapter roles, `SourceEnv` and binding by `base_url` → `app/sourcing/sources/base.py` (CREATE)
- GET with retries, never logging query params → `app/sourcing/sources/http.py` (CREATE)
- SODA client and the rule→SoQL pushdown → `app/sourcing/sources/socrata.py` (CREATE)
- Census, QCMobile and Motus revocations adapters, plus `FMCSA_ADAPTERS` → `app/sourcing/sources/fmcsa.py` (CREATE)
- `SourcingPool` and the shared `REGISTRY_ID_IS_CITED` → `app/sourcing/models.py` (UPDATE)
- Migration `0006_sourcing_pool` (`down_revision = "0005_cadence"`) → `alembic/versions/0006_sourcing_pool.py` (CREATE)
- `PoolRepository` (refresh, count available, select best-first) → `app/sourcing/pool.py` (CREATE)
- Conditional `mark_finished` on the DB clock, and `reap_stale_runs` → `app/sourcing/repository.py` (UPDATE)
- Conditional `finish_run`, `reap_stale_runs` and `refresh_pool_and_select` → `app/sourcing/service.py` (UPDATE)
- Stage 1 → `app/tools/search_registry.py` (CREATE)
- The runner → `app/sourcing/pipeline.py` (CREATE)
- `lpe sourcing run` → `app/sourcing/cli.py` (CREATE), `app/cli.py` (UPDATE)
- Docs → `app/sourcing/README.md` (incl. **Contract with T7**), `CLAUDE.md`, the T5 note in `docs/tickets/local-prospect-engine.md`, `_tasks/todo.md`

## Tests added

126 new tests: 690 → **816 passed, 0 skipped** with the database.

| File | What it proves |
|---|---|
| `tests/sourcing/sources/test_socrata.py` | Each operator's keep clause, null semantics as SQL, quoting, refusals, **freight v3's exact `$where`**, paging, app token, 429 retry, 400/5xx/shape errors |
| `tests/sourcing/sources/test_fmcsa.py` | Census→`PoolRecord` mapping and citations, partial address absent, `"N/A"` left out, ordering keys, deduplication and sort, QCMobile found / not found / key refused / 5xx / odd shape, **webKey never in a log or error**, revocation grouping, deduplication and chunking |
| `tests/sourcing/sources/test_base.py` | URL normalisation; freight binds census + QCMobile + Motus; legacy revocations unbound; no key → degraded; fire → no registry; fire + stub → bound with Places unbound; two discovery sources refused |
| `tests/sourcing/test_pool.py` | Refresh idempotence, never clears `batched_run_id`, duplicates refused, cited-id CHECK, **best-first order**, unseen → unselectable, **only a failed run returns its batch**, separate pools per vertical, conditional finish, DB-clock `finished_at`, reaper scope, reaped run returns its batch |
| `tests/sourcing/test_pipeline.py` | **Determinism**, **disjoint batches across 3 runs** (twice), failed run recorded with its counts and batch returned, **aborted transaction still recorded as failed**, reaper before start, every field cited, degraded without a key, a key refused mid-batch, clashing count names, no active manifest → no run, empty pull |
| `tests/sourcing/test_portability.py` | **M9**: the same stage class sources fire from a stub registry; the mock FMCSA is never touched |
| `tests/sourcing/test_cli.py` | Run prints id, status and counts; `--batch-size` reaches the stage; degraded exits 0 with a warning; no active manifest → one `error:` line, exit 1; bad batch size → argparse 2 |
| `tests/tools/test_search_registry.py` | Registered by being a file; the real package includes it |
| `tests/shared/test_provenance.py`, `tests/sourcing/test_schemas.py` | URL shape; `SourceRow`/`SourceRecord`; D13 inside tuple fields |

**Mutation check.** I broke each guarantee on purpose and confirmed the targeted test failed: **10 of 11 caught**. The breaks caught were:
- never batched twice;
- a failed run returns its batch;
- rollback before finish;
- portability;
- best-first order;
- conditional finish;
- webKey in a citation;
- stop after an auth failure;
- the reaper.

The survivor is under *Issues encountered*.

## Validation results

- `ruff check .` ✓ · `ruff format --check .` ✓ (126 files)
- `mypy .` ✓ (126 files) · `pyright` 0 errors · zero suppressions
- `pytest` with `TEST_DATABASE_URL` (port 5434): **816 passed, 0 skipped**. Offline: 544 passed, 272 skipped
- `alembic heads` → one head, `0006_sourcing_pool`; upgrade → `alembic check` clean → downgrade to `0005` → upgrade → clean (throwaway DB only)
- **Level 4, live and read-only, run before coding.** The SODA `count(*)` with freight v3's pushed-down `$where` returns **4,464** (vs 4,449 measured by hand on 2026-10-09; the dataset refreshes daily). This confirms that SoQL `contains()` and the `::number` cast work. Motus `wb4f-neki` returns unpadded `usdot_number`.
- `lpe sourcing run --help` ✓. **No run against dev Supabase**: freight v3 is DRAFT, and activating it is the human's call.

## Deviations from the plan

1. **`app/sourcing/sources/http.py` added.** It holds one GET-with-retries for both SODA and QCMobile, instead of a copy in each.
2. **Factories are built with a `SourceEnv`** (client, settings, **clock**, backoff, throttle) instead of `(client, settings)`. The plan wanted `retrieved_at` pinned for the determinism test, and that needs the clock to reach the adapters. Backoff and throttle let tests skip their sleeps.
3. **`CandidateFields.to_stored()` added**, and used by both writers. An empty `source_records` was being stored as `[]`, which broke T4's "absent fields are absent keys" rule. An existing T4 test caught it.
4. **`refresh_pool_and_select` returns `PoolSelection`/`PoolEntry`** (new frozen schemas) rather than ORM rows, so the stage never handles the model.
5. **`SearchRegistryStage.with_batch_size()`.** Rebuilding the stage for `--batch-size` would have thrown away its configuration.
6. **The portability test lives in `tests/sourcing/test_portability.py`**, not `tests/tools/`, because the `session_factory` fixture is in `tests/sourcing/conftest.py`.
7. **Log event names differ from the plan.** They were renamed to pass `tests/core/test_logging.py`'s `domain.component.action_state` guard (the last segment has exactly one underscore). Examples: `sourcing.qcmobile.lookup_failed`, `sourcing.socrata.pushdown_skipped`, `sourcing.pool.refresh_completed`.
8. **The mid-batch auth-failure test** goes beyond the plan's edge-case list. It checks that lookups stop at the refused key.

## Issues encountered

- **One surviving mutation: the fresh session.** Finishing on the *same* session after the rollback also works, so no test shows the fresh session is required. It matters only when the rollback itself fails, for example on a dropped connection, which the shared-connection fixture cannot simulate. It is kept as defence. The `pipeline.py` docstring now says exactly this and does not claim it is tested.
- A test named "zero-padded usdot still joins" was not testing padding (both sides unpadded). It was deleted rather than left as a fake. The live data is unpadded, and the strip is defensive.
- **An untracked `.claude/plans/t8-route-clustering.md` is in this worktree.** It is not from this session. I left it untouched, and it should not be committed with T5.
- **Still open, for the human** (also in the T5 ticket note):
  - freight **v4** needs revocations → `https://data.transportation.gov/d/wb4f-neki` before the first live run;
  - an **FMCSA webKey** is needed, and then one real `/carriers/{dot}` capture to settle `allowToOperate` vs `allowedToOperate`;
  - **T7's plan must confirm or object to the null semantics** in *Contract with T7*.
