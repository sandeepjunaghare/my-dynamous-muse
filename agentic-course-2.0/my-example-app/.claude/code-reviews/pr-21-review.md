# PR #21 review: T5, the pipeline runner, `search_registry` and the FMCSA adapters

**Verdict: APPROVE.** There are no Critical or High issues, and validation is green. One Medium needs a human decision before the first live run (M1). The rest are Lows that can be fixed in this PR or deferred.

**Method:** The diff was reviewed in a clean context by the `code-reviewer` agent, which read every changed file in full against `CLAUDE.md`, the plan and the implementation report. Documented deviations were treated as intentional. Validation was re-run on the PR branch. The two findings that one quick test could settle were then checked against the live public datasets: one is closed and one is downgraded.

## Validation (re-run on `d4ed48f`)

| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | ✓ / ✓ (126 files) |
| `mypy .` / `pyright` | ✓ / 0 errors; zero suppressions |
| `pytest`, with a throwaway Postgres | **816 passed, 0 skipped** |
| `alembic heads` / `alembic check` | one head, `0006_sourcing_pool` / no drift |

## Issues

### Medium

**M1. A degraded run keeps its batch, and nothing ever re-looks those candidates up.**
- **Where:** `app/sourcing/pool.py` (`_available`), `app/tools/search_registry.py`, `app/sourcing/pipeline.py`.
- **What happens:** Only a `failed` run hands its batch back. A missing webKey, a refused key or any failed lookup makes a run `degraded`, and that run keeps its batch for good.
- **Why it matters:** There is **no webKey today**. Each live `lpe sourcing run` would take 150 candidates without `allowToOperate` and exit 0 with a warning. The next run would take the next 150, and the first 150 would never be looked up.
- **Documented or not:** The plan states "nothing is batched twice by a run that finished". It never spells out this consequence for degraded runs, and `test_pool.py` pins the behaviour without comment.
- **Fix (human decision needed):**
  - **Option (a), recommended:** bind sources before the pool refresh. Fail fast (`failed`, batch untouched) when a recognised lookup source is unavailable, unless the person passes something like `--allow-degraded`.
  - **Option (b):** treat "evidence missing" degradation as returning the batch.
  - **Option (c):** accept the behaviour and document it.
- **Not blocking:** no live run can happen until a freight v4 is activated.

### Low

- **L1. A run can be left `running` between `start_run` and the `try`.** `pipeline.py`: `ManifestService.get()` runs after `start_run` has committed but outside the `try`. If it raises, the run stays `running` until the 6-hour reaper. No batch has been taken at that point. **Fix:** move the call inside the `try`, or resolve the manifest before `start_run`.
- **L2. A source the stage could not bind is silent in the run status.** Under freight v3 the legacy revocations source is unbound, and the run still ends `completed`. `join_matched: 0` looks the same as "no revocations found". T7 reads the absent record as unevaluable, so nothing passes falsely, but the CLI shows nothing wrong. **Fix:** add a count such as `pool_sources_unbound`, or print the unbound names. *(The reviewer also asked whether v3's QCMobile URL matches the adapter. Checked: v3 declares `https://mobile.fmcsa.dot.gov/qc/services/`, which matches.)*
- **L3. The webKey is a plain `str`, and the log test covers one channel.** `config.py`: a repr or `model_dump` of `Settings` or `SourceEnv` would show the key. httpx's stdlib logger prints full URLs, query included, at INFO. Nothing enables it today, but the test uses `structlog.testing.capture_logs`, which cannot see stdlib records. **Fix:** make the key a `SecretStr`, and set `logging.getLogger("httpx").setLevel(logging.WARNING)` in `setup_logging`.
- **L4. `refresh_pool_and_select` reads ORM attributes after `commit()`.** `service.py`: this is safe only because `get_sessionmaker()` sets `expire_on_commit=False`. **Fix:** build `PoolSelection` before the commit.
- **L5. Some tests are weak.**
  - `test_pool.py::test_the_cutoff_is_two_years_by_days_not_by_calendar` tests the standard library, not `CURRENCY_WINDOW`.
  - `test_pipeline.py::test_the_stale_window_is_the_setting` asserts only a default value.
  - `MockFmcsa` ignores the census `$where`, so the pipeline tests cannot detect the pushdown being dropped. Only `test_fmcsa.py` checks it, with one substring.
  - The CLI's failure output omits the failed run id.

### Closed during review
- **(was Medium) The revocations query had never run live.** The reviewer could not run commands. Checked live and read-only: `wb4f-neki.json` with `$where=usdot_number IN ('1006607','104701')` and `$order=usdot_number,:id` returns **HTTP 200** with the expected rows. The metadata types `usdot_number` as `text`, so the string literals are correct.

## What's done well
- **Availability comes from the batching run's status**, so the reaper's one `UPDATE` is the whole crash recovery, even after a hard kill.
- **The pool refresh and the batch marking commit together**, and only `SourcingService` ever commits.
- **Sources bind by `base_url`** through an injectable factory table. The M9 portability test proves this and asserts the mock FMCSA is never touched.
- **The SoQL pushdown is sound.**
  - Column names pass through an allow-list before interpolation, and literals are quote-doubled.
  - Every keep clause is the correct negation, and the null contract holds for all eight operators.
  - Paging cannot short-circuit, and freight v3's exact `$where` is pinned to the live count of 4,464.
- **The webKey stays out of citations, structlog lines and exception text.**
- **The merge with `to_stored()` cannot erase evidence.**
- **The `0006` migration matches the model exactly.**
- **The author's report is honest:** mutation testing, and an explicit note of the one guarantee no test proves.

## Recommendation
**Approve.** Before the first live run, decide M1. Option (a), refusing to batch when a lookup source is unavailable, fits "free before paid" and needs about 20 lines. L1–L4 are small enough to fix in this PR if wanted. L5 can be deferred.
