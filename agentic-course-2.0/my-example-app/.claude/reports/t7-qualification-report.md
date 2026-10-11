# Implementation Report — T7: qualification slice

**Plan**: `.claude/plans/t7-qualification.md`   **Branch**: `feat/t7-qualification`   **Status**: COMPLETE.
Phases 1–5 (tasks 1–26) were built first. Phase 6 (tasks 27–28, wiring into T5) followed once T5 and T8 merged;
see *Phase 6* at the end.

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

## Phase 6 — merge and wiring (2026-10-10)

**Task 27: merge.** `main` (T5, T8, #26–#28) was merged in as `a1448b3`, and `0008` now revises `0007_routing`.
The six conflicts all kept both sides:
- `CandidateFields` holds both `source_records` and `priority`. The D13 walk over tuple fields comes from T5,
  calling T7's public `is_google_maps`.
- `FIELD_OWNERS`, the alembic model registry and the schema tests keep both entries.
- `CLAUDE.md` gets a combined architecture map, and `todo.md` keeps both logs.

A stale local merge of an unpushed `main` was set aside as `backup/t7-local-merge-110c796`.

**Task 28: wiring, re-scoped to T5 as built.** T5 pushes census rules into SoQL, so there is no in-code pool
filter to replace. Two decisions came from the owner:
1. On a found record, an absent or blank field fires `not_equals`, `not_in_set` and `not_contains`, as the
   pushdown already does. A missing record stays `not_evaluable`.
2. The free predicates run at the end of `search_registry`.

What was built:
- `rules.py`: `NEGATED_OPERATORS`, the absent-field rule, and the module docstring.
- `qualification/records.py` (new): `records_from`, which turns `source_records` into a `RecordSet` plus record
  URLs. One row becomes text; no rows and many rows are left out.
- `tools/search_registry.py`:
  - suppression is passed into selection;
  - `disqualify_by_predicates` runs per recorded candidate;
  - new counts `pool_suppressed` and `qualification_disqualified`;
  - `_refuse_misrouted_rules`.
- `sourcing/pool.py` and `sourcing/service.py`: `exclude` on availability, the count, and selection.
- `sources/base.py` and `fmcsa.py`: `DiscoveryPull.known_fields`, which the census sets to `CENSUS_COLUMNS`.
- `qualification/service.py`: the evidence for an absent field reads "`<field>` absent".
- The old spelling `allowToOperate` was renamed in T7's code, tests and the adding-a-vertical guide.

**Deviations from task 28 as written**
- **The guard.** The plan asked for a grep guard ("one evaluator"). T5's SoQL translation is a second operator
  implementation by design, so the grep could never pass. It was replaced by `tests/qualification/test_parity.py`,
  which checks per operator that the evaluator fires on an absent field exactly when the SQL drops the row.
- **No `qualification_not_evaluable` count.** It would need the rules evaluated twice. The service already logs
  `qualification.service.evaluation_incomplete` per candidate.
- **Added: the misrouted-rule refusal (not in the plan).** A rule without `source` reads the census. Under the
  new semantics, freight v6's `broker_authority_not_active` (`brokerAuthorityStatus not_equals 'A'`, no
  `source`) would fire on every candidate and suppress the whole backlog for the vertical. The refusal costs
  one pull. The T5 test builder's QCMobile rule now names its source.

**Tests:** 1,081 passed, 2 skipped (the paid live evals).
- New: `test_qualification_wiring.py` (5, including the two-run test), `test_records.py` (4), `test_parity.py`
  (8), and the rewritten absent-field cases in `test_rules.py`.
- **Mutation check:** 6 of 6 caught. The six breaks were:
  - suppression dropped;
  - the guard not called;
  - an absent field never firing (twice: the wiring test and the parity test);
  - an empty record kept;
  - the predicates not run.

**Still open**
- **Freight v7.** Set `source: fmcsa_qcmobile` on `not_allowed_to_operate` and `broker_authority_not_active`,
  dry-run it, and activate it before the next live run; otherwise the run refuses. Dev also needs
  `alembic upgrade head` for `0008`.
- ~~**Residual risk.** A typo in the field of a negated rule on a non-discovery source (QCMobile) would fire on
  every candidate.~~ Closed by the review fixes below.

## PR #18 Phase 6 review fixes (M1, M2, L1)

**Status:** COMPLETE. Owner approved option (a) for M1, with M2 and L1 in the same change.

- **M1, the circuit breaker.** `rules.check_batch` checks each predicate rule. It trips when the rule fires on at
  least `TRIP_SHARE` (90%) of the batch candidates that have a record for its source, with at least `MIN_SAMPLE` (5)
  of them. It raises `BatchWideDisqualificationError` (422), with the rule, the counts and up to three observed
  values. `QualificationService.disqualify_batch` evaluates the whole batch first, checks it, and only then
  records. `search_registry` calls it in place of the per-candidate loop. The run fails, its batch goes back,
  and nothing is suppressed.
- **M2.** `_refuse_unevaluable_rules` refuses a predicate routed to a join source (the revocations), before the
  pull.
- **L1.** The same function runs `check_shape` on every predicate rule before the pull.

**Choices worth a look**
- **Threshold.** A share and not "every one": a value spelled in the wrong case (`'a'` against `'A'`) fires on
  real values, and a few odd records shouldn't hide the mistake. A real rule that rejects 90% of a batch also
  trips. That is loud and recoverable: the run fails, and nothing is lost.
- **Minimum sample of 5.** Below that, nothing trips. A small batch whose first candidates happen to fire would
  otherwise refuse on every run and jam the backlog.

**Tests:** 1,087 passed, 2 skipped.
- New: `TestCheckBatch` (5) in `test_rules.py`. In `test_qualification_wiring.py` (3): a misspelled QCMobile field
  trips with nothing recorded or suppressed; a join-routed predicate and a malformed rule each refuse with no
  request made.
- **Mutation check:** 5 of 5 caught: the breaker not called; the join guard removed; the early shape check
  removed; missing-source candidates counted; the minimum sample ignored. The missing-source mutant survived the
  first version of its test, which passed either way, so the test was rewritten.
