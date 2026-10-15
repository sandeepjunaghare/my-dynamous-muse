# Implementation Report — T6 PR B: `resolve_owner`, the T7 node edits, the headcount band

**Plan**: `.claude/plans/t6-enrichment-pr-b.md`   **Branch**: `feat/t6-owner`   **Status**: COMPLETE

## Summary

Stage 4, `resolve_owner`, is built. It fills `owner` for verified, officer-less survivors through a new Agent
SDK node (`app/sourcing/owner/`). A name is kept only through A10's eight rules, cited `llm_inference` to a page
the node verifiably fetched. `classify_rollup` now reads the run's Places channel, skips unverified candidates
(OQ-5), and ends degraded at its cap instead of failing the run (Amendment 3). It also stores a cited homepage
origin (fill only) and `headcount_band`. With PR A, T6 is complete. No migration; the head stays `0010_promotion`.

## Tasks completed

- B1 `OwnerJudgmentError` → `app/sourcing/exceptions.py` (UPDATE)
- B2 `OwnerCitation`, `OwnerAnswer`, `AdmittedOwner` → `app/sourcing/owner/{__init__,schemas}.py` (CREATE)
- B3 vertical-free prompt with the cited identity and email → `app/sourcing/owner/prompts.py` (CREATE)
- B4 node, isolation options, cost on every exit, `admit_owner` (A10) → `app/sourcing/owner/judgment.py` (CREATE)
- B5 SDK allow-list → `tests/test_structure.py` (UPDATE)
- B6 stage 4 → `app/tools/resolve_owner.py` (CREATE)
- B7 channel read, OQ-5 skip, degraded cap → `app/tools/classify_rollup.py` (UPDATE)
- B8 `HeadcountAnswer`, `JudgmentAnswer.website/.headcount`, `AdmittedJudgment.website/.headcount_band` →
  `app/qualification/schemas.py` (UPDATE)
- B9 the two prompt asks, `_Gate.read_for`, `_admit_website`, `_admit_headcount` →
  `app/qualification/{prompts,judgment}.py` (UPDATE)
- B10 one combined write of priority, website (fill only) and headcount → `app/tools/classify_rollup.py` (UPDATE)
- B11 five-stage E2E → `tests/sourcing/test_end_to_end.py` (CREATE)
- B12 opt-in live eval, fixture committed empty → `tests/sourcing/test_live_enrichment.py`,
  `tests/sourcing/fixtures/officerless_sample.json` (CREATE)
- B13 docs → `CLAUDE.md`, `app/sourcing/README.md`, `docs/tickets/local-prospect-engine.md`,
  `.claude/references/adding-a-vertical.md`, `_tasks/todo.md` (UPDATE)
- B14 validation + nine mutation checks (below)

## Tests added

- `tests/sourcing/owner/test_prompts.py`: no vertical word in the system prompt; every cited field rendered with
  its URL; an absent field prints nothing.
- `tests/sourcing/owner/test_judgment.py` (Replay): admitted path; the name check across case, punctuation, a
  designator inside a word and a one-word name; each of the eight omission rules with its reason, including
  "Owner / Registered Agent"; fail-closed reads (403, cross-host redirect, host mismatch); null answer; no name in
  any log; five failure paths, each recording cost (`usd=None` when there was no result); isolation.
- `tests/tools/test_resolve_owner.py` (DB): census first, with one call per verified officer-less survivor; a
  retry pays nothing; the cap degrades without raising; a failed call does not stop the batch; an omitted answer
  stores nothing; the stage runs right after `classify_rollup`. `TestOwnershipPR31L2` covers three ownership
  cases, each checking `source_url` and `retrieval_method`.
- `tests/qualification/test_stage.py` (DB): Places evidence reaches the prompt; unverified candidates are never
  sent; the cap is rewritten to degrade; a cited homepage and headcount are stored; `verify_business`'s website is
  never replaced; a disqualified candidate stores neither.
- `tests/qualification/test_judgment.py`: homepage origin admitted; `/about` refused; unfetched, Maps and registry
  URLs refused; headcount admitted, inverted, zero and uncited cases; old answers still validate; the prompt asks
  for both.
- `tests/sourcing/test_end_to_end.py` (DB): the real runner, the four real stages and a configured
  `VerifyBusinessStage`, over MockFmcsa, MockPlaces, MockCensus and Replay. Of four candidates (census-owned,
  node-resolved, unverified, predicate-removed), the run completes and Places is called 3×. Anthropic cost is
  $0.30 (2 judgment calls + 1 owner call). The listing name reaches the judgment prompt and no stored row.

## Validation results

- `ruff check` / `ruff format --check`: pass (211 files)
- `mypy .`: no issues in 214 files · `pyright`: 0 errors
- `pytest` offline: 1128 passed, 461 skipped · with the DB (5434): **1585 passed, 4 skipped** (all `live`)
- `alembic upgrade head && alembic check`: no new operations (head `0010_promotion`)
- Zero suppressions.
- **Mutation checks (9/9 caught; baseline green):**
  1. Unread URL → caught by the omission tests.
  2. Quote check → `[the quote does not contain the name]`.
  3. Agent title → `[an agent is not the owner]` and the log test.
  4. OQ-5 in `resolve_owner` → stage and E2E tests.
  5. OQ-5 in `classify_rollup` → `test_an_unverified_candidate_is_never_sent` and E2E.
  6. `places=None` → `test_places_evidence_reaches_the_prompt` and E2E.
  7. Cap raises → `test_the_cap_ends_the_stage_degraded_and_never_raises`.
  8. Website overwrite, with ownership patched → `test_verify_business_website_is_never_replaced`.
  9. No-result cost → `test_no_result_at_all`.

## Deviations from the plan

1. **Owner prompt lines say `(source: URL)`, not `(registry URL: URL)`.** The website is cited `web_lookup`, not
   to a registry. The `registry id: {id}` text the stage tests key on is unchanged.
2. **The "cost recorded with `usd=None`" tests use a typed `RunCost` spy, not a log capture.** `RunCost` has no
   per-entry read API. A `capture_logs` assertion was order-dependent, because structlog caches the `core.cost`
   logger on first use. The spy observes the same `record` call deterministically.
3. **The E2E test passes `stages=` explicitly.** It uses the real `registry_stage(MockFmcsa)` and a
   `VerifyBusinessStage` built with a mock transport, plus the three registered `STAGE` singletons. The two
   network-bound stages are built with transports, which the registry's singletons don't take. The order and
   the runner are the real ones. Candidate (d) is removed by QCMobile's `allowedToOperate = N` (a free,
   post-pool predicate), so it becomes a candidate that is then removed rather than being filtered out of the pool.
4. **The website admission also refuses a read whose URL has no host**, and keeps the port when one is present.
   The plan's `f"{scheme}://{host}"` is otherwise unchanged.
5. **`classify_rollup`'s refused write still counts `judgment_failed`** (T7's existing behaviour, kept). Only a
   refused combined write is counted, not each field.

## Issues encountered

- The mutation run's first grep matched pytest's `-x` banner rather than the summary; it was rerun with a
  baseline and the name of each failing test.
- **Left open, not answered (outside this plan):** the T6 ticket's three *Decide* items, namely the Spearpoint false
  miss, a mail-drop or no-listing verdict, and candidates with no score. CLAUDE.md: never silently answer an open
  question.
- **PB-1 still applies:** until `GOOGLE_PLACES_API_KEY` is set and freight v9 is ACTIVE, no candidate is
  verified. OQ-5 then means a live run calls neither node. Level 4 manual checks wait on that.

## Ready for the next step

All changes are complete and validated. Next: `piv-create-pr`, then `piv-review-pr`. Level 4 manual steps for the
human are in the plan (`lpe sourcing run --vertical freight --batch-size 10`, a hand spot-check of each
`owner_resolved` page, and the optional paid `LPE_LIVE_EVAL=1` run once the sample is filled locally).
