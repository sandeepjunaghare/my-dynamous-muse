# Code Review — PR #7: T4 sourcing data model (`sourcing_run` + `candidate`)

**Reviewed**: `feat/t4-sourcing-model` → `main` at `b0430aa` · 16 files
**Method**: a fresh-eyes pass by the `code-reviewer` agent in a clean context, plus a full validation run in the
PR's worktree against a throwaway Postgres 16 (`localhost:5434/lpe_t4`; `DATABASE_URL` was overridden and
checked before every alembic call, and hosted Supabase was never touched). Every Medium finding was reproduced:
findings 1–3 against that database through the real repository inside rolled-back transactions, and finding 4
by mutation on a scratch copy of the branch.

**Recommendation: approve once findings 1–4 are fixed or explicitly deferred.** There are no Critical or High
findings, and every gate is green with the PR's own counts reproduced exactly. The four Medium findings sit
where this slice says it is strongest: the database invariant, the merge, the key, and the transaction
contract. Each is small to fix now and expensive once T5 writes real rows.

## Validation

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 70 files |
| `uv run mypy .` | pass, 69 source files |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | 218 passed, 76 skipped |
| `TEST_DATABASE_URL=…/lpe_t4 uv run pytest` | **294 passed**, 0 skipped, 2 warnings (Starlette `HTTP_422` deprecation, already on `main`) |
| `tests/sourcing/` alone (DB) | 60 passed |
| `alembic heads` | one head, `0004_sourcing` |
| `alembic downgrade 0003_seed_freight_and_fire` | clean; leaves only `alembic_version` and `vertical_manifest` |
| `alembic upgrade head` → `alembic check` | clean; "No new upgrade operations detected" |
| Suppression scan (`type: ignore`, `pyright: ignore`, `noqa`, `Any`) over `app/`, `tests/`, `alembic/` | **zero**. The only hits are the guard itself in `tests/test_structure.py` and prose in docstrings |

## What the review verified, rather than took on trust

- **Storable but not promotable holds.** `CandidateFields` gives every field the type
  `ProvenancedValue[T] | None` with `extra="forbid"`, so a raw value has no slot to go in
  (`app/sourcing/schemas.py:103-112`, and `test_a_raw_uncited_value_cannot_be_stored`). The repository dumps the
  model, so nothing reaches `fields` without passing it. On load, `CandidateResponse` validates back into
  `CandidateFields`, and `is_promotable` returns `False` for the absent field. This is proven at three levels:
  schema, repository after `expunge_all`, and service end to end. It holds through the typed API. The database
  itself does not enforce it (see finding 1 and the first Low item).
- **Provenance survives the JSONB round trip.** `test_provenance_survives_persist_then_load` expunges the session
  and compares the reloaded model to the original as a whole. That covers `source_url`, tz-aware `retrieved_at`,
  the `RetrievalMethod` enum, and both the `str` and the frozen `PostalAddress` value types.
- **The repository flushes and the service commits.** There is no `commit()` anywhere in `repository.py`. Every
  service write path commits exactly once, after its last flush (`service.py:55`, `:87`, `:121`). A batch passed
  to `record_candidates` is atomic, because the commit comes after the loop. The code is right, but the tests
  cannot see this (finding 4).
- **`0004_sourcing` is reversible.** The downgrade drops `candidate`, then the index, then `sourcing_run`, in FK
  order. The downgrade, upgrade and check round trip above was run, not inferred.
- **`RunOutcome` makes "finished as running" a type error**, and the two-sided CHECK
  `(status = 'running') = (finished_at is null)` is correct, including its NULL handling.
- **The `populate_existing` guard is tested properly.** The test keeps a strong reference to the first object,
  which is exactly what makes the identity map return a stale copy without the option. The report's "proven by
  failing" claim is consistent with that (it was not re-run here), and the report is candid about the plan's
  wrong assumption on this point.

## Findings

### Critical — none

### High — none

### 1 · Medium — `ck_candidate_registry_id_is_cited` passes when the citation is missing

`app/sourcing/models.py:129-132` · `alembic/versions/0004_sourcing.py:101-104`

`registry_id = (fields -> 'registry_id' ->> 'value')` evaluates to NULL when the key is absent or is JSON
`null`, and a CHECK passes on NULL. Verified against the database:

```
fields without registry_id     -> ACCEPTED
registry_id is json null       -> ACCEPTED
mismatched registry_id         -> REJECTED CheckViolationError
```

The model docstring says the CHECK makes the two copies "impossible to disagree". The migration says it exists
"so a slice nobody has written yet cannot write around" the invariant. The case it misses is the one the
docstring names, a registry id nobody can cite (E10). This is PR #6 finding 1 again: a database invariant that
covers only rows written correctly.

It also accepts `{"registry_id": {"value": "1"}}` with no `source_url`, `retrieved_at` or `retrieval_method`.
A constraint named `…_is_cited` checks the value but not the citation.

**Fix**, in both places:

```sql
fields -> 'registry_id' ?& array['value','source_url','retrieved_at','retrieval_method']
AND registry_id = (fields -> 'registry_id' ->> 'value')
```

Add tests for `fields={}` and for a registry id with no `source_url`, each expecting `IntegrityError`.

### 2 · Medium — the merge is last-writer-wins, so an older citation overwrites a newer one

`app/sourcing/repository.py:104-110`

`existing || incoming` is a shallow merge per field, and whoever writes last wins. Verified: write `legal_name`
from `web_lookup` dated 2026-10-05, then retry an earlier stage that writes `legal_name` from `bulk_file` dated
2026-10-01. The row now holds the older registry value. The same thing happens to the `registry_id` citation:
the QCMobile source is replaced by the census-file source.

The PR's rationale for merging is that "a retried earlier stage cannot erase later enrichment". That holds only
for fields the earlier stage does not write. T5 (census, then QCMobile) and T6 (Places verification) will
plausibly cite the same fields: name, phone and address. That is exactly where a retry downgrades verified data
to raw data, and it does so silently.

**Fix (pick one, and write the choice down for T5/T6)**:
- *Newest citation wins*: merge per key, keeping whichever side has the later `retrieved_at`. This can be one
  `jsonb_object_agg` over `jsonb_each` of both sides in the `SET` expression, or a read-merge-write under
  `SELECT … FOR UPDATE`.
- *Field ownership*: a stated rule that each field has exactly one writing stage. Then last-writer-wins can
  never cross stages. It is cheaper, but it is a convention rather than a guarantee.

Add a test either way: an older write after a newer one must not downgrade the field.

### 3 · Medium — the upsert key is not normalized or bounded, so a malformed id becomes a duplicate row or a raw DB error

`app/sourcing/schemas.py:114-120` · `app/sourcing/models.py:110`

Two problems, both verified through `upsert_candidate`:
- **No normalization.** The validator rejects blank ids, but `" 555 "` and `"555"` produce two candidate rows
  in one run. For a key whose whole purpose is idempotency, the same carrier read twice with different
  padding (a CSV census column, for example) defeats the upsert.
- **No length bound.** The column is `String(128)` and the Pydantic field is unbounded. A 129-character id
  validates and then fails in the database with a raw `DBAPIError`, not with a `ValidationError` or a
  `SourcingError`. Because `record_candidates` is atomic, one bad row aborts the whole batch.

**Fix**: in `_reject_blank_registry_id`, strip and return the stripped value, which means rebuilding the
`ProvenancedValue` with `model_copy(update={"value": stripped})`. Also reject values longer than 128 characters
there, so the limit is enforced at the edge with a clear message. Add a test for each.

### 4 · Medium — no test can tell whether the service commits

`tests/conftest.py:152-156` · `tests/sourcing/test_service.py`

"The repository flushes; the service commits" is the slice's stated transaction contract. The code honours it,
but the suite cannot detect a violation. `db_session` joins with `create_savepoint`, and every assertion reads
back through that same session, so uncommitted writes look committed. **Verified on a scratch copy:** with all
three `await self._session.commit()` calls in `service.py` replaced by `pass`, `tests/sourcing/` still passes,
60/60. A commit added to the repository would be just as invisible.

**Fix**: wrap the session's `commit` with
`patch.object(db_session, "commit", wraps=db_session.commit)`. Then assert one call per service write
(`start_run`, `record_candidates`, `finish_run`), and zero calls for each repository method.

### Low / advisory

- **`app/sourcing/models.py:112`**: the provenance guarantee is held by types, not by the database. A
  `Candidate(fields={..., "phone": "+1-817-555-0100"})` built through the ORM flushes cleanly, and the tests
  already build `Candidate` directly. One such row then makes `list_candidates`
  (`service.py:100`) raise `ValidationError` for the entire run. Either say "typed-API-only" in the README, or
  extend finding 1's CHECK to every key. Making it per-key in SQL is awkward, so the README line is probably
  the right size.
- **`app/sourcing/service.py:82-87`, `:117-121`**: there is no `rollback()` after a failed write. That matches
  the house pattern (manifests does the same, and `get_db` closes the session), but it has a consequence for
  T5: after a failed upsert, calling `finish_run(failed, …)` on the *same* session raises
  `PendingRollbackError`. T5's runner must roll back first or use a fresh session (see decision 3 below).
- **`tests/sourcing/test_repository.py:310`**: `test_a_run_must_reference_a_real_manifest` expects a bare
  `IntegrityError` with no `match=`, so any constraint failure satisfies it. Match on the FK name.
- **`tests/sourcing/builders.py:14`**: `RETRIEVED_AT` is a whole minute in UTC, so the database round trip never
  exercises microseconds or a non-UTC input being normalized. Use something like
  `datetime(2026, 10, 1, 9, 30, 15, 123456, tzinfo=ZoneInfo("America/Chicago"))` in one round-trip test.
- **`app/sourcing/models.py:31`, `schemas.py:20`, `service.py:20`**: the sourcing README documents its
  cross-slice reads of manifests, but the manifests README does not list sourcing as a reader. The plan
  forbade touching `app/manifests/`, so this is a follow-up line, not a defect.

- **`app/sourcing/repository.py:122`**: `candidate_upserted` logs `unprovenanced_fields` for the *incoming*
  write, not for the merged row. After a partial re-write it reports fields as missing that the stored row
  still holds. Compute it from `CandidateFields.model_validate(candidate.fields)`, or rename the key
  `incoming_unprovenanced_fields`.
- **`app/sourcing/repository.py:73`**: `finished_at` comes from the application clock, while `started_at` is
  Postgres `now()`. With hosted Supabase in the loop, clock skew can record a run that finished before it
  started. `test_counts_cost_and_outcome_are_persisted` asserts `finished_at >= started_at` and only passes
  because both clocks are the same machine. Use `func.now()`, then `refresh(run)` (or set the value with
  `RETURNING`).
- **`app/sourcing/service.py:139-154`**: `_require_running` checks status and then writes without a lock. That
  is fine for one user. Note, though, that `get_run` returns the identity-map object without
  `populate_existing`, so a long-lived session such as T5's runner will not see a status changed by another
  session. Two concurrent `finish_run` calls both pass, and the second overwrites the first's counts. That
  contradicts the `SourcingRunNotRunningError` docstring, though it is remote for one user. The cheapest fix is
  a conditional `UPDATE … WHERE status = 'running'` in `mark_finished`; otherwise add a sentence in T5's plan.
- **`app/sourcing/service.py:84-86`**: one round trip per candidate. That is fine at weekly-run scale, and the
  sequential form is also what keeps two copies of the same id within one batch legal (a multi-row
  `ON CONFLICT` would reject them). Leave it, but don't "optimize" it into one statement without handling that.
- **`app/sourcing/models.py:48`**: `sourcing_run.vertical` duplicates `vertical_manifest.vertical`, and nothing
  ties the two together. `start_run` sets both from one source, so it is consistent today. It is a
  denormalization for T7/T10 to keep in mind, not a defect.

## The four decisions the human is deciding

**1. The unrequested `manifest_id` FK (refuses a vertical with only DRAFT manifests): right; keep it.** The
alternative is a run whose candidates' rule ids (T7) can't be interpreted, and a run that sourced under terms
nobody accepted. Both seeds ship DRAFT, so no run can start until someone runs `manifest activate`, which is the
terms gate working as designed and not a regression. Risks the author did not name:
- the first run for any new vertical (including T12's output) fails with `ActiveManifestNotFoundError`, so
  T5's trigger endpoint must render that as a clear 409/404 and not a crashed weekly job;
- the run pins the version that was ACTIVE when it started, which is the intended behaviour, and a supersede
  mid-run is harmless;
- `vertical` is now stored twice (see Low).

**2. The merge never clears a field: acceptable, but the bigger risk is the one in finding 2.** "Can't remove a
stale citation" only lasts for one run, because each run gets its own rows (`(run_id, registry_id)`), so next
week starts clean. Inside a run, T6 legitimately needs to mark a field *refuted* (a disconnected phone, for
example). That is a different state from absent, and it is better modelled later as an explicit value than as
a cleared key. The risk the author missed is the opposite one: the merge doesn't just fail to clear, it lets an
older citation silently replace a newer one.

**3. Cost is persisted only at `finish_run`: acceptable for T4, but T5 must own the crash path, and it should be
written down.** Two risks:
- **Spend becomes invisible to the database**: a run that dies at Places call 499 records `cost = {}`.
  `core.cost.call_recorded` does log each call, but those lines carry no `run_id`, so the spend can't be tied
  back to the row. That undercuts CLAUDE.md's "the first real run sets the ceiling".
- **Orphans**: the `running` row stays open forever. If the deferred run-lock ("one running run per vertical")
  is ever added, the first crash will block every later run.

One more trap: the service never calls `rollback()`, so after a failed write, `finish_run(failed)` on the same
session raises `PendingRollbackError`.

Recommendation:
- T5's runner wraps the stages in `try/finally` and calls `finish_run(failed, cost=…)` on a fresh session,
  because the failed session's transaction is aborted.
- Bind `run_id` into the structlog context for the life of the run.
- For a hard kill, either checkpoint `cost` after each paid stage (one cheap UPDATE), or add a "reap stale
  running runs" step at the start of the next run.

None of this needs to land in T4.

**4. The `(run_id, registry_id)` key and a possible collision across two registries: acceptable as is, and the
deferral is honest.** For freight, the census file and QCMobile both key on the USDOT number, which is one
namespace, so T5 has no collision. The real exposure is a fire manifest declaring two state sources with
independent numbering. Two notes:
- the prefix fix ("T5 namespaces the ids", for example `fmcsa:1234567`) is cheaper than a key migration, but
  only if it is decided before the first real run writes unprefixed ids, so T5's plan should make that call
  explicitly;
- T7's planned *cross-run* index on `registry_id` alone already crosses verticals, so it needs the namespace
  (or the vertical) in the lookup regardless.

## Documented deviations — checked, not flagged

Both report deviations were read as intentional and are not counted as issues: the extra identity-map test and
the `model_config` frozen check. All nine "Decisions for review" are documented in the plan's Open Questions as
well as the report. Leaving `CLAUDE.md` untouched until after the T11/T12 merges follows the T2/T3 precedent. No
**undocumented** divergences from the plan were found.

## Routing (per `conventions.md` → review)

- **AGENT FIXES**:
  - finding 1, `app/sourcing/models.py:129` and `alembic/versions/0004_sourcing.py:101`;
  - finding 3, `app/sourcing/schemas.py:114`;
  - finding 4, `tests/sourcing/test_service.py` (commit spy);
  - Low, `tests/sourcing/test_repository.py:310` (FK `match=`) and `tests/sourcing/builders.py:14`
    (precise, non-UTC timestamp);
  - Low, `app/sourcing/repository.py:122` (log key) and `:73` (DB clock).
- **HUMAN DECIDES**:
  - finding 2's merge policy (newest wins, or field ownership), `app/sourcing/repository.py:104`;
  - decision 3, the crash path T5 must own, `app/sourcing/service.py:102`;
  - decision 4, namespacing ids before the first real run, `app/sourcing/models.py:128`.
- **HUMAN READS**:
  - `app/sourcing/repository.py:85-124`, the merge-upsert and the key into everything T7–T9 consume;
  - `app/sourcing/schemas.py:91-128`, the write-gate at rest.
- **HUMAN TESTS**: none needed beyond the suite; the DB-backed tests exercise the real Postgres paths.
- **FYI**:
  - `app/sourcing/service.py:139` (stale identity map in a long-lived session);
  - `app/sourcing/models.py:48` (`vertical` stored twice);
  - `app/core/cost.py:60` (per-call cost logs carry no `run_id`).

## Recommendation

**Approve once findings 1–4 are fixed (all four are small and agent-fixable, except the policy choice in 2),
or explicitly deferred to the log the way `source_url` validation was.** The slice is well built: the shape
makes the write-gate structural rather than procedural, the tests prove their claims (one was proven by
deliberately breaking it), and the documentation of each decision made this review faster. Fix before T5,
because findings 1–3 get harder to correct once real candidate rows exist in dev and prod.

Posted as a comment rather than a formal approval: GitHub does not allow approving your own pull request, and
the human merge decision is the real gate regardless.

## Fixes applied

Pushed to `feat/t4-sourcing-model` as `4f92311`, `c0b3fed`, `4d45d13`, `8f53532` and `e9866ba` (`b0430aa..e9866ba`). PR comment: https://github.com/sandeepjunaghare/my-dynamous-muse/pull/7#issuecomment-6073986923

| Finding | Status | Proving test(s) | Commit |
|---|---|---|---|
| **M1**: the registry-id CHECK passed on a missing or JSON-`null` citation | Fixed. The CHECK is wrapped in `coalesce(…, false)`. It requires a string `value` equal to the key column, plus a non-blank `source_url`, a `retrieved_at` and a `retrieval_method`. `0004` is edited in place; downgrade → upgrade → `alembic check` is clean | `test_repository.py::TestDatabaseInvariants::test_an_uncited_registry_id_is_refused` (10 cases, all accepted before) | `4f92311` |
| **M2**: the merge was last-writer-wins across stages | Fixed by **field ownership**, as decided. The stored fields become `fill || existing || owned`: the owner overwrites its own field, and any other stage only fills an empty one. `stage` is required and keyword-only on `upsert_candidate` and `record_candidates` | `TestFieldOwnership` (4 tests), `test_service.py::test_the_writing_stage_reaches_the_merge`, `test_stages.py` (coverage plus the required-param check). Mutation: restoring `existing || incoming` fails the 3 cross-stage tests | `4d45d13` |
| **M3**: the upsert key was not normalised or bounded | Fixed. `CandidateFields` strips the id while keeping its citation, and refuses more than `REGISTRY_ID_MAX_LENGTH` (128, now also the column width) with a `ValidationError` | `test_the_registry_id_is_stripped`, `test_a_registry_id_longer_than_its_column_is_refused`, `test_a_registry_id_at_the_limit_is_accepted`, `test_padding_does_not_make_a_second_row` | `c0b3fed` |
| **M4**: no test could see a missing `commit()` | Fixed. A spy wraps the real `commit`: one call each for `start_run`, `record_candidates` (per batch) and `finish_run`, and zero for a refused write or any repository method | `test_service.py::TestTransactionBoundary` (5 tests). Mutation: removing the three service commits fails 3 tests, and adding a commit to `upsert_candidate` fails 2. Source restored | `8f53532` |

### Field → owner (`app/sourcing/stages.py`, `FIELD_OWNERS`)

| Field | Owning stage |
|---|---|
| `registry_id` | `search_registry` |
| `legal_name` | `search_registry` |
| `dba_name` | `search_registry` |
| `address` | `verify_business` |
| `phone` | `verify_business` |
| `website` | `verify_business` |

`resolve_owner`, `classify_rollup` and `cluster_routes` own nothing yet. T6's owner fields add rows, and `test_every_candidate_field_has_an_owner` fails until they do.

**Decisions to check:**
- **`legal_name` / `dba_name` → `search_registry`.** The registry is the authority on legal identity, and Places returns a display name, not a legal name. Places can fill these when they are empty but never replace them. If T6 is meant to correct legal names, flip the row.
- **`website` → `verify_business` (most ambiguous).** No doc names its source. Places is the likely one, and the FMCSA census has no website. Under the conservative choice the registry can fill it but never overwrite a verified one.
- **`address` / `phone` → `verify_business`**, per T6 ("verify business identity (address, phone via Google Places)"). The registry's copy fills these until verification runs.
- **An undeclared field has no owner.** Every stage can fill it and none can overwrite it.
- **Ownership does not mark data as verified.** If verification finds nothing, the registry's address stays. T8's "verified address" will have to check `retrieval_method` / `source_url`, not merely that the field is present.

### Low findings
- **Fixed:** FK test matches `sourcing_run_manifest_id_fkey`. There is a precise, non-UTC timestamp round trip (`test_a_precise_non_utc_timestamp_survives_the_round_trip`). The log key is renamed to `incoming_unprovenanced_fields` and the log now records the `stage`. The README states "typed-API-only" for every field except `registry_id`.
- **Deferred:**
  - `finished_at` from the DB clock: needs `mark_finished` rewritten as an `UPDATE … RETURNING`. Do it together with the conditional-update fix in T5.
  - Conditional `UPDATE … WHERE status='running'` and the stale identity map: one user, so the risk is remote. T5's runner owns the session model.
  - No `rollback()` after a failed write: the house pattern. T5's runner must finish a failed run on a fresh session.
  - Manifests README "read by sourcing": the plan forbade touching `app/manifests/`. Leave it as a follow-up line.
  - One round trip per candidate: advisory. Leave it, because the sequential form is what makes duplicates within a batch legal.
  - `vertical` stored twice: FYI for T7/T10.

### Validation
ruff check and format ✓ · mypy (71 files) ✓ · pyright 0/0/0 ✓ · pytest without DB **227 passed, 99 skipped** · pytest with DB **326 passed** (sourcing 92) · `alembic heads` one head (`0004_sourcing`) · downgrade → upgrade → `alembic check` "No new upgrade operations detected", against the throwaway DB only. Zero suppressions. `alembic/env.py` is untouched.
