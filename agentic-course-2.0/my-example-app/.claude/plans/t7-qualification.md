# Feature: T7 — Qualification slice: disqualifiers, the `classify_rollup` judgment node, priority score, manifest dry-run

The following plan should be complete, but it is important that you validate documentation and codebase
patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils, types and models. `ProvenancedValue`, `RetrievalMethod`,
`is_promotable` come from `app.shared.provenance`; `DisqualifierRule`, `RuleKind`, `RuleOperator`,
`ManifestBody`, `ManifestResponse`, `QualifyingSignal`, `ScoreAxis`, `SourceKind`, `SLUG_PATTERN` from
`app.manifests.schemas`; `CandidateFields`, `CandidateResponse` from `app.sourcing.schemas`; `PipelineStage`,
`FIELD_OWNERS`, `owns` from `app.sourcing.stages`; `StageContext`, `StageResult`, `Stage` from
`app.tools.registry`; `RunCost`, `BillableKind`, `format_usd` from `app.core.cost`. Every exception derives
from `app.core.exceptions.LocalProspectEngineError`.

**Where this runs:** worktree `worktrees/t7`, branch `feat/t7-qualification`, cut from `main` at `5a80710`.
Test database port **5435** (5433 is taken; T5 has 5434, T8 has 5436). Migration **`0008`**,
`down_revision = "0005_cadence"`, re-chained at merge (merge order T5 → T8 → T7).

## Feature Description

T7 turns the rules in a founder's head into rows, and makes a rejected candidate stay rejected. It
delivers five things:

1. **A disqualifier evaluator**, driven entirely by the manifest. There are no rule literals in the slice.
   It handles every `RuleOperator`, casts text numerics, and knows which declared source each predicate
   reads. It is pure, free and deterministic.
2. **`classify_rollup`**, pipeline stage 4 and the second Agent SDK judgment node. **One call per
   candidate** decides every judgment rule in the manifest and answers its qualifying signals (D12). Each
   verdict carries a citation to a page the agent actually fetched, and an uncited verdict never
   disqualifies anyone.
3. **A `disqualification` table and re-source suppression.** It records the candidate, the rule that
   fired and when. Suppression is keyed on `(vertical, registry_id)`, so the next run, which creates new
   candidate rows, still skips it.
4. **A priority score**: Intensity (1–5) × Automatable (1–5), so 1–25. ≥16 is live and <9 is dead (E9).
   It is computed only from cited signal answers and stored as a cited `CandidateFields.priority`.
5. **`lpe manifest dry-run <id> --source-file <name>=<path>`**: the manifest quality gate decided on
   2026-10-09. It runs the free predicates over a local bulk-file extract and prints the pool after each
   rule, with sample rows. It flags dead, redundant and all-consuming rules, as well as unknown fields and
   literals never seen in the data. It records the run, and **`activate` refuses a manifest that has no
   recorded dry-run**.

## User Story

As the founder who works the weekly prospect list
I want the manifest's disqualifiers and the rollup judgment applied mechanically, cited, and remembered
So that I never see the same national rollup twice, a wrong rule is caught in a dry-run before activation
rather than after a week of wrong companies (E10), and my ~4 hrs/week go to the right 20 names.

## Problem Statement

- E7: whether a business is a rollup is decided in a founder's head and a hand-written skip list. HubSpot
  alone cannot remember a rejection, so the same rollups come back every week.
- Freight v1 had a rule matching nothing (`carrier_operation equals 'asset_based'` against real values
  A/B/C). v2 had an exclusion list that let through anything it didn't list. Only a hand check against
  the real census caught them, and `activate` checks terms of use, not correctness.
- The census is text: `power_units` arrives as a string, so a numeric compare fails without a cast.
  `allowToOperate` lives in QCMobile, not the census, so a predicate must know its source.
- Freight v3's free predicates leave a pool of 4,449, so judgment has to be **one** call per candidate
  covering every rule (~$0.08), not one call per rule.

## Solution Statement

`app/qualification/` is a new vertical slice that depends on `manifests` (it reads rules) and `sourcing`
(it reads candidates), and nothing depends on it except the CLI, the stage module and, after merge,
T5's runner.

- `rules.py` is a pure evaluator over a `RecordSet` (`{source_name: {field: raw text | None}}`). It returns
  a per-rule `RuleOutcome`: `fired`, `passed`, or `cannot_evaluate` with a reason. A record that cannot be
  evaluated is **never disqualified**, and it is counted and surfaced instead.
- `dry_run.py` streams a CSV extract per bulk-file source through the evaluator and builds a
  `DryRunReport`, which the manifests slice records.
- `judgment.py` is the Agent SDK node. It mirrors `app/manifests/agent.py` exactly (runner seam, options
  isolation, cost recorded before `ResultError`, fail-closed fetch tracking). The fetch tracking moves to
  `app/shared/agent_reads.py`, decided 2026-10-10, because T6's `resolve_owner` is the known third consumer.
- `scoring.py` holds the pure Intensity × Automatable computation from admitted signal answers.
- `models.py`, `repository.py` and `service.py` hold the `disqualification` table, the idempotent record,
  and the suppression query.
- `app/tools/classify_rollup.py` is the `STAGE` export. Its stage body reads the run's candidates, skips
  the ones already disqualified, calls the judgment node, records fired rules as disqualifications, and
  writes `priority` through `SourcingService.record_candidates(..., stage=classify_rollup)`.
- **Wiring into T5's backlog and batch selection is the last phase.** It happens after rebasing onto
  merged T5 (decided 2026-10-10: T7 owns the evaluator and wires it at merge).

## Out of Scope / Non-Goals

- Not included: **the census adapter, QCMobile client, revocations join, backlog and batch selection.**
  Those are T5's. T7 defines the `RecordSet` contract they feed, and the join's result reaches T7 as an
  ordinary field (e.g. `authority_revoked`) on a declared source, so the rule language never needs a
  "join" operator.
- Not included: **the Places evidence channel into `classify_rollup`.** T6 produces the Places response,
  and D13 says it is used in-run and never stored. T7's node accepts `places: PlacesEvidence | None`,
  always `None` until T6 adds the channel. **T7 does not edit `StageContext`.**
- Not included: **running the pipeline end to end** (T10), and **promotion of the priority score to
  HubSpot** (T9).
- Not included: changing the T12 authoring agent's prompt or `ProposedRule` to emit `source`. That is a
  follow-up; `source` is optional and defaults to the first declared source.
- Not included: activating freight v3 on dev. That is the human's step, and now goes through dry-run
  first.
- Not changing: the `upsert_candidate` merge semantics, the `Stage` contract, or `registered_stages()`.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: High. The judgment node, the dry-run gate and the evaluator all land together
  (~1,800–2,200 lines with tests, above the ticket's 900–1,300, because the dry-run and the read-tracker
  extraction were added after slicing).
**Primary Systems Affected**: new `app/qualification/`, new `app/tools/classify_rollup.py`,
  `app/manifests/{schemas,models,repository,service,cli,agent,proposal,exceptions}.py`,
  `app/sourcing/{schemas,stages}.py`, new `app/shared/agent_reads.py`, `app/core/config.py`, `app/cli.py`
  (no change expected: dry-run hangs off `manifest`), `alembic/versions/0008_*.py`, `tests/test_structure.py`
**Dependencies**: `claude-agent-sdk` (already present via T12), stdlib `csv` and `hashlib`. Nothing new to install.

## Related Work

**Implements**: T7 in `docs/tickets/local-prospect-engine.md` (lines 174–197) · **Epic**:
`docs/local-prospect-engine.architecture.md` (*Recommended approach*, *Data model*, *Open questions*:
dry-run) + decisions D1, D8, D12, D13 in the ticket doc.

**Back-references:**

- `.claude/plans/t2-vertical-manifest.md`: the rule shape and the activate gate this extends.
- `.claude/plans/t4-sourcing-model.md`: `CandidateFields`, field ownership, the upsert merge.
- `.claude/plans/t12-manifest-agent.md`: the Agent SDK pattern, read-tracking and replay harness reused here.
- PR #17 (`app/tools/registry.py`): the stage-as-a-file seam.

**Forward-references:**

- T5: emits `RecordSet`s and applies `apply_predicates` / `suppressed_registry_ids` (wired in Phase 6).
- T6: adds the Places evidence channel and becomes the third consumer of `app/shared/agent_reads.py`.
- T9: promotes `priority` as a HubSpot property, gated by `is_promotable`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files IMPORTANT: YOU MUST READ THESE FILES BEFORE IMPLEMENTING!

- `CLAUDE.md`: ground rules, especially provenance as a write-gate, D13, "vertical is data", zero
  suppressions, and the structlog naming `domain.component.action_state`.
- `app/manifests/schemas.py` (68–88 `RuleOperator`; 147–174 `DisqualifierRule`; 177–186 `QualifyingSignal`;
  256–330 `ManifestBody` and its validators). Why: the rule shape the evaluator interprets, and where
  `source` is added.
- `alembic/versions/0003_seed_freight_and_fire_manifests.py`. Why: the fire body's
  `national_rollup_no_local_owner` judgment rule is what the rollup fixtures run under, and `_cited` is
  the citation-builder pattern.
- `_tasks/todo.md` lines 546–561. Why: freight v3's real rules (`phy_cnty not_in_set` 11 FIPS counties,
  `carship not_contains 'B'`, `power_units greater_than 10`) and the measured funnel (DFW 45,855 → broker
  4,518 → ≤10 units 4,449). The dry-run must reproduce these numbers.
- `app/manifests/service.py` (89–204 `activate`). Why: where the dry-run precondition goes. Mirror its
  refusal-logging pattern exactly.
- `app/manifests/repository.py` (whole). Why: "repository flushes, service commits".
- `app/manifests/cli.py` (60–130 `register`/`dispatch`; 133–143 `_with_session`; 233–296 rendering). Why:
  where `dry-run` is registered, and the output style.
- `app/manifests/agent.py` (whole, especially 58–68 the `AgentRunner` seam, 81–106 `build_options`, 113–205
  read tracking, 208–218 `_record_cost`, 258–359 `run_agent`). Why: `judgment.py` mirrors it line for line;
  113–205 moves to `app/shared/agent_reads.py`.
- `app/manifests/proposal.py` (139–144 `PageRead`; 182–191 `normalize_url`; 194–250 `_Gate.admit`). Why:
  `PageRead` and `normalize_url` move to shared, and `_Gate.admit` is the citation rule the judgment
  node's admission mirrors.
- `app/sourcing/schemas.py` (145–221 `CandidateFields`). Why: `priority` is added here. It must be frozen,
  and the D13 validator keeps working.
- `app/sourcing/stages.py` (45–60 `FIELD_OWNERS`). Why: `priority` gets an owner row, which
  `tests/sourcing/test_stages.py:14` enforces.
- `app/sourcing/service.py` (72–109 `record_candidates`, `list_candidates`) and `app/sourcing/repository.py`
  (86–150 `upsert_candidate`). Why: how the stage writes `priority`.
- `app/sourcing/models.py` (whole) and `alembic/versions/0004_sourcing.py`. Why: the house style for
  tables. Constraints are declared in the model **and** hand-written in the migration so `alembic check`
  stays clean.
- `app/tools/registry.py` (whole). Why: the `Stage` Protocol and `StageContext` the stage module meets.
- `app/core/cost.py` (89–110 `check_cap`) and `app/core/config.py` (53–62). Why: the circuit-breaker and
  settings pattern for the judgment node.
- `app/cli.py` (51–107). Why: one-line errors, never a traceback. Nothing to change if `dry-run` is a
  `manifest` subcommand.
- `tests/test_structure.py` (51–72). Why: the Agent SDK import allow-list. It gains
  `app/qualification/judgment.py` and `app/shared/agent_reads.py`.
- `tests/manifests/replay.py` (whole: `Replay`, `web_fetch_call`, `web_fetch_result`, `result_line`,
  `with_result`). Why: the offline SDK replay. Reuse it, don't fork it.
- `tests/manifests/conftest.py` and `tests/manifests/builders.py`. Why: the CLI tests commit their setup
  (`committed_draft`), and `a_body` / `a_vertical` are the builders.
- `tests/sourcing/builders.py`. Why: `sourced`, `a_candidate`, `an_active_manifest`.
- `tests/conftest.py` (`requires_db`, `migrated_database`, `db_session`). Why: database tests roll back
  and skip without `TEST_DATABASE_URL`.

### New Files to Create

- `app/shared/agent_reads.py`: `PageRead`, `normalize_url`, `ReadTracker` (public), moved from manifests.
- `app/qualification/__init__.py`
- `app/qualification/schemas.py`: `RecordSet`, `RuleOutcome`, `RuleVerdict`, `PredicateResult`,
  `JudgmentAnswer` / `SignalAnswer` (the node's structured output), `AdmittedRule`, `AdmittedSignal`,
  `AdmittedJudgment`, `PlacesEvidence`, `DisqualificationResponse`. (`PriorityScore` and `ScoreBand` live
  in `app/sourcing/schemas.py`; see task 4.)
- `app/qualification/rules.py`: the pure predicate evaluator.
- `app/qualification/dry_run.py`: CSV extract → `DryRunReport` (the manifests type).
- `app/qualification/scoring.py`: pure Intensity × Automatable.
- `app/qualification/prompts.py`: `SYSTEM_PROMPT` and `build_user_prompt` for the judgment node. It names
  no vertical.
- `app/qualification/judgment.py`: the Agent SDK node (`run_judgment`) and `admit_judgment`.
- `app/qualification/models.py`: the `Disqualification` table.
- `app/qualification/repository.py`: idempotent `record`, `list_for_candidate`, `suppressed_registry_ids`.
- `app/qualification/service.py`: `QualificationService`, which owns commits.
- `app/qualification/exceptions.py`: `QualificationError` → `JudgmentNodeError` (502),
  `RuleNotEvaluableError` (422), `DryRunSourceError` (422).
- `app/tools/classify_rollup.py`: `STAGE`.
- `alembic/versions/0008_qualification.py`: `disqualification` and `manifest_dry_run`.
- `tests/qualification/__init__.py`, `builders.py`, `test_rules.py`, `test_scoring.py`, `test_dry_run.py`,
  `test_judgment.py`, `test_repository.py`, `test_service.py`, `test_stage.py`, `test_live_eval.py`,
  `fixtures/` (CSV extract, judgment transcripts, `rollup_eval_set.json` placeholder).
- `tests/shared/test_agent_reads.py`: the moved read-tracker tests, if any lived in `test_agent.py`.

### Relevant Documentation YOU SHOULD READ THESE BEFORE IMPLEMENTING!

- [Claude Agent SDK — Python reference](https://docs.claude.com/en/api/agent-sdk/python#claudeagentoptions):
  `ClaudeAgentOptions` (`tools`, `allowed_tools`, `permission_mode`, `setting_sources`,
  `strict_mcp_config`, `max_turns`, `max_budget_usd`, `output_format`). Why: the node's isolation. Copy
  `build_options` from `app/manifests/agent.py:81-106`; that code is the verified usage.
- [Agent SDK — structured outputs](https://docs.claude.com/en/api/agent-sdk/structured-outputs): the
  `output_format={"type": "json_schema", "schema": ...}` and `ResultMessage.structured_output` contract.
- [Python `csv.DictReader`](https://docs.python.org/3/library/csv.html#csv.DictReader): the dry-run reads
  extracts streaming, never whole-file into memory (the TX census is ~378k rows).
- [Alembic — autogenerate limitations](https://alembic.sqlalchemy.org/en/latest/autogenerate.html#what-does-autogenerate-detect-and-what-does-it-not-detect).
  Why: the migration is hand-written, and the model mirrors it so `alembic check` is clean.

### Patterns to Follow

**Naming:** modules `snake_case`, classes PascalCase, StrEnums for closed vocabularies (`ScoreBand`,
`OutcomeKind`). Slugs validated with `SLUG_PATTERN`.

**Frozen, cited values** (from `app/sourcing/schemas.py:86-117`). Anything wrapped in `ProvenancedValue` is
a frozen model with tuples, never lists:
```python
class PriorityScore(BaseModel):
    model_config = ConfigDict(frozen=True)
    intensity: int = Field(ge=1, le=5)
    automatable: int = Field(ge=1, le=5)
```

**Error handling** (from `app/manifests/exceptions.py`): a `ClassVar` `default_code` and `status_code`,
plus keyword-only context attributes. Route and CLI code never catches these; `app/cli.py:82` prints
them as one line.

**Logging:** `logger = get_logger(__name__)`; events `qualification.<component>.<action>_<state>`, e.g.
`qualification.judgment.run_completed`, `qualification.rules.rule_not_evaluable`,
`qualification.service.disqualification_recorded`, `manifests.service.activation_refused` with
`reason="dry_run_missing"`.

**Repository flushes, service commits** (`app/manifests/repository.py:1-7`). Upserts use
`insert(...).on_conflict_do_nothing/update(constraint=...)` (`app/sourcing/repository.py:124-139`).

**Agent SDK node** (`app/manifests/agent.py:258-359`): build options, iterate the runner, record cost the
moment the `ResultMessage` arrives, map `ClaudeSDKError` / error subtypes / missing or invalid
`structured_output` to a typed 502 error, then validate with Pydantic.

**Tests:** class-grouped, docstrings stating the *why*; DB tests use `@requires_db` and `db_session`
(rolled back); CLI tests use committed fixtures and throwaway verticals; SDK tests use `Replay(stream)` as
the `runner`.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation: contracts, shared reads, schema additions

The types everyone else codes against: `source` on rules, `priority` on candidates, the read tracker in
`shared/`, and the dry-run record shape.

### Phase 2: Pure core: evaluator, scoring, dry-run

**Depends on:** Phase 1 (rule `source`, `DryRunReport`).
No database, no network. This is the cheapest part to get exactly right, and everything else stands on it.

### Phase 3: Persistence: migration 0008, disqualification, dry-run record, activate gate

**Depends on:** Phase 1. **Independent of:** Phase 2 (can be built in parallel).

### Phase 4: The judgment node and the stage

**Depends on:** Phases 1–3.

### Phase 5: CLI, structure guards, docs

**Depends on:** Phases 2–4.

### Phase 6: Merge-time wiring into T5 (only after T5 and T8 are on `main`)

**Depends on:** T5 merged; this branch rebased; migration re-chained. Decided 2026-10-10: T7 wires its
evaluator and suppression into T5's backlog and batch selection here, so exactly one evaluator exists.

---

## STEP-BY-STEP TASKS

IMPORTANT: Execute every task in order, top to bottom. Each task is atomic and independently testable.
Every `uv` command needs the sandbox disabled (memory: `uv-commands-need-sandbox-disabled`).
For DB tiers: `docker run --rm -d --name lpe-test-pg-t7 -p 5435:5432 -e POSTGRES_PASSWORD=test postgres:16`
and `export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5435/postgres`.

### Phase 1

#### 1. CREATE `app/shared/agent_reads.py` (move, don't copy)

- **IMPLEMENT**: Move `PageRead` (`proposal.py:139-144`), `normalize_url` (`proposal.py:182-191`), and from
  `agent.py` `FETCH_TOOL`, `REDIRECT_MARKER`, `_host`, `_result_text`, `_FetchOutcome`, `_unread_reason`,
  `_ReadTracker` (113–205). Rename `_ReadTracker` → `ReadTracker` (public, it now has two consumers). Add
  a `log_prefix: str` constructor argument so each consumer logs under its own domain
  (`manifests.agent.fetch_failed` stays byte-identical; the judgment node logs `qualification.judgment.*`).
  The module docstring states the fail-closed rule and names its consumers: manifests, qualification, and
  T6 next.
- **UPDATE** `app/manifests/agent.py` and `app/manifests/proposal.py` to import from `app.shared.agent_reads`.
  Keep `PageRead` re-exported from `proposal.py` **only if** a test imports it from there
  (`tests/manifests/test_proposal.py:15` does). Prefer updating the test import instead.
- **GOTCHA**: this is a pure move. `tests/manifests/` must pass **unchanged in behaviour**, and log event
  names must not change (tests may assert them).
- **VALIDATE**: `uv run pytest tests/manifests -q && uv run mypy app/shared app/manifests && uv run pyright app/shared app/manifests`
- **SATISFIES**: AC 6 (prerequisite)

#### 2. UPDATE `app/manifests/schemas.py`: `DisqualifierRule.source`

- **IMPLEMENT**: `source: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=64)`, with a
  docstring saying which declared source the predicate's `field` is read from; `None` means the
  manifest's first declared source. In `_shape_matches_kind`, a judgment rule must not carry `source`.
  In `ManifestBody`, add `_rule_sources_are_declared`: every non-`None` `rule.source` must be in
  `source_names()`. Add `ManifestBody.source_for(rule) -> str` returning the resolved name.
- **GOTCHA**: existing rows (seeds, dev v1–v3) have no `source` key, so `None` must load. Do **not** add
  `extra="forbid"`.
- **VALIDATE**: `uv run pytest tests/manifests/test_schemas.py tests/manifests/test_seeds.py -q`
- **SATISFIES**: AC 1 (a field's source)

#### 3. ADD dry-run record shapes to `app/manifests/schemas.py`

- **IMPLEMENT**: frozen models:
  - `DryRunFlag` (StrEnum): `matches_nothing` (the rule fires on 0 records evaluated standalone),
    `excludes_nothing` (it fires on some records, but removes 0 at its position because earlier rules
    already removed them), `excludes_everything` (the pool after this rule is 0), `unknown_field` (the
    field is absent from the extract's header), `value_not_seen` (an `equals`, `in_set` or `contains`
    literal never appears in the data, which is the v1 check), `not_evaluable_offline` (the rule's source
    has no file because it is not a bulk file), `malformed` (the operator and value shapes don't fit).
  - `DryRunSample` (`fields: tuple[tuple[str, str], ...]`, the record's key fields only).
  - `DryRunStep`: `rule_id`, `source`, `pool_before`, `removed`, `matched_standalone`, `not_evaluable`,
    `flags: tuple[DryRunFlag, ...]`, `removed_samples: tuple[DryRunSample, ...]`.
  - `DryRunSourceFile`: `source_name`, `file_name` (basename only, **never a full path**: 12-factor),
    `sha256`, `rows`.
  - `DryRunReport`: `manifest_id`, `files`, `pool_start`, `steps`, `pool_end`, `passing_samples`, and
    `flagged() -> tuple[str, ...]`.
  - `DryRunResponse`: the row form, with `id`, `ran_at`, `ran_by`, `report`.
- **VALIDATE**: `uv run mypy app/manifests && uv run pyright app/manifests`
- **SATISFIES**: AC 8

#### 4. UPDATE `app/sourcing/schemas.py` + `app/sourcing/stages.py`: `priority`

- **IMPLEMENT**: in `app/sourcing/schemas.py`, a frozen `PriorityScore` (`intensity`, `automatable` 1–5;
  `score` property = product; `band` property → `ScoreBand.live` ≥16 / `dead` <9 / `middle` otherwise).
  Put `ScoreBand` beside it. Then `CandidateFields.priority: ProvenancedValue[PriorityScore] | None = None`,
  appended **last** in the class, with a docstring citing E9. In `FIELD_OWNERS`, add
  `"priority": PipelineStage.classify_rollup` **as the last entry**.
- **GOTCHA**: `PriorityScore` lives in `sourcing/schemas.py` because `CandidateFields` must import it and
  sourcing must not import qualification (that would be a cycle once T5 calls qualification).
  `qualification/scoring.py` builds it. Appending last keeps the merge with T5, which may add fields too,
  a trivial conflict.
- **VALIDATE**: `uv run pytest tests/sourcing/test_stages.py tests/sourcing/test_schemas.py -q`
- **SATISFIES**: AC 5

#### 5. CREATE `app/qualification/schemas.py` + `exceptions.py`

- **IMPLEMENT**:
  - `type SourceRecord = Mapping[str, str | None]`; `type RecordSet = Mapping[str, SourceRecord]` (source
    name → raw fields). Docstring: **this is the contract T5's adapters emit**. Values stay raw text;
    casting is the evaluator's job.
  - `OutcomeKind` (StrEnum: `fired`, `passed`, `not_evaluable`); `RuleOutcome` (rule_id, kind, reason
    when not_evaluable, the observed value); `PredicateResult` (`outcomes` in declaration order;
    `first_fired() -> RuleOutcome | None`; `not_evaluable()`).
  - The node's output contract, made **not frozen** because it is model output validated once:
    `Citation` (url, quote); `RuleAnswer` (rule_id, fired: bool, reason, citation | None);
    `SignalAnswer` (signal_id, answer, axis_score: int 1–5, citation | None); `JudgmentAnswer` (rules,
    signals). Mirror `app/manifests/proposal.py:59-136`.
  - `PlacesEvidence` (frozen; `display_name`, `business_status`, `types: tuple[str, ...]`). Docstring:
    D13, in-memory only, never persisted. It is `None` until T6.
  - `AdmittedRule` (rule_id, `ProvenancedValue[str]` of the reason), `AdmittedSignal` (signal_id,
    axis: `ScoreAxis`, axis_score, `ProvenancedValue[str]` of the answer), `AdmittedJudgment` (fired rules,
    `priority: ProvenancedValue[PriorityScore] | None`, omitted: tuple of (label, reason)).
  - `DisqualificationResponse` (from_attributes).
  - Exceptions: `QualificationError` → `JudgmentNodeError(reason)` 502, `RuleNotEvaluableError` 422,
    `DryRunSourceError(source_name)` 422.
- **VALIDATE**: `uv run mypy app/qualification && uv run pyright app/qualification`
- **SATISFIES**: AC 1, 2

### Phase 2

#### 6. CREATE `app/qualification/rules.py`

- **IMPLEMENT**: `evaluate(rule: DisqualifierRule, records: RecordSet, *, source: str) -> RuleOutcome`
  and `apply_predicates(body: ManifestBody, records: RecordSet) -> PredicateResult`. The second evaluates
  **predicate rules only, in declaration order, every rule** (no short-circuit, because the dry-run
  needs standalone matches). Semantics:
  - Source absent from `records` → `not_evaluable("source_missing")`. Field absent or blank →
    `not_evaluable("field_missing")`.
  - Text is `.strip()`ped and compared exactly, case-sensitively. These are registry codes. Document it.
  - `equals` / `not_equals`: the value must be a `str`, `int` or `bool` scalar, compared against the field
    text as `str(value)`.
  - `contains` / `not_contains`: substring on the stripped text (`'B' in 'C;B'`).
  - `in_set` / `not_in_set`: the value must be a `tuple[str, ...]`, and the check is membership.
  - `greater_than` / `less_than`: the value must be an `int` (and not a `bool`). The field is cast with
    `Decimal(text)`. `InvalidOperation` → `not_evaluable("not_numeric")`. **This is the `power_units`
    cast.**
  - `is_true`: parse `Y/YES/TRUE/T/1` → true and `N/NO/FALSE/F/0` → false, case-insensitively; anything
    else is `not_evaluable("not_boolean")`.
  - A shape mismatch, such as `in_set` with a scalar, raises `RuleNotEvaluableError`. The dry-run catches
    it per rule and flags it `malformed`.
  - **Never disqualify on `not_evaluable`.** Log `qualification.rules.rule_not_evaluable` once per
    (rule, reason) per call, at debug, so 378k rows don't flood the log.
- **PATTERN**: pure functions, `match operator:` over the `RuleOperator` enum, exhaustive with
  `assert_never`.
- **GOTCHA**: `bool` is a subclass of `int`, so check `isinstance(value, bool)` first. No rule literals
  and no vertical names anywhere (`tests/test_structure.py:219`).
- **VALIDATE**: `uv run pytest tests/qualification/test_rules.py -q`
- **SATISFIES**: AC 1

#### 7. CREATE `tests/qualification/test_rules.py` (write alongside task 6, test first)

- **IMPLEMENT**: a parametrized test per operator, covering fires, passes and not-evaluable. The
  freight-v3-shaped cases: `phy_cnty not_in_set (11 FIPS)`, `carship not_contains 'B'` on
  `C;B` / `C` / `B` / blank, `power_units greater_than 10` on `"11"`, `"10"`, `" 7 "`, `""`, `"N/A"`.
  Also test that `source` routes to a second source (`qcmobile.allowToOperate is_true`), that a missing
  source is not_evaluable, the bool-is-not-int case, that a malformed rule raises, that declaration order
  is preserved, and that a judgment rule is ignored by `apply_predicates`.
- **SATISFIES**: AC 1

#### 8. CREATE `app/qualification/scoring.py` + `tests/qualification/test_scoring.py`

- **IMPLEMENT**: `score(body: ManifestBody, signals: Sequence[AdmittedSignal]) -> PriorityScore | None`.
  The axis score for each `ScoreAxis` is the **rounded mean** (half up) of the admitted signals feeding
  that axis. If either axis has no admitted signal, return `None`: an absent score is honest, a guessed
  one is E18. Signals whose id is not in the manifest are ignored and logged.
- **TESTS**: the 4×4=16 boundary is live and 3×3=9 is middle; 2×4=8 is dead; one axis missing gives
  `None`; an unknown signal id is ignored; the mean rounds half up (4,5 → 5).
- **VALIDATE**: `uv run pytest tests/qualification/test_scoring.py -q`
- **SATISFIES**: AC 5

#### 9. CREATE `app/qualification/dry_run.py`

- **IMPLEMENT**: `dry_run(manifest: ManifestResponse, files: Mapping[str, Path], *, samples: int = 3) -> DryRunReport`.
  - Every key in `files` must be a declared source of kind `SourceKind.bulk_file`. Otherwise raise
    `DryRunSourceError`. At least one file is required.
  - **Single source per pass:** iterate the rows of each bulk-file source with `csv.DictReader`
    (`newline=""`, `encoding="utf-8-sig"`), streaming. A row becomes `RecordSet` `{source: row}`. Rules
    whose resolved source has no file are reported `not_evaluable_offline` and skipped from the funnel.
    If more than one bulk file is passed, each source's rules run over that source's rows (no
    cross-source join offline), and the report says so.
  - For each row, `apply_predicates`. Sequential funnel: a row is removed at the **first** rule that
    fires on it, which gives `removed` per step. `matched_standalone` counts every rule that fires on
    the row regardless of position.
  - Flags: `unknown_field` (the field is not in `reader.fieldnames`), `value_not_seen` (track whether any
    row's text equals or contains the literal, cheaply, during the same pass), `matches_nothing`,
    `excludes_nothing`, `excludes_everything`, `malformed`.
  - Samples: the first `samples` removed rows per rule and the first `samples` passing rows, in file
    order (deterministic), keeping only the fields the rules read plus a row identifier. That identifier
    is the first header column, and the report names it.
  - `sha256` and `rows` are computed in the same pass (hash the raw bytes in a separate read; the file is
    local).
  - **Writes nothing and makes no network call.**
- **GOTCHA**: never load the file into memory. Keep the file name only (no absolute path) in the report.
- **VALIDATE**: `uv run pytest tests/qualification/test_dry_run.py -q`
- **SATISFIES**: AC 8, AC 9

#### 10. CREATE `tests/qualification/test_dry_run.py` + `fixtures/census_extract.csv`

- **IMPLEMENT**: a ~30-row hand-built CSV with census-shaped headers (`dot_number,phy_cnty,carship,power_units,...`)
  and a manifest via `a_body` with freight-v3-shaped rules plus two deliberately bad ones:
  `carrier_operation equals 'asset_based'` (v1: flags `unknown_field`, or with the column present, A/B/C
  values give `value_not_seen` + `matches_nothing`) and a rule fully shadowed by an earlier one
  (`excludes_nothing`). Assert exact step counts, the flags, sample determinism (two runs give equal
  reports), `not_evaluable_offline` for a `qcmobile`-sourced rule, `DryRunSourceError` for an undeclared
  or non-bulk source, and that only the file basename appears in the report.
- **SATISFIES**: AC 8, 9

### Phase 3

#### 11. CREATE `app/qualification/models.py` + UPDATE `app/manifests/models.py`

- **IMPLEMENT** `Disqualification`: `id` (UUID PK), `candidate_id` (FK `candidate.id`, not null), `run_id`
  (FK `sourcing_run.id`), `manifest_id` (FK `vertical_manifest.id`), `vertical` String(64), `registry_id`
  String(128), `rule_id` String(64), `rule_kind` String(16) with
  CHECK `rule_kind in ('predicate','judgment')`, `source_url` Text not null, `retrieval_method`
  String(32) not null, `evidence` Text null (the observed value for a predicate, the quote and reason
  for a judgment), `disqualified_at` timestamptz default `now()`.
  `UniqueConstraint("candidate_id","rule_id", name="uq_disqualification_candidate_rule")` and
  `Index("ix_disqualification_vertical_registry_id","vertical","registry_id")`.
- **IMPLEMENT** in `app/manifests/models.py`: `ManifestDryRun`: `id`, `manifest_id` (FK, not null),
  `ran_at` (timestamptz default now), `ran_by` String(128), `report` JSONB not null, with
  `Index("ix_manifest_dry_run_manifest_id","manifest_id")`. It lives in **manifests**, because it is
  manifest lifecycle state and `activate` reads it.
- **PATTERN**: `app/sourcing/models.py` (constraints declared in the model too, so `alembic check` sees them).
- **GOTCHA**: the suppression key is `(vertical, registry_id)`, **not** `candidate_id`. Every run makes new
  candidate rows (`uq_candidate_run_registry_id`), so suppressing by candidate id would suppress nothing.
- **VALIDATE**: `uv run mypy app && uv run pyright app`

#### 12. CREATE `alembic/versions/0008_qualification.py`

- **IMPLEMENT**: `revision = "0008_qualification"`, `down_revision = "0005_cadence"`. The docstring says it is
  pre-numbered for Wave 5 and re-chained at merge. Hand-write both tables, matching the models textually.
  `downgrade` drops `disqualification`, then `manifest_dry_run`.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q` (single head), then against the throwaway DB
  `uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1 && uv run alembic upgrade head`
- **SATISFIES**: AC 3, 8

#### 13. CREATE `app/qualification/repository.py` + `service.py`

- **IMPLEMENT** repository: `record(...)` is an `insert ... on_conflict_do_nothing(constraint="uq_disqualification_candidate_rule")`
  that returns the row (re-select on conflict); `list_for_candidate(candidate_id)`;
  `disqualified_candidate_ids(run_id) -> frozenset[UUID]`;
  `suppressed_registry_ids(vertical, registry_ids: Collection[str] | None = None) -> frozenset[str]`.
- **IMPLEMENT** service: `QualificationService(session)` with:
  - `disqualify_by_predicates(candidate: CandidateResponse, run: SourcingRunResponse, manifest: ManifestResponse, records: RecordSet) -> RuleOutcome | None`,
    which records the **first** fired predicate. The citation is the source's `base_url`, or a
    record-level URL if the caller passes one, with `retrieval_method` = the source kind's matching
    `RetrievalMethod` (`SourceKind` and `RetrievalMethod` are deliberately parallel:
    `manifests/schemas.py:37-51`).
  - `record_judgment(candidate, run, manifest, admitted: AdmittedJudgment) -> list[DisqualificationResponse]`,
    which records **every** fired judgment rule.
  - `suppressed_registry_ids(vertical, registry_ids=None)` and `is_suppressed(vertical, registry_id)`.
  - Each write commits once and logs `qualification.service.disqualification_recorded`.
- **VALIDATE**: `uv run pytest tests/qualification/test_repository.py tests/qualification/test_service.py -q` (with `TEST_DATABASE_URL`)
- **SATISFIES**: AC 3, 4

#### 14. CREATE `tests/qualification/test_repository.py` + `test_service.py`

- **IMPLEMENT** (`@requires_db`, `db_session`): idempotency (recording twice gives one row), a judgment
  with two fired rules gives two rows, and an unfired one gives none. **Re-source suppression across two
  runs:** run 1 disqualifies registry `555` → run 2 under the same vertical:
  `suppressed_registry_ids(vertical, ["555","556"]) == {"555"}`. A different vertical is not suppressed.
  A `not_evaluable` record is never disqualified.
- **SATISFIES**: AC 4 (the T7-side half; the end-to-end half is Phase 6)

#### 15. UPDATE `app/manifests/{repository,service,exceptions}.py`: record a dry-run, gate `activate`

- **IMPLEMENT**:
  - Repository: `record_dry_run(manifest_id, report: DryRunReport, ran_by) -> ManifestDryRun` (flush) and
    `latest_dry_run(manifest_id) -> ManifestDryRun | None`.
  - Service: `record_dry_run(manifest_id, report, actor) -> DryRunResponse`. It refuses unless the
    manifest exists and `report.manifest_id == manifest_id`, then commits. It works on any status but is
    meaningful on a DRAFT.
  - In `activate`, after the not-draft check and **before** the terms checks, if `latest_dry_run` is
    `None`, log `manifests.service.activation_refused` with `reason="dry_run_missing"` and raise
    `DryRunNotRecordedError` (409; context `manifest_id`). Its message names the remedy:
    `lpe manifest dry-run <id> --source-file <source>=<extract.csv>`.
  - The gate requires a dry-run **recorded**, not a **clean** one. Flags are for the human to read
    (decided 2026-10-09).
- **UPDATE tests**: every existing `activate` call in `tests/manifests/test_service.py`, `test_routes.py`,
  `test_seeds.py` and `test_cli.py` needs a recorded dry-run first. Add the builder
  `tests/manifests/builders.py::a_dry_run(manifest_id)` (an empty-steps `DryRunReport`) and a helper that
  records it through the repository (no commit). In the CLI suite, `committed_draft` gains a committed
  dry-run, or a sibling fixture `committed_draft_with_dry_run` is added. Add new tests: activation without
  a dry-run is refused with `dry_run_missing`, and nothing is written (status still draft, no terms).
- **GOTCHA**: `tests/sourcing/builders.py::an_active_manifest` uses `repository.mark_active` directly and
  bypasses the gate. Leave it so: T5 and T8 depend on it.
- **VALIDATE**: `uv run pytest tests/manifests -q` (with `TEST_DATABASE_URL`)
- **SATISFIES**: AC 8

### Phase 4

#### 16. ADD settings to `app/core/config.py` (+ `.env.example`)

- **IMPLEMENT**: `rollup_classifier_model: str = "claude-opus-5-5"` (the ~$0.08 measurement was taken on
  this model, so it is the cost basis; tune later), `rollup_classifier_max_turns: int = Field(default=8, gt=0)`,
  `rollup_classifier_max_budget_usd: Decimal = Field(default=Decimal("0.50"), gt=0)` (per call),
  `max_judgment_calls_per_run: int = Field(default=500, gt=0)`. The 500 is a circuit breaker, mirroring
  the Places cap (D7), not a budget. Add a comment block mirroring lines 53–62.
- **VALIDATE**: `uv run pytest tests/core/test_config.py -q`

#### 17. CREATE `app/qualification/prompts.py`

- **IMPLEMENT**: a `SYSTEM_PROMPT` that **names no vertical**. It explains the job: decide each listed
  judgment rule for one business and answer each qualifying signal with a 1–5 axis score. Rules for
  the model:
  - Cite a page you fetched, with a short quote, for every `fired: true` and every signal answer.
  - If you cannot cite, say `fired: false` with `citation: null` and explain.
  - Never infer ownership from the name alone.
  - Search the business by its legal name and city.
  - Places evidence, when given, is context only and is not a citable page.

  `build_user_prompt(candidate: CandidateFields, rules, signals, places: PlacesEvidence | None)` renders
  the cited identity fields (name, DBA, address, phone, website, each with its source URL), then the
  judgment rules (id and description), then the signals (id, question, axis).
- **PATTERN**: `app/manifests/prompts.py` (no vertical examples).
- **GOTCHA**: the D13 statement goes in the prompt. Places content is passed for this call only, and is
  never returned or stored.

#### 18. CREATE `app/qualification/judgment.py`

- **IMPLEMENT**:
  - `build_options(settings)`: tools `("WebSearch","WebFetch")`, `permission_mode="dontAsk"`,
    `setting_sources=[]`, `strict_mcp_config=True`, model, `max_turns` and `max_budget_usd` from
    settings, `output_format` json_schema of `JudgmentAnswer`, and `env` with the API key when it is set.
    This is a copy of `agent.py:81-106`.
  - `JudgmentRun` dataclass: answer, reads, turns, cost_usd.
  - `run_judgment(candidate, body, *, cost: RunCost, places=None, runner=None, settings=None, clock=_utc_now) -> JudgmentRun`.
    Same control flow as `run_agent` (`agent.py:258-359`), using `ReadTracker(clock, log_prefix="qualification.judgment")`
    and raising `JudgmentNodeError(reason=...)`.
  - `admit_judgment(answer, reads, body, *, evidence_urls) -> AdmittedJudgment`, the citation gate,
    mirroring `_Gate.admit` (`proposal.py:194-250`):
    - A fired rule is admitted only if its citation URL normalizes to a successful read **or** to one of
      `evidence_urls` (the candidate's own field `source_url`s, which are already-cited facts).
    - Its `ProvenancedValue` uses that read's URL and time with `RetrievalMethod.llm_inference`.
    - Rule ids not declared as **judgment** rules in the body are dropped and logged (the model cannot
      invent a disqualifier).
    - Signals follow the same rule, then go to `scoring.score`. The `priority` citation is the
      lexicographically first admitted signal's URL, so the choice is deterministic.
    - Every omission is recorded with a reason.
  - **An uncited `fired: true` is omitted and never disqualifies.**
- **GOTCHA**: one call per candidate covers every judgment rule (D12). The node does **not** call
  `check_cap`; the stage does, before each call.
- **VALIDATE**: `uv run pytest tests/qualification/test_judgment.py -q`
- **SATISFIES**: AC 2, 5

#### 19. CREATE `tests/qualification/test_judgment.py` + transcripts

- **IMPLEMENT**: build streams with `tests/manifests/replay.py`'s `web_fetch_call`, `web_fetch_result` and
  `result_line`, plus a `with_result`-style helper parametrized by path. Store recorded fixtures under
  `tests/qualification/fixtures/judgment_*.json`, in the same `{"stream": [...]}` shape.
  - **Four rollup fixtures** (Impact Fire, Summit Fire, Century Fire, Control Systems) under a fire-shaped
    manifest with `national_rollup_no_local_owner`: each fetches a page and returns
    `fired: true` + citation → admitted → one fired rule.
  - **Locals:** fired false → nothing.
  - Fired true, citing an unfetched URL → omitted, nothing fired. A 403 or a redirect on the cited page →
    omitted (fail-closed holds).
  - Citing a candidate evidence URL (census) is admitted.
  - An undeclared rule id is dropped. A predicate rule id returned as judgment is dropped.
  - Signals on both axes → `priority` admitted with the right band; one axis only → `priority None`.
  - Error subtypes (`error_max_turns`, `error_max_budget_usd`), no result, and invalid structured output
    → `JudgmentNodeError`, **with cost recorded** in each case.
  - `build_options` isolation is asserted (tools, `setting_sources == []`, `strict_mcp_config`).
- **GOTCHA**: these prove the wiring and the gate, not model accuracy. Say so in the module docstring;
  accuracy is task 23.
- **SATISFIES**: AC 2, 6 (offline half)

#### 20. CREATE `app/tools/classify_rollup.py`

- **IMPLEMENT**: `class _ClassifyRollup` with `stage = PipelineStage.classify_rollup` and
  `async def run(self, context: StageContext) -> StageResult`:
  1. `candidates = await SourcingService(context.session).list_candidates(context.run_id)`.
  2. Skip the ids in `QualificationRepository.disqualified_candidate_ids(run_id)`, i.e. the ones the free
     batch checks already removed.
  3. Skip any candidate whose `priority` is already set. That is the idempotent retry of this stage, and
     it does not pay twice.
  4. If the manifest has no judgment rules **and** no signals, return `{classified: 0}` and make no call.
  5. Per candidate, call `context.cost.check_cap(BillableKind.anthropic_tokens, ...)`. Note that the
     count is turns, so the cap is compared against **calls**: keep a local call counter and raise
     `CostLimitExceededError` at `settings.max_judgment_calls_per_run`. The runner (T5) decides
     degraded. Then `run_judgment` → `admit_judgment` → `QualificationService.record_judgment` →
     `SourcingService.record_candidates(run_id, [fields.model_copy(update={"priority": ...})], stage=PipelineStage.classify_rollup)`
     when `priority` is not `None`.
  6. A `JudgmentNodeError` for one candidate is logged
     (`qualification.stage.candidate_failed`), counted as `judgment_failed`, and the loop continues.
     One bad SDK run must not sink a paid batch.
  7. Return `StageResult(counts={"classified": n, "judgment_disqualified": d, "judgment_failed": f, "scored": s, "not_scored": u})`.
  8. `STAGE = _ClassifyRollup()`.
- **GOTCHA**: Places evidence is `None` here. T6 adds the channel. `model_copy(update=...)` skips
  validators, but the repository re-validates (`app/sourcing/repository.py:116-119`).
- **VALIDATE**: `uv run pytest tests/tools/test_registry.py tests/qualification/test_stage.py -q`

#### 21. CREATE `tests/qualification/test_stage.py`

- **IMPLEMENT** (`@requires_db`): `registered_stages()` includes `classify_rollup` in position. A run with
  three candidates (one already disqualified, one rollup, one local) via a `Replay` runner injected
  through a module-level seam (`judgment.sdk_runner`, monkeypatched like T12's `as_query`): the counts
  are correct, one disqualification row is written, `priority` is written with `llm_inference`
  provenance, and the already-disqualified candidate is never sent to the SDK (the replay saw two
  prompts). Re-running the stage sends zero prompts (idempotent). With the cap at 1, the stage raises
  `CostLimitExceededError` after one call. A failed SDK run gives `judgment_failed` and the loop continues.
- **SATISFIES**: AC 2, 3, 5

### Phase 5

#### 22. UPDATE `app/manifests/cli.py`: `dry-run`

- **IMPLEMENT**: the subcommand
  `dry-run <manifest_id> --source-file NAME=PATH (repeatable, required) [--samples 3] [--actor cli]`.
  1. Parse `NAME=PATH` with an argparse `type` that rejects a missing `=` and a nonexistent file
     (`ArgumentTypeError`).
  2. Load the manifest (`ManifestService.get`) in one short session, run `qualification.dry_run.dry_run`
     **with no session open** (it may take a minute on 378k rows; mirror `_run_propose`), then record it
     in a second short session.
  3. Print the funnel table:
     `rule | source | before | removed | standalone | n/a | flags`, then the sample rows per rule, the
     passing samples, a `FLAGGED:` summary, and the dry-run id. Finish with a line that `activate` now
     accepts this manifest's dry-run and that a person must read the flags.
  - Update `QUALITY_REVIEW_NOTE` to point at `dry-run`, and the module docstring.
- **GOTCHA**: the CLI is the composition edge. `manifests.cli` → `qualification.dry_run` is fine, but
  `manifests.service` must **not** import `qualification`.
- **VALIDATE**: `uv run pytest tests/manifests/test_cli.py -q`, then manually
  `uv run lpe manifest dry-run --help`.

#### 23. CREATE `tests/qualification/test_live_eval.py` + register the `live` marker

- **IMPLEMENT**: add `markers = ["live: calls a paid external API; opt-in with LPE_LIVE_EVAL=1"]` to
  `[tool.pytest.ini_options]` (needed because of `--strict-markers`). Tests are skipped unless
  `LPE_LIVE_EVAL == "1"` and an Anthropic credential is resolvable.
  - Load `tests/qualification/fixtures/rollup_eval_set.json`:
    `{"rollups": [{legal_name, city, state, website?}], "locals": [...]}`. Commit a **placeholder** with
    the 4 rollups only and `"locals": []`, plus a docstring saying the human supplies ≥20 known-local DFW
    fire companies.
  - Run `run_judgment` and `admit_judgment` under a fire-shaped body.
  - Assert all 4 rollups fire, and that the false-positive rate on locals is <5% when there are ≥20
    locals. Otherwise `pytest.skip("labelled locals not supplied")`.
  - Print the per-company verdict and total cost.
- **GOTCHA**: this costs ~$0.08 × N per run. Never run it in `/piv-validate`.
- **SATISFIES**: AC 6 (accuracy half, deferred to the human's labelled set)

#### 24. UPDATE `tests/test_structure.py`

- **IMPLEMENT**: extend the SDK allow-list (line 59) to
  `{"app/manifests/agent.py", "app/qualification/judgment.py", "app/shared/agent_reads.py"}`, and update
  the docstring: the classify_rollup node has landed, and the shared read tracker imports SDK message
  types only.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q`

#### 25. UPDATE docs

- **IMPLEMENT**:
  - `CLAUDE.md` *Architecture map*: add `app/qualification/` and `app/tools/classify_rollup.py`, mention
    `0008`, and add the `lpe manifest dry-run` command line. Remove "Manifest *quality* review is still
    an open question — no gate", which is now gated by dry-run.
  - `docs/tickets/local-prospect-engine.md` T7: note `source` on rules, suppression keyed on
    `(vertical, registry_id)`, and the decisions of 2026-10-10.
  - `.claude/references/adding-a-vertical.md`: "run `lpe manifest dry-run` before `activate`".
  - Append a T7 section to `_tasks/todo.md` (never overwrite).
- **VALIDATE**: `/rules-check-drift` (advisory)

#### 26. Full validation on the branch

- **VALIDATE**: `/piv-validate` (ruff, format, mypy, pyright, pytest with `TEST_DATABASE_URL` on 5435,
  alembic check). All green, zero suppressions. Then `/piv-create-pr`; review happens before Phase 6.

### Phase 6: merge-time wiring (after T5 and T8 are merged to `main`)

#### 27. REBASE onto `main`; RE-CHAIN `0008` onto T8's `0007`

- **IMPLEMENT**: `down_revision = "0007_..."` (T8's revision id). Resolve the `CandidateFields` and
  `FIELD_OWNERS` append conflicts with T5, keeping both sides.
- **VALIDATE**: `uv run pytest tests/test_structure.py -q` (one head), `uv run alembic upgrade head && uv run alembic check`

#### 28. WIRE the evaluator into T5's backlog and suppression into batch selection

- **IMPLEMENT**:
  - Where T5's FMCSA adapter builds the pool from census rows, replace any interim filter with
    `apply_predicates(body, {source: row})`. A row is kept when nothing fires.
  - Where T5 runs the batch's free checks (QCMobile `allowToOperate`, the revocations join), build the
    `RecordSet` with those fields under their declared source names and call
    `QualificationService.disqualify_by_predicates`.
  - Where T5 selects the batch, exclude `suppressed_registry_ids(vertical, pool_ids)`.
  - Name each edited file and function in the PR body (risk noted in `_tasks/todo.md` Wave 5: T5 and T7
    both touch `app/sourcing/`).
- **TEST**: the ticket's two-run test, end to end. Run 1 disqualifies a fixture candidate. Run 2 over
  the same fixture pool does not select it. Also: one evaluator exists, so a grep for a second operator
  `match` outside `app/qualification/rules.py` comes up empty.
- **VALIDATE**: `/piv-validate`
- **SATISFIES**: AC 4 (end to end), AC 1 (the evaluator drives the pipeline)

---

## TESTING STRATEGY

### Unit Tests (no database, no network)
`test_rules.py` (each operator × fires / passes / not-evaluable, the casts, source routing, malformed
rules) · `test_scoring.py` (band boundaries 16 and 9, a missing axis, rounding) · `test_dry_run.py`
(exact funnel counts, all seven flags, determinism, basename only) · `test_judgment.py` (replay: the
citation gate, fail-closed reads, error paths with cost recorded, options isolation) ·
`test_schemas.py` additions (rule `source` validation, `priority` frozen and D13-safe).

### Integration Tests (`@requires_db`, port 5435)
`test_repository.py` / `test_service.py` (idempotent disqualification, suppression across two runs
keyed on vertical + registry id) · `test_stage.py` (the stage over a real run, with a Replay runner) ·
manifests suite (activate refuses without a dry-run, records and reads a dry-run, and the CLI `dry-run`
end to end against a committed draft and a fixture CSV). Phase 6 adds the end-to-end two-run
suppression test through T5's runner.

### Opt-in live eval
`test_live_eval.py`, marked `live`: the M6 accuracy bar on a labelled set the human supplies.

### Edge Cases
- `power_units` is `""`, `"N/A"`, `" 7 "` or `"10.0"`. A blank `carship`. A county code with stray spaces.
- A rule naming an undeclared source: refused at the schema. A rule whose source is not a bulk file:
  `not_evaluable_offline` in the dry-run.
- A judgment answer that fires a predicate rule's id, or an undeclared id: dropped.
- The model cites a page it fetched that returned 403 or redirected cross-host: omitted.
- A manifest with zero judgment rules and zero signals: the stage makes no call.
- The cap is reached mid-batch: `CostLimitExceededError` after exactly N calls.
- A stage retry: no second call, and no duplicate disqualification row.
- Two dry-runs recorded: `activate` uses the latest (existence is all that's checked).
- CSV with a BOM, or a header with trailing spaces (normalise the header names with `.strip()`).

---

## VALIDATION COMMANDS

### Level 1: Syntax & Style
`uv run ruff check . && uv run ruff format --check .`

### Level 2: Types
`uv run mypy . && uv run pyright`

### Level 3: Tests
`uv run pytest -q` (offline tier), then with
`TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5435/postgres uv run pytest -q` (full tier),
and `uv run alembic upgrade head && uv run alembic check` against the same database.
Or everything at once: `/piv-validate`.

### Level 4: Manual Validation
1. `uv run lpe manifest dry-run --help`.
2. Against the throwaway DB: create a DRAFT (fixture), then `uv run lpe manifest activate <id> --accept-terms fmcsa`
   → refused with `dry_run_missing`. Then `uv run lpe manifest dry-run <id> --source-file fmcsa=tests/qualification/fixtures/census_extract.csv`,
   then `activate` again → accepted.
3. **The real check (human, dev data):** with the TX census extract the freight v3 review used,
   `uv run lpe manifest dry-run ecdc1c8e-f6d3-4eec-81a7-6a80cacb70d3 --source-file fmcsa=<census.csv>`
   must reproduce the recorded funnel for the rule steps: DFW 45,855 → broker 4,518 → ≤10 units **4,449**.
   A mismatch means the operator semantics (e.g. substring vs token for `carship`) differ from the hand
   check. **Stop and reconcile; don't tune the numbers.** It writes one `manifest_dry_run` row on dev
   Supabase, which is machine state, and nothing goes to HubSpot.
4. Optional, paid: `LPE_LIVE_EVAL=1 uv run pytest tests/qualification/test_live_eval.py -s` once the
   labelled locals exist.

### Level 5: Additional
`/rules-check-drift` · `mcp__codebase-search__find_references` on `PageRead` / `ReadTracker` (no stale
imports left in `manifests`).

---

## ACCEPTANCE CRITERIA

- [ ] **AC 1**: the disqualifier evaluator is driven entirely by the manifest, with no rule literals or
      vertical names in `app/`. It handles all nine operators, including `not_contains` and `not_in_set`,
      casts text numerics (`power_units`), and resolves each predicate's declared `source`. The
      revocations join reaches it as a field on a declared source, not as a model call.
- [ ] **AC 2**: `classify_rollup` is stage 4 and the second Agent SDK node. **One call per candidate**
      decides every judgment rule and answers every signal. A fired verdict is stored only with a
      citation to a fetched page or the candidate's own cited evidence. Places evidence is accepted
      in-memory and never stored (D13).
- [ ] **AC 3**: the `disqualification` table records the candidate, the rule that fired, its kind,
      citation and time. Recording is idempotent.
- [ ] **AC 4**: re-source suppression, keyed on `(vertical, registry_id)`: a disqualified business is not
      selected by the next run. This is tested across two runs at the service level now, and end to end
      through T5's runner in Phase 6.
- [ ] **AC 5**: the priority score is Intensity × Automatable = 1–25, ≥16 live and <9 dead, computed only
      from cited signals. It is absent (never guessed) when an axis has none, and stored as a cited
      `CandidateFields.priority` owned by `classify_rollup`.
- [ ] **AC 6**: in replay fixtures, the four known rollups (Impact Fire, Summit Fire, Century Fire,
      Control Systems) classify as rollup. The <5% false-positive bar is measured by the opt-in `live`
      eval on a human-supplied labelled set (decided 2026-10-10).
- [ ] **AC 7**: the judgment calls are circuit-broken per run (`max_judgment_calls_per_run`). Cost is
      recorded through `RunCost` on success **and** failure.
- [ ] **AC 8**: `lpe manifest dry-run <id> --source-file name=path` runs the free predicates over a local
      bulk-file extract, prints the pool after each rule with sample removed and passing rows, writes
      only a `manifest_dry_run` row, and makes no network call. `activate` refuses a manifest with no
      recorded dry-run.
- [ ] **AC 9**: the dry-run flags a rule that matches nothing, a rule that excludes nothing at its
      position, a rule that excludes everything, an unknown field, and an unseen literal. There is a
      test for each.
- [ ] `/piv-validate` green: ruff, mypy, pyright, pytest (both tiers), alembic check. Zero suppressions.
      The structure guards pass, with the SDK allow-list extended by exactly two files.

---

## COMPLETION CHECKLIST

- [ ] All tasks 1–26 completed in order on `feat/t7-qualification`; tasks 27–28 after T5 and T8 merge
- [ ] Each task's validation passed when it was done
- [ ] Full suite green, offline and with the database
- [ ] Manual Level 4 steps 1–2 done; step 3 handed to the human with the exact command
- [ ] `_tasks/todo.md` T7 section appended, with a review once done

---

## OPEN QUESTIONS / ASSUMPTIONS

**Decided by the human, 2026-10-10:**

1. T7 owns the evaluator and wires it into T5 at merge (Phase 6).
2. The dry-run reads local bulk-file extracts.
3. `classify_rollup` gets WebSearch + WebFetch, and the read tracker moves to `app/shared/`.
4. Offline replay plus an opt-in live eval.

**Assumptions. Flag at plan review if any is wrong:**

- **A1. Operator text semantics.** Values are stripped and compared case-sensitively, and `contains` is a
  substring test. Level 4 step 3 (reproducing 4,518 / 4,449) is the check. If freight v3's hand count
  used token semantics for `carship`, `contains` changes to token-on-`;`.
- **A2. "Excludes nothing" means redundant at its position.** It is read as distinct from "matches
  nothing" (dead everywhere), and `excludes_everything` is added as a third flag. If the ticket meant
  something else by "a rule that excludes nothing", say so.
- **A3. Suppression ignores the manifest version.** A business disqualified under v3 stays suppressed
  under v4, even if v4 drops the rule. Re-opening it is a manual delete for now. The row records
  `manifest_id`, so a later "re-qualify on rule change" is possible.
- **A4. Census rows that fail the free predicates become no candidate and no `disqualification` row.**
  That is 377k rows; the pool is re-derived deterministically, at no cost, on each refresh. Only batch
  checks and judgment write rows.
- **A5. The activate gate requires a recorded dry-run, not a clean one.** The human reads the flags.
- **A6. The priority score's axis is the rounded mean of the admitted signals feeding it.** Freight v3
  may declare only an `automatable` signal; if so, it scores **nothing** until it gains an `intensity`
  signal. That is honest, but it means freight's `priority` will be absent on the first runs. Is a
  manifest edit wanted?
- **A7. Judgment model default `claude-opus-5-5` and a 500-call circuit breaker.** Both are settings.
  `claude-sonnet-5-5` is the obvious cost lever once the live eval shows it holds the <5% bar.
- **A8. `rules.py` and `scoring.py` stay in `qualification/`.** Moving them to `shared/` would be early.

**Still open, not blocking T7:**

- Who supplies the ≥20 labelled DFW local fire companies for the live eval (the fire contacts in HubSpot,
  read-only, are a candidate source; no HubSpot writes).
- T12 follow-up: teach the authoring agent's prompt and `ProposedRule` to emit `source`.

## NOTES (open canvas)

**Why `priority` is a candidate field and verdicts are rows.** `priority` is a prospect fact T9 will push
to HubSpot, so it belongs in the cited `CandidateFields` and passes through the write-gate.
A disqualification is the opposite: machine memory that never reaches HubSpot and must outlive the run,
so it is its own table keyed for cross-run lookup. Storing verdicts on the candidate would have made
suppression a JSONB scan across every past run.

**Why the stage writes `priority` through `SourcingService`.** That keeps field ownership in one place
(`FIELD_OWNERS`), and a retry of stage 4 can overwrite its own `priority` without touching T5's or T6's
citations.

**Why the dry-run record lives in `manifests/`.** `activate` must read it, and `manifests` must not
import `qualification` (qualification reads manifests). The CLI composes the two, which is the one
place both directions are allowed.

**Data flow, per run (after Phase 6):**
```
census rows ─apply_predicates─▶ pool (T5 persists) ─minus suppressed─▶ batch(150)
  batch ─QCMobile/revocations RecordSet─▶ disqualify_by_predicates ─▶ disqualification rows
  ─▶ verify_business (T6, Places in-memory) ─▶ classify_rollup: 1 call/candidate
        ├─ fired judgment rules (cited) ─▶ disqualification rows
        └─ signals (cited) ─▶ PriorityScore ─▶ candidate.priority (llm_inference)
  ─▶ resolve_owner (T6) ─▶ cluster_routes (T8)
```
(The stage order in `PipelineStage` puts `resolve_owner` before `classify_rollup`. D12 says owner runs
"census first", and T6 decides whether `resolve_owner` skips candidates disqualified at stage 4; that
is not T7's to reorder.)

**Size risk.** This is ~2× the ticket's estimate. If the PR is too big to review, split at the Phase 3/4
boundary: PR A covers the evaluator, dry-run, gate and disqualification table (tasks 1–15, 22, 24); PR B
covers the judgment node, stage, scoring and live eval. PR A alone unblocks activating freight v3.

## AMENDMENTS

- 2026-10-10: implemented tasks 1–26. These deviations are recorded in `.claude/reports/t7-qualification-report.md`:
  - The read tracker is split into `shared/page_reads.py` (pure) and `shared/agent_reads.py` (SDK types).
  - The judgment cap counts calls locally, because `RunCost` counts turns.
  - `value_not_seen` fires only when none of a rule's literals is seen.
  - Repository tests are folded into `test_service.py`.
  - Four log event names were renamed for the naming guard.
