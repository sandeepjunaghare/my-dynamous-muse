# PR #33 review: the promotion slice, the write-gate, HubSpot create and the ledger (T9)

**Verdict: fix the three data-integrity Mediums (M1–M3) before merging.** There are no Critical or High issues, and
validation passes. GitHub won't let the author approve their own PR, so this verdict is posted as a comment. M1–M3
are each a narrow case, but each one ends in a wrong HubSpot write or a lost founder decision. Those are the two
things this slice exists to prevent.

The deep review was done by two `code-reviewer` agents in clean contexts. One covered the promote path and the
migration; the other covered review sync, the CLI, the gateway and routing. M1, M2 and M3 were then checked against
the code at `8ed9aac`, and each failure scenario is reachable as described.

The slice's design holds up:
- The write-gate is enforced before any create.
- D13 is structural: no field except `business_check` may cite Google Maps, and `business_check` is never mapped.
- Every ledger transition is a compare-and-set, and the intent to create is committed before each create.
- `FakeCrm` models search lag and lost create responses, which is what made these edge cases reviewable at all.

## Validation (head `8ed9aac`, rebased onto `main` at `2c12758`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 200 files formatted |
| mypy strict | ✅ 203 files, no issues |
| pyright strict | ✅ 0 errors, 0 warnings |
| pytest (DB tier, Postgres 16 on port 5434) | ✅ 1,503 passed, 2 skipped (paid live eval) |
| alembic upgrade head (empty DB) / check | ✅ applies / no drift; one head, `0010_promotion` |

The 8 deviations documented in `.claude/reports/t9-promotion-report.md` count as intentional and are not flagged.

## Issues

### Medium

**M1. A second interrupted company create can duplicate the Company.** `app/promotion/service.py:529`
- **The gap:** the recovery guard compares the clock with `row.created_at`, which is when the row was claimed.
  Nothing records when a create was last attempted. `updated_at` moves only in `PromotionRepository.transition`
  (`app/promotion/repository.py:175`).
- **Failure:**
  1. Pass 1: the company create times out after HubSpot recorded it. The guard correctly defers recovery for 10
     minutes.
  2. Pass 2, 11 minutes later: the key search misses, so recovery creates the company. That create is also
     ambiguous, for example another 5xx during a HubSpot incident.
  3. The operator re-runs seconds later. `created_at` is now more than 10 minutes old, so nothing is deferred.
     Search lags the pass-2 create, both the key search and the domain/phone search miss it, and a second company
     is created.
- **Tests miss it:** the two recovery tests cover only one failure.
- **Fix:**
  - Stamp the attempt time and commit it before every `create_company`, fresh or recovered. Either add a nullable
    `create_attempted_at` column, or do a `pending_create → pending_create` transition, which already sets
    `updated_at`.
  - Base the guard on the later of `created_at` and that stamp.
  - Test: lose two company-create responses, advance 11 minutes between them, re-run immediately, and assert one
    company plus `recovery_deferred`.

**M2. A row left `enrolling` is enrolled even if the founder has since rejected it.**
`app/promotion/service.py:670-672`, `:759-768`
- **The gap:** `sync_reviews` retries `enrolling` rows first, and calls `_enrol` without reading
  `lpe_review_status` again. Only `pending_review` rows reach `_review_statuses`.
- **Failure:**
  1. The founder accepts company X, and the first sync's `enrol` gets a 5xx. The row stays `enrolling`.
  2. The founder changes X to `rejected`.
  3. The next sync creates a `Cadence 1/3 · call` task for a rejected business and marks the row `enrolled`.
- This breaks OQ3 ("rejected → left alone") and is the nag the plan's notes warn about. No test covers it.
- **Fix:**
  - Include `enrolling` rows in the batch read, and enrol only while the value is still `accepted`.
  - Otherwise move the row to `rejected`, or back to `pending_review`. `_move` hard-codes
    `from_status=pending_review`, so it needs a `from_status` parameter.
  - Test the scenario above.

**M3. A company missing from a batch read is permanently marked `withdrawn`, whatever the reason.**
`app/promotion/service.py:729-734`, `app/promotion/client.py:664`, `app/promotion/schemas.py:428-438`
- **The gap:** `BatchReadResponse` ignores the 207 `errors` list, so a deleted company and a per-item read failure
  look the same.
- **Failure:** one company in a chunk fails to read for any reason other than "not found". An accepted or pending
  row moves to `withdrawn`. Nothing reads `withdrawn` again, and the `(vertical, registry_id)` key stops the
  candidate being promoted again. The founder's acceptance is lost without anyone being told.
- **Confidence:** HubSpot's 207 behaviour is documented in the code's docstrings, not confirmed live. The failure
  needs only one per-item error, though, and the code can't tell it apart from a deletion.
- **Fix:**
  - Parse `errors`, and withdraw only on an explicit `OBJECT_NOT_FOUND`. Or have the client return the not-found
    ids separately.
  - Leave any other absence `pending_review`, and count it as a read failure.

**M4. The `_Row` snapshot is untested on the path that motivated it.** `app/promotion/service.py:119-140`;
`tests/promotion/test_review_sync.py`
- **The gap:** deviation 5 says `enrol` rolls back after a lost race, which expires ORM objects. The only
  already-enrolled test exits at the early `get_by_contact` check, which doesn't roll back.
- **Failure:** a refactor back to ORM rows passes the whole suite. In production it would crash on the second row
  after a lost race, with an async expired-attribute error.
- **Fix:** test two accepted rows where the first enrol loses the race (`raced_by_another_run`). Assert that the
  second row is still enrolled and that the first reports its orphan task.

### Low

- **L1. A malformed HubSpot body still produces a traceback.** `app/promotion/client.py:525`, `:538`, `:664`.
  - The client doesn't wrap `model_validate` or `response.json()`, so a `ValidationError` or `JSONDecodeError`
    escapes the handlers in `app/cli.py:97-120`. That breaks the "never a traceback" promise.
  - In `promote` it aborts the pass. The ledger stays `pending_create`, so nothing is duplicated.
  - **Fix:** raise `HubSpotResponseShapeError` from the client.
- **L2. A blank `HUBSPOT_UI_DOMAIN` builds `https:///…` record URLs.** `app/core/config.py:76-79`,
  `app/promotion/mapping.py:156`.
  - The comment says blank counts as unset, but that's true only for the portal id.
  - **Fix:** fall back to the default when the value is blank, and add a test.
- **L3. A dry run can report different duplicate counts from the real pass.** `app/promotion/service.py:360-361`.
  - **Failure:** the dry run adds a candidate's keys to `seen_keys` before the portal check. So a later candidate
    that shares a key with a portal duplicate counts `duplicate_in_ledger` in a dry run but
    `duplicate_in_hubspot` in a real pass.
  - The writes are the same either way.
  - **Fix:** record keys only for a would-create or would-fail outcome.
- **L4. Nothing at the service level tests that a refused contact payload creates no company.**
  `app/promotion/service.py:317-323`.
  - **The gap:** deleting the contact gate from `_writes` fails no test. That is the plan's AC 7, and it isn't
    listed as a deviation.
  - **Fix:** patch `contact_write` to return a `None` property. Assert zero writes, no ledger row, and
    `failed == 1`.
- **L5. The lost-claim branch is untested.** `app/promotion/service.py:297-300`.
  - **Fix:** add a test that inserts the row between `get_by_registry_id` and `claim`.
- **L6. `_mark_duplicate` ignores the compare-and-set result.** `app/promotion/service.py:485-498`.
  - When the transition loses a race, it still logs and counts a duplicate, unlike `_record_company` and
    `_finish_contact`.
  - **Fix:** return `None` and skip the count and log.
- **L7. A founder's change of mind after a sync is ignored without a word.** `app/promotion/service.py:673-674`.
  - `rejected` and `withdrawn` are final, and only `pending_review` rows are read.
  - **Fix:** if that is the intent, say so in `app/promotion/README.md`. If not, log a warning when a decided
    company's status no longer matches the ledger.

## Routing

**AGENT FIXES**
- M1 `service.py:529`, M2 `service.py:670`, M3 `service.py:729`, M4 `test_review_sync.py`
- L1 `client.py:525`, L2 `mapping.py:156`, L3 `service.py:360`, L4 `service.py:317`, L5 `service.py:297`,
  L6 `service.py:485`

**HUMAN DECIDES**
- M1 `service.py:529`: a new `create_attempted_at` column (migration `0011`), or reuse `updated_at` through a
  self-transition. The column is clearer, but dev Supabase is already at `0010`.
- M2 `service.py:759`: a decision changed away from `accepted` while the row was `enrolling` goes to `rejected`
  (final), or back to `pending_review` (re-read next sync).
- L7 `service.py:673`: are review decisions final once synced?

**HUMAN READS**
- `app/promotion/service.py:513-545`: the recovery path, where a duplicate Company could come from.
- `app/promotion/service.py:655-735`: sync's decision loop, which writes cadence tasks.
- `app/promotion/eligibility.py`: the write-gate's required set.

**HUMAN TESTS**
- The report's Level 4 steps: a dry-run `promote`, then accept and reject one company each in the portal and run
  `sync`. The first real `promote` creates `lpe_review_status`, which can't be undone, at
  `app/promotion/properties.py`.

**FYI**
- D13 holds: `app/shared/provenance.py` refuses a Maps citation on every field except `business_check`.
- The routing exclusion (`app/routing/service.py`) is run-scoped, and routes stay balanced (10+10, not 12+12).
- No `Any` or suppressions. Event names follow `domain.component.action_state`.

## Recommendation
Run `piv-fix-review-findings` on this report. Answer the two HUMAN DECIDES questions on M1 and M2 first, fix
M1–M4, and take the Lows in the same pass, since each is a few lines. Then run validation again and merge.
