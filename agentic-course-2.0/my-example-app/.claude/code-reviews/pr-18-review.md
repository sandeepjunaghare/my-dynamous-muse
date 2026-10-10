# PR #18 Review: T7 qualification slice

**PR:** https://github.com/sandeepjunaghare/my-dynamous-muse/pull/18 · `feat/t7-qualification` → `main` · +5625 / −185, 62 files
**Reviewed at:** `2071662` · **Reviewer:** `code-reviewer` agent, in a clean context, with no part in writing the code.
The deviations documented in `.claude/reports/t7-qualification-report.md` were treated as intentional and are not flagged.

**Recommendation: REQUEST CHANGES.** One High: for some manifests the new `activate` gate cannot be
satisfied at all. Two Mediums concern the judgment node's citation gate. Validation is green.

## Validation

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` | pass (131 files) |
| `mypy .` / `pyright` | 0 issues |
| `pytest` (with Postgres) | **837 passed, 2 skipped** (the opt-in paid live eval) |
| `alembic check` | no drift |

## High

### H1. A manifest with no `bulk_file` source can never be activated

**Where:**
- `app/qualification/dry_run.py:90-108` (`_check_files`)
- `app/manifests/cli.py:115-122` (`--source-file` is `required=True`)
- `app/manifests/service.py:153` (the gate)

**Scenario:**
- The seeded fire draft (`0003`) declares only `tx_fire_marshal` (`registry_api`) and `places` (`web_lookup`).
- `dry_run` refuses an empty file map and refuses any source that is not a bulk file, so fire can never be
  dry-run, and so it can never be activated.
- The same is true of any T12-authored manifest whose authoritative source is an API. That blocks M9
  (vertical #2) behind the gate meant to protect it.
- The PR body's "refused with the dry-run remedy" is true but misleading: the remedy cannot be followed for
  this manifest.

**Fix:**
- Make `--source-file` optional, and require it only when the manifest declares a `bulk_file` source.
- With no bulk source, record a report with `files=()` and every predicate step flagged
  `not_evaluable_offline`. A person then sees, on the record, that nothing was checked against data.
- Add CLI and unit tests on a manifest with no bulk file.

## Medium

### M1. A fired rule can be admitted on registry evidence with no page read

**Where:**
- `app/qualification/judgment.py:216-234` (`evidence_reads`)
- `app/qualification/judgment.py:289` (where the gate takes `[*reads, *evidence]`)
- `tests/qualification/test_judgment.py` (`test_the_candidates_own_registry_record_is_citable`)

**Scenario:**
- A census row cannot evidence "national rollup with no local owner", yet `fired: true` citing the census
  URL is admitted with zero fetches.
- The quote is never checked, so it may be invented.
- Disqualification is permanent suppression (A3), so one such verdict removes a business for good.

**Fix:**
- Let registry evidence back signal answers only, never a judgment rule's `fired: true`. Update the test to
  assert that such a verdict is omitted.
- Checking quotes against fetched text is a larger change. Defer it with a note.

### M2. A Google Maps citation is admitted (D13), and storing it crashes the stage mid-batch

**Where:**
- `app/qualification/judgment.py:253-273` (`_Gate.admit`)
- `app/tools/classify_rollup.py:72-93`

**Scenario:**
- A fetched `google.com/maps/...` page is admitted as a citation.
- If the answer is a fired rule, the Maps URL and its quote land in `disqualification`, which has no D13
  guard.
- If the answer is a signal, the `priority` anchor is a Maps URL, and `record_candidates` re-validation
  raises a `ValidationError`.
- The stage catches only `JudgmentNodeError`, so the error escapes, the paid batch stops, and a retry pays
  again.

**Fix:**
- Make `_is_google_maps` public in `app/sourcing/schemas.py` and reject Maps citations in `_Gate.admit`,
  with a recorded reason.
- Catch `ValidationError` per candidate in the stage and count it as `judgment_failed`.

## Low

- **L1:** `tests/qualification/test_dry_run.py` (BOM test) passes even if BOM handling is removed. Assert
  `row_id_column == "dot_number"`.
- **L2:** `app/qualification/dry_run.py:126`. Blank CSV lines count as rows and as pool members, which skews
  the numbers a person reconciles against 4,449. Skip empty rows.
- **L3:** `app/qualification/dry_run.py:124`. With duplicate header names, the last one silently wins.
  Refuse duplicates with `DryRunSourceError`.
- **L4:** `app/qualification/dry_run.py:156`. The hash comes from a second read of the file, so a file
  replaced mid-run gets a hash for rows that were never counted. Hash during the parse pass.
- **L5:** `app/tools/classify_rollup.py:85-93`. A candidate disqualified by judgment still gets a `priority`
  written, so a rejected rollup can read as "live" on its candidate row. Skip the write when a rule fired.
- **L6:** `tests/qualification/*`. The test rule named `not_allowed` uses `is_true`, so it reads inverted.
  There is also no "disqualify when false" operator; authors must use `not_equals`. Rename the rule and
  document this on `RuleOperator.is_true`.

## Checked and sound
- **Evaluator:** the semantics and the `bool`/`int` ordering hold up, and the `Decimal` cast rejects NaN and
  infinity. Unjudgeable records never fire.
- **Funnel arithmetic:** `pool_before`, `removed`, `matched_standalone` and `not_evaluable` have no
  off-by-one.
- **Citation gate:** invented and predicate rule ids are dropped, a duplicate answer keeps the first, and
  the priority anchor is deterministic.
- **Read tracker move:** the moved tracker is behaviour-identical to the original, so fail-closed is
  preserved.
- **Stage merge:** the stage owns only `priority`, so the merge cannot clobber another stage's citation. The
  retry skip and the cap work as specified.
- **Activate gate:** draft check, then dry-run, then terms. Nothing is written on refusal.
- **Migration 0008:** it matches the models (names, FKs, constraints, index) and downgrades in order.
- **Type and naming rules:** no `Any`, no suppressions, no vertical literals.

## Done well
- The evaluator is pure and manifest-driven, with one exhaustive operator `match` guarded by
  `assert_never`. Unjudgeable records are surfaced, not disqualified.
- The dry-run is deterministic and streaming, records only the basename, and has a test per flag.
- The judgment node mirrors the authoring agent's isolation and records cost on every exit path, with tests.
- The stage tests assert call counts and the cap, not just outcomes.

## Next
Run `/piv-fix-review-findings` on this report: H1, M1 and M2 before merge; the Lows are cheap and can go in
the same pass. Then a human approves.
