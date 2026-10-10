# Review of PR #17: the pipeline stage contract and registry, ahead of Wave 5

**Branch:** `feat/tools-stage-seam` → `main` · 7 files, +276 / −9
**Recommendation:** **Approve.** There are no Critical or High issues, and the validation suite is green. The four Low findings below can be fixed in this PR or deferred.

## Summary

The PR adds `app/tools/registry.py`, which defines the `Stage` Protocol, `StageContext`, `StageResult` and `registered_stages()`. It also adds 7 tests and records the Wave 5 re-plan in the tickets, `CLAUDE.md` and `_tasks/todo.md`. A stage registers by being `app/tools/<PipelineStage value>.py` and exporting `STAGE`. That lets T5, T7 and T8 add files in parallel without a merge conflict.

The PR body documents three deliberate departures from the docs, and this review does not treat them as issues:
- registration is by file name rather than by appending to a list;
- the contract lives in `registry.py` rather than `__init__.py`, because of the structure guard;
- `StageContext` carries no candidate list.

No implementation report exists for this PR (it is a seam PR, not a `piv-implement` run). The "changed from the plan" note in `_tasks/todo.md` serves the same purpose.

## Issues

### Critical: none
### High: none
### Medium: none

### Low

**L1. A `stage` that is not the enum raises `AttributeError` instead of the intended `TypeError`.** `app/tools/registry.py:84-88`
- How it fails:
  - A stage module sets `stage = "cluster_routes"`, a plain `str`.
  - `isinstance(exported, Stage)` passes, because a runtime-checkable Protocol only checks that the attribute exists.
  - `exported.stage is not pipeline_stage` is True, so the stage is still rejected. That part is correct.
  - The error message then evaluates `exported.stage.value`, and a `str` has no `.value`. The author gets `AttributeError: 'str' object has no attribute 'value'` instead of the "named for the stage it exports" message.
- Static typing catches this inside `app/`, but the registry loads `STAGE` as `object` at runtime, so the runtime check is the guard that matters.
- **Fix:** before the identity check, add `if not isinstance(exported.stage, PipelineStage): raise TypeError(f"{module_name}.STAGE.stage must be a PipelineStage, got {exported.stage!r}")`. Add a test for it next to `test_a_module_whose_stage_claims_another_name_is_refused`.

**L2. The test against the real package is trivially true today.** `tests/tools/test_registry.py:65-68`
- `app/tools/` has no stage modules yet, so the list is empty and the ordering assertion holds automatically.
- The test still has some value: it imports `app/tools/`, so a broken stage module would fail it.
- It becomes meaningful once T5 lands.
- **Optional fix:** also assert no duplicates, `len(stages) == len(set(stages))`.

**L3. T5's `Files:` line still lists `app/tools/__init__.py`.** `docs/tickets/local-prospect-engine.md:149`
- That file landed in this PR, and the strikethrough at line 136 of the same ticket already says so.
- **Fix:** change the line to `app/tools/search_registry.py`.

**L4. The step 1 checkbox in `_tasks/todo.md` still describes the abandoned design.**
- The checkbox describes an empty `STAGES` list in `__init__.py`, "batch candidate ids" in `StageContext`, and a "one-line append".
- The "Step 1 changed from the plan" note further down corrects it, and the file is a cumulative log, so this is acceptable.
- A reader who sees only the checkbox would be misled.
- **Fix:** tag the checkbox "(superseded, see *Step 1 changed from the plan* below)".

## Validation

| Check | Result |
|---|---|
| `ruff check .` | ✅ All checks passed |
| `ruff format --check .` | ✅ 107 files already formatted |
| `mypy .` (strict) | ✅ No issues in 106 source files |
| `pyright` (strict) | ✅ 0 errors, 0 warnings |
| `pytest` (database tier, Postgres on 5434) | ✅ **689 passed**, 2 warnings |
| `alembic check` | Not run: this PR has no migration |

Both warnings come from the existing use of Starlette's deprecated `HTTP_422_UNPROCESSABLE_ENTITY` in `app/main.py`. This PR does not touch that code.

## Checked and fine

- **Telling a missing stage from a broken one.** `exc.name != module_name`:
  - skips only when the stage module itself is absent;
  - re-raises when the package is missing, because `exc.name` is then the parent;
  - re-raises when a stage module has a broken import inside it, which is tested.
- **No import cycle.**
  - `app/sourcing/stages.py`, `app/manifests/schemas.py` and `app/core/cost.py` import nothing that leads back to `app.tools`.
  - Stage modules are imported when `registered_stages()` is called, not when the registry is imported.
  - So T5's runner in `app/sourcing/service.py` can import the registry safely.
- **Test isolation.**
  - The `sys.path` change is undone by the monkeypatch.
  - Each test package has a unique `uuid4` name, and its modules are removed from `sys.modules` after the test.
  - `importlib.invalidate_caches()` is called after the files are written.
- **`StageContext` covers what the tickets ask for.** No ticket for T5, T7 or T8 requires a settings object, a HubSpot client or a clock in the context.

## What's done well

- Registering by file removes the three-way merge conflict instead of managing it. Taking the order from `PipelineStage` keeps the run the same whatever the file system lists.
- A missing stage is skipped and a broken one raises. A stage can never drop out of a run silently.
- The code meets the strict-typing rule: no `Any` and no suppressions. `STAGE` is read as `object` and narrowed with `isinstance`.
- The `package` parameter plus throwaway packages means the tests describe the contract and will stay true as T5 to T8 add real stages.
- The PR body flags each departure from the docs, and the docs were updated in the same PR.

---
*Review by the `piv-review-pr` agentic gate. A human makes the final call and merges.*
