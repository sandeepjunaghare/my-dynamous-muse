# Implementation Report — T4 Sourcing data model (`sourcing_run` + `candidate`, field-level provenance)

**Plan**: `.claude/plans/t4-sourcing-model.md`  **Branch**: `feat/t4-sourcing-model`  **Status**: COMPLETE

## Summary

The sourcing workbench now has its two tables. `sourcing_run` records one execution of a brief
(vertical · ICP band · geography) under one manifest **version**, with its timings, stage counts,
cost snapshot and status (`running → completed | degraded | failed`). `candidate` records one business
that run found. Every field on it is a `ProvenancedValue[T]` stored in JSONB, and the upsert is
idempotent on `(run_id, registry_id)`. "Storable but not promotable" falls out of the shape: an uncited
field is *absent* (`None`), the row still persists and reloads, and `is_promotable` refuses that field.
The ticket's round-trip test shows every citation coming back from Postgres exactly as it went in,
including `retrieved_at` (tz-aware) and the retrieval method enum.

## Meta

- **Files added**: `app/sourcing/{__init__,schemas,models,exceptions,repository,service}.py`,
  `app/sourcing/README.md`, `alembic/versions/0004_sourcing.py`,
  `tests/sourcing/{__init__,builders,test_schemas,test_repository,test_service}.py`,
  `.claude/plans/t4-sourcing-model.md`, this report
- **Files modified**: `alembic/env.py` (registers the two models for autogenerate)
- **Lines**: roughly +1,600 in code, tests and slice docs (the plan and this report are extra);
  `alembic/env.py` +6 −1

## Tasks completed

| Task | File | |
|---|---|---|
| Slice exceptions (404 not-found, 409 not-running) | `app/sourcing/exceptions.py` | CREATE |
| Brief, statuses/outcomes, `CandidateFields`, cost summary, responses | `app/sourcing/schemas.py` | CREATE |
| `SourcingRun`, `Candidate` + CHECKs, unique key, FKs | `app/sourcing/models.py` | CREATE |
| Hand-written migration `0004_sourcing` ← `0003_seed_freight_and_fire` | `alembic/versions/0004_sourcing.py` | CREATE |
| Model registration | `alembic/env.py` | UPDATE |
| Repository: create/get/finish run, merge-upsert, list (deterministic order) | `app/sourcing/repository.py` | CREATE |
| Service: `start_run` (ACTIVE manifest required), `record_candidates`, `finish_run`, reads | `app/sourcing/service.py` | CREATE |
| Slice README | `app/sourcing/README.md` | CREATE |

## Tests added — 60, all passing

| Module | Count | Covers |
|---|---|---|
| `test_schemas.py` (no DB) | 27 | slug brief fields; the inherited ICP-band ordering check; `registry_id` required and non-blank; unknown keys refused; **a raw uncited value cannot be stored**; `unprovenanced_fields()`; `is_promotable` False for an absent field; editable address cannot be cited; JSON round-trip keeps tz and enum; `RunOutcome` excludes `running`; cost snapshot has every kind and keeps `Decimal` exact; negative / non-slug counts refused |
| `test_repository.py` (DB) | 20 | server defaults on a new run; brief + manifest recorded; `mark_finished` persists counts/cost/detail; **provenance survives persist → load** (`expunge_all` then reload); **unprovenanced field storable but not promotable**; absent fields are absent JSON keys; same input twice is one row; a write that knows less keeps the citation; a later cited value replaces; **an object held in the session is refreshed** (the `populate_existing` guard); same id in two runs is two rows; ordering; DB refuses duplicate key, key/citation mismatch, running-with-finish-time, finished-without-finish-time, unknown status, dangling manifest FK |
| `test_service.py` (DB) | 13 | draft-only or missing manifest → `ActiveManifestNotFoundError`; the run records the active manifest id; unknown run 404 and finished run 409 for writes; idempotent `record_candidates`; storable-not-promotable end to end; `finish_run` persists outcome/counts/cost/detail; zero-candidate run; finishing twice 409; a negative count writes nothing |

**Guard proven by failing.** With `populate_existing` removed from the upsert, the full sourcing
suite still passed: the session's identity map holds objects weakly, so tests that drop their
reference had nothing stale to get back. I added
`test_an_object_already_in_the_session_is_refreshed`, which keeps the first object. Without the
option it fails (`'Acme Logistics 1234567 LLC' == 'Acme Renamed LLC'`), and with it, it passes. The
probe was reverted.

## Validation results

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 70 files |
| `uv run mypy .` | pass, 69 source files |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | 218 passed, 76 skipped (the skips print the `docker run` command) |
| `TEST_DATABASE_URL=…/lpe_t4 uv run pytest` | **294 passed, 0 skipped**, 2 warnings (Starlette's `HTTP_422` deprecation, already on `main`) |
| `alembic downgrade 0003_seed_freight_and_fire` → `upgrade head` | round-trips clean |
| `alembic check` | no new upgrade operations |
| `alembic heads` | one head, `0004_sourcing` |
| Suppressions / `Any` / `if vertical ==` | none (structure guards green; `grep ignore` over the new files is empty) |

Baseline before T4: 234 passed. Net +60.

## Divergences from plan

**Extra repository test for the identity map**
- Planned: rely on the idempotency test to catch a missing `populate_existing`.
- Actual: added a dedicated test that holds the first object.
- Reason: the plan's assumption was wrong. The idempotency test passes without the option (see
  *Guard proven by failing*).
- Type: Plan assumption wrong

**Frozen-brief test checks `model_config`, not an assignment**
- Planned: assert that assigning to a frozen brief raises.
- Actual: assert `SourcingBrief.model_config["frozen"] is True`.
- Reason: assigning to a frozen field type-checks as an error, so the test would need a
  `# pyright: ignore`, and zero suppressions applies to tests too.
- Type: Other (ground rule)

No other divergences. Every planned file, method and constraint exists as specified.

## Decisions for review (each also in the plan's *Open Questions*)

None of these sit on the architecture doc's *Open questions* or in PRD §9. Each is the conservative
reading of an ambiguity in the ticket.

1. **"Unprovenanced" is stored as absent (`None`)**, not as a raw value without a citation. T1 made
   such values unrepresentable, and T6 says a field nobody can cite stays absent.
2. **The upsert merges (`existing || incoming`) rather than replacing.** A retry of an earlier stage
   cannot erase later enrichment within the same run. One consequence: no upsert can clear a field.
3. **The key is exactly `(run_id, registry_id)`**, as the ticket says. If a manifest ever declares two
   registries for one run, their ids could collide. T5 would then namespace them, or a later revision
   adds the source to the key.
4. **`sourcing_run.manifest_id` (FK, NOT NULL) is added beyond the ticket's column list.** Without it a
   run cannot explain its candidates, because T7's rule ids only mean something per manifest version.
   As a result, `start_run` refuses a vertical with no ACTIVE manifest.
5. **The brief's ICP band reuses `app.manifests.schemas.IcpBand`.** This is a cross-slice read of a
   type. Reconciling the brief's band with the manifest's is left to T5/T7.
6. **`geography` is a slug** (`dfw`), not an enum, so no metro is hard-coded.
7. **`counts` and `cost` are JSONB**, so each stage adds its count without a migration. The total cost
   is computed rather than stored, and cost is persisted at `finish_run` only.
8. **There are no routes and no CLI.** The ticket names none, and T10 is the first reader.
9. **`registry_id` is stored twice**, as the key column and as the cited field, and a CHECK keeps them
   equal.

## Challenges encountered

- **mypy vs a re-assigned statement.** `insert(...)` then `.on_conflict_do_update(...).returning(...)`
  has a different type at each step (`Insert` → `ReturningInsert[Candidate]`). Reusing one variable
  failed mypy while Pyright accepted it. I gave each step its own name.
- **Overlapping CHECKs.** The first unknown-status probe also tripped the finish-time CHECK. The test
  now sets `finished_at` alongside the bad status so only one constraint can fire.

## Skipped items

None from the plan. **Not done, by design:** the `CLAUDE.md` architecture-map update (moving
`sourcing/` from *Planned* to *Today*). T11 and T12 are editing in sibling worktrees and all three
would conflict on those lines, so the update should land after the merge, the way T2's and T3's did
(`9bbf26c`, `bbed27a`).

## Merge notes

- **`alembic/env.py`**: T11 appends to the same `_REGISTERED_MODELS` tuple. Expect a trivial conflict
  and keep both entries.
- **`0005_cadence`** sets `down_revision = "0004_sourcing"`. `test_migrations_have_exactly_one_head`
  fails if it does not.
- No `uv.lock` or `pyproject.toml` change.

## Recommendations

- **Plan skill**: when a plan relies on an ORM guard (`populate_existing`, `expire_on_commit`), name the
  test that fails without it. "The idempotency test will catch it" was a guess, and the guess was wrong.
- **CLAUDE.md**: once merged, the architecture map's *Today* block gains `app/sourcing/` and
  `0004 sourcing_run/candidate`.
- **Deferred finding #2** (`source_url` has no URL-shape validation) is still due before T5, now more
  pressing because this slice persists those URLs.
