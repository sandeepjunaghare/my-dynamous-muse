# PR #14 review, round 3: T13 adoption after the round-2 fixes (`7e69d39`)

**Recommendation: approve.** Both round-2 findings are resolved. This round found no issue at any severity. All
findings from rounds 1 and 2 are closed. The remaining test gaps are tracked in #15 on purpose.

A fresh `code-reviewer` agent did this review. It read both earlier rounds and every touched file in full, and it
skimmed the PR's whole app code for anything High or Critical. The author ran validation on the PR head.

## Validation (`7e69d39`)

| Check | Result |
|---|---|
| `ruff check` · `ruff format --check` | clean · 103 files formatted |
| `mypy .` · `pyright` | no issues in 102 files · 0 errors (strict, no suppressions) |
| `pytest` with a database (Postgres 16) | **674 passed, 0 skipped** |
| `alembic check` | no new operations |
| New and extended tests against `c9943ec` | all 4 race tests fail there, and pass on `7e69d39` |

## Round-2 findings

| ID | Status | Evidence |
|---|---|---|
| N1 | Resolved | `AlreadyEnrolledError.orphan_task_id` is set only when this run created the task (`created_here`). A task reused by key is the winner's and is never reported. `AdoptionReport.orphaned_tasks` reaches the CLI line `…enrolled first — close in HubSpot: <id>`. Every other place the exception is built keeps the default `None`, and `None` is correct at each of them. |
| N2 | Resolved | A plan is appended only after `_apply` returns, and `_apply` adds to `adopted` or `parked` only after a commit. So on apply, every plan has a state, and removing the `failed` and `done` filters changes no behaviour. Hand tasks of parked contacts are still listed for closing. |

## Notes (not issues)

- **`raced_by_another_run`** is a realistic simulation. Two inserts in one transaction make the second flush
  hit `uq_cadence_state_contact`. `enrol`'s rollback discards both rows and leaves the session usable. The CLI
  test cannot leave a committed row, and `fresh_contact` deletes by contact id as a backstop.
- **A non-unique `IntegrityError`** is re-raised and becomes `database_error`. A task this run created is then
  not listed as orphaned. That is a different failure mode from the race, and it is reported as a failure.
- **No test covers "lost race, then the next entry adopts".** The rollback is the standard path, and the
  database-error test in the same file already proves the next entry still adopts.

## Done well across the three rounds

- **The anchor rule holds end to end.** The first sync after adoption cannot credit an activity twice. The
  live run checked this: `sync --dry-run` would advance 0.
- **Each fix was pinned by a test that was first watched failing on the previous head.**
- **No HubSpot field writes.** The fake portal rejects any request it does not model, so this is proved
  structurally, not just asserted.
- **Strict typing throughout.** The test doubles are typed with ParamSpec and use no suppressions.

## Next

Nothing blocks the merge. A person reviews the code and these three reviews, then merges.
