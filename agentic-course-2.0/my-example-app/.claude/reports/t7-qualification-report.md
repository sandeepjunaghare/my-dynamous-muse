# Implementation Report — T7: qualification slice

**Plan**: `.claude/plans/t7-qualification.md`   **Branch**: `feat/t7-qualification`   **Status**: PARTIAL by
design. Phases 1–5 (tasks 1–26) are complete. Phase 6 (tasks 27–28, wiring into T5) waits until T5 and T8
merge, as the plan specifies.

## Summary
T7 adds `app/qualification/`, with five parts:
- **A manifest-driven predicate evaluator.** It covers all nine operators, converts numbers sent as text,
  and lets each rule name its source. A record it cannot judge is never disqualified.
- **`classify_rollup`, pipeline stage 4.** It is an Agent SDK judgment node with WebSearch and WebFetch,
  one call per candidate. It keeps a verdict only when the verdict cites a page the node verifiably read,
  or the candidate's own registry record.
- **A priority score** (Intensity × Automatable) built only from cited answers.
- **A `disqualification` table with re-source suppression**, keyed on `(vertical, registry_id)`.
- **`lpe manifest dry-run`**, the manifest quality check. `activate` now refuses a manifest that has never
  been dry-run.

## Tasks completed
- 1 → `app/shared/page_reads.py`, `app/shared/agent_reads.py` (CREATE: moved from `app/manifests/agent.py` and `proposal.py`, which now import them)
- 2 → `app/manifests/schemas.py` (UPDATE: `DisqualifierRule.source`, `_rule_sources_are_declared`, `ManifestBody.source_for`)
- 3 → `app/manifests/schemas.py` (UPDATE: `DryRunFlag`, `DryRunSample`, `DryRunStep`, `DryRunSourceFile`, `DryRunReport`, `DryRunResponse`)
- 4 → `app/sourcing/schemas.py` (`PriorityScore`, `ScoreBand`, `CandidateFields.priority`), `app/sourcing/stages.py` (owner row) (UPDATE)
- 5 → `app/qualification/{__init__,schemas,exceptions}.py` (CREATE)
- 6–7 → `app/qualification/rules.py`, `tests/qualification/{builders,test_rules}.py` (CREATE)
- 8 → `app/qualification/scoring.py`, `tests/qualification/test_scoring.py` (CREATE)
- 9–10 → `app/qualification/dry_run.py`, `tests/qualification/test_dry_run.py`, `fixtures/census_extract.csv` (CREATE)
- 11 → `app/qualification/models.py` (CREATE), `app/manifests/models.py` (`ManifestDryRun`), `alembic/env.py` (UPDATE)
- 12 → `alembic/versions/0008_qualification.py` (CREATE; `down_revision = "0005_cadence"`, to be re-chained at merge)
- 13–14 → `app/qualification/{repository,service}.py`, `tests/qualification/test_service.py` (CREATE)
- 15 → `app/manifests/{repository,service,exceptions}.py` (UPDATE: `record_dry_run`, `latest_dry_run`, the `activate` gate, `DryRunNotRecordedError`, `DryRunMismatchError`). Existing activation tests now record a dry-run first, and the CLI fixtures clean up dry-run rows.
- 16 → `app/core/config.py`, `.env.example` (UPDATE: four `rollup_classifier_*` / `max_judgment_calls_per_run` settings)
- 17–19 → `app/qualification/{prompts,judgment}.py`, `tests/qualification/test_judgment.py`, four `fixtures/judgment_rollup_*.json` (CREATE)
- 20–21 → `app/tools/classify_rollup.py`, `tests/qualification/test_stage.py` (CREATE)
- 22 → `app/manifests/cli.py` (UPDATE: `dry-run` subcommand, `QUALITY_REVIEW_NOTE` now points to it), `tests/manifests/test_cli_dry_run.py` (CREATE)
- 23 → `pyproject.toml` (`live` marker), `tests/qualification/test_live_eval.py`, `fixtures/rollup_eval_set.json` (CREATE)
- 24 → `tests/test_structure.py` (UPDATE: SDK allow-list gains `app/qualification/judgment.py` and `app/shared/agent_reads.py`)
- 25 → `CLAUDE.md`, `.claude/references/adding-a-vertical.md`, `docs/tickets/local-prospect-engine.md`, `_tasks/todo.md` (UPDATE)

## Tests added
About 145 new tests:
- `test_rules.py`: every operator; the casts; blank, absent and non-numeric fields; malformed rules; source routing.
- `test_scoring.py`: the E9 band boundaries; a missing axis gives no score; the axis is taken from the manifest.
- `test_dry_run.py`: an exact funnel on the 30-row fixture, plus every flag, determinism, refusals, a
  BOM-prefixed header, and basename-only recording.
- `test_judgment.py`: four rollup replays; the citation gate (an unread page, a 403, a redirect, a host
  mismatch, an invented rule, a re-decided predicate, a duplicate answer); priority; failures that still
  record cost; isolation; the prompt.
- `test_service.py`: idempotent records; suppression across two runs and per vertical.
- `test_stage.py`: free checks run before paid ones; a retry pays nothing; one failure doesn't stop the
  batch; the call cap; nothing to judge.
- `test_cli_dry_run.py`: the gate and dry-run end to end through `lpe`.
- Schema tests for rule `source` and `priority`.
- `test_live_eval.py`: two tests, skipped unless `LPE_LIVE_EVAL=1`.

## Validation results
- ruff check: pass · ruff format --check: 131 files formatted
- mypy: no issues in 131 files · pyright: 0 errors
- pytest with the database (port 5435): **837 passed, 2 skipped** (the two live-eval tests)
- `alembic check`: no drift. Upgrade, downgrade -1 and upgrade round-trip cleanly.
- Manual CLI check against the throwaway DB:
  - `activate` on the fire draft is refused with the dry-run remedy.
  - `dry-run` of seeded freight v1 flags `asset_based_carrier: value_not_seen, matches_nothing`, which is
    exactly the failure the hand check found. The manual row was deleted afterwards.
- **Not run:** the paid live eval (it needs the founder's labelled locals), and the freight v3 dry-run on
  the real census extract (the human's step).

## Deviations from the plan
1. **The shared read tracker is split into two modules.** `page_reads.py` holds `PageRead` and
   `normalize_url` and is pure; `agent_reads.py` holds `ReadTracker` and imports SDK types. Why:
   `app/manifests/proposal.py` is documented as having no SDK import, and importing the tracker from one
   module would have broken that.
2. **There is no separate `tests/qualification/test_repository.py`.** Repository behaviour (idempotency,
   suppression queries) is covered through the service in `test_service.py`.
3. **The judgment cap counts calls locally, not through `RunCost.check_cap`.** `RunCost` counts Agent SDK
   *turns* for `anthropic_tokens`, so `check_cap` would cap turns, not calls. The stage raises the same
   `CostLimitExceededError`.
4. **How the retry skip works.** A retry skips candidates that are already disqualified or already have a
   `priority`. A candidate that was judged but neither disqualified nor scored, because none of its
   signal answers were cited, is judged again on retry. That costs one extra call per such candidate and
   has no correctness impact. If it matters, the fix is a "judged" marker.
5. **`value_not_seen` meaning.** It flags a rule when **none** of its literals appear in the data, not when
   any one is missing. Otherwise an 11-county set would flag every county that happens to have no rows.
6. **The `not_evaluable` count.** In a dry-run step it counts only rows still in the pool at that rule's
   position (the funnel's view). Standalone matches are counted separately.
7. **Log event names.** Four were renamed to satisfy the existing naming guard (`action_state`, exactly one
   underscore), e.g. `manifests.repository.dryrun_recorded` and `qualification.stage.judging_skipped`.
   Events built from f-strings (the tracker's prefix) aren't seen by that guard's regex. They resolve to
   conforming names.
8. **No `effort="high"` on the judgment node.** The authoring agent sets it; a per-candidate judgment is
   kept cheap.
9. **The rollup fixtures are synthetic.** They are marked `_note: SYNTHETIC` and make no claims about the
   real companies. Accuracy belongs to the live eval.

## Issues encountered
- **Sandbox.** The session was opened in the t5 worktree, so every command ran in t7 with the sandbox
  disabled. The `uv` and git rules are in memory.
- **Expected fallout.** Every existing `activate` test needed a recorded dry-run; this was handled with
  `tests/manifests/builders.dry_run_recorded`.

## Ready for the next step
- `piv-commit` → `piv-create-pr` → `piv-review-pr`.
- After T5 and T8 merge, run Phase 6: re-chain `0008` and wire `apply_predicates` and suppression into
  T5's backlog and batch selection, plus the end-to-end two-run test.
- Human:
  - Dry-run freight v3 on the real census extract and confirm DFW 45,855 → 4,518 → 4,449.
  - Supply at least 20 labelled local fire companies for the live eval.
