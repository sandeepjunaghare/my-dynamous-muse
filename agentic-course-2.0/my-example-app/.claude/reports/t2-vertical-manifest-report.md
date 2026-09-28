# Implementation Report — T2 Vertical manifest slice (the M9 lever)

**Plan**: `.claude/plans/t2-vertical-manifest.md`  **Branch**: `feat/t2-vertical-manifest`  **Status**: COMPLETE

## Summary

`vertical_manifest` exists: identity and lifecycle in columns, cited content in one JSONB `body` whose
shape `ManifestBody` owns, with every researched field wrapped in `ProvenancedValue[T]`. A manifest is
born DRAFT and only a human moves it to ACTIVE on the CLI, where the per-source terms-of-use decision
is recorded — and a source with no recorded decision cannot go active, enforced by the service (good
error) and by a partial unique index (still true when a future slice writes by a path nobody
anticipated). Freight and fire are both seeded, fully cited, and **both DRAFT**; fire cannot be
activated while Google Places' terms remain an open question. An AST guard fails the build on any
`if vertical == "..."` in `app/`.

## Tasks completed

| Task | File | |
|---|---|---|
| Manifest schema (enums, frozen leaves, `ManifestBody`, `undecided_sources`) | `app/manifests/schemas.py` | CREATE |
| `RetrievalMethod.manual_research` | `app/shared/provenance.py` | UPDATE |
| `VerticalManifest` typed declarative model | `app/manifests/models.py` | CREATE |
| Table + partial unique index, hand-written | `alembic/versions/0002_vertical_manifest.py` | CREATE |
| Model registration for autogenerate | `alembic/env.py` | UPDATE |
| Repository (queries, flush, never commit) | `app/manifests/repository.py` | CREATE |
| Slice exceptions | `app/manifests/exceptions.py` | CREATE |
| Service: terms gate, supersede, transaction boundary | `app/manifests/service.py` | CREATE |
| Read-only routes | `app/manifests/routes.py` | CREATE |
| Router mount + docstring correction | `app/main.py` | UPDATE |
| `lpe manifest list \| show \| activate` | `app/manifests/cli.py`, `app/cli.py` | CREATE |
| `[project.scripts] lpe` | `pyproject.toml` | UPDATE |
| Freight + fire seeds, both DRAFT | `alembic/versions/0003_seed_freight_and_fire_manifests.py` | CREATE |
| `TEST_DATABASE_URL`, `requires_db`, migrated/rolled-back DB fixtures | `tests/conftest.py` | UPDATE |
| No-`if vertical ==` AST guard | `tests/test_structure.py` | UPDATE |
| Slice README (lifecycle, conventions, what is absent) | `app/manifests/README.md` | CREATE |
| Architecture map, commands, test-DB note | `CLAUDE.md` | UPDATE |

## Tests added

`tests/manifests/` — 6 modules plus shared builders and CLI fixtures:

| Module | Covers |
|---|---|
| `test_schemas.py` (22, no DB) | citation mandatory; `list`/non-frozen model rejected inside a citation; slug, band, rule-kind and tz validation; duplicate sources/rules/signals; orphan and doubled terms; `undecided_sources` incl. a *rejected* decision still blocking; `model_dump(mode="json")` → `model_validate` round-trip with `retrieved_at` still tz-aware |
| `test_repository.py` (10) | versions 1→2, old row retained, `(vertical, version)` uniqueness; **a DRAFT is never returned by `get_active`**; **two ACTIVE rows for one vertical raise `IntegrityError`**; ordering, paging; JSONB round-trip |
| `test_service.py` (18) | **activation refused while a source is undecided, naming it**; unknown source refused; happy path records one decision per source; not-draft and unknown-id refusals; supersede leaves exactly one active; version not bumped; **cited fields identical before and after activation**; `parse_accept_terms` (7 cases, no DB) |
| `test_routes.py` (8) | list shape + filters, `limit` cap 422, active 200, structured 404 through the centralized handler, non-UUID id → 422 |
| `test_cli.py` (8) | `show` prints all six citations and `UNDECIDED`; missing flag exits 2; `propose` absent from `--help`; refusal exits 1 with `error:` and no traceback; `activate` flips the committed row |
| `test_seeds.py` (7) | both verticals seeded at v1, **both DRAFT**, neither active; freight names `fmcsa` with the asset-based exclusion; every seeded field cites `manual_research`; fire names `tx_fire_marshal` + `places`, rollup rule is `judgment`; **fire cannot be activated with `tx_fire_marshal` alone** |

`tests/manifests/builders.py` (shared body/vertical builders) and `tests/manifests/conftest.py`
(committed-draft fixtures) are new support files — the CLI opens its own engine in its own loop, so it
cannot see anything written inside the rolled-back `db_session`.

**The structural guard was proven by making it fail**: a probe with `if vertical == "freight"` and a
`match vertical:` tripped it at both lines, while `vertical == other` (two variables) correctly did
not. Probe removed.

## Validation results

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 45 files |
| `uv run mypy .` | pass, 45 source files, zero suppressions |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | **124 passed, 41 skipped** — skips name the exact `docker run` |
| `TEST_DATABASE_URL=… uv run pytest` | **165 passed**, 2 warnings (see *Issues*) |
| `alembic upgrade head` / `downgrade base` / `upgrade head` | round-trips clean |
| `alembic check` | no new upgrade operations |
| `alembic heads` | one head (`0003_seed_freight_and_fire`) |
| Manual CLI walk | `list` → both DRAFT · `show` → every citation + `UNDECIDED` · `activate fire --accept-terms tx_fire_marshal` → **refused**, exit 1 · `activate freight --accept-terms fmcsa` → ACTIVE, citations unchanged (still dated 2026-09-26) |
| Manual API walk | `GET /manifests` → both rows · `GET /manifests/active/freight` → 404 while only drafts exist · `GET /health` → 200 |

Test database: throwaway Postgres 16 in Docker.

```bash
docker run --rm -d --name lpe-test-pg-t2 -p 5434:5432 -e POSTGRES_PASSWORD=test postgres:16
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5434/postgres
```

## Deviations from the plan

1. **The partial unique index is declared in the model as well as the migration.** The plan said model
   `__table_args__` must not carry it, *and* that `alembic check` should report no pending changes —
   those contradict. Measured: with the index only in the migration, `alembic check` reports
   `Detected removed index 'uq_vertical_manifest_one_active_per_vertical'`, so the next ticket running
   `--autogenerate` would silently emit a `drop_index` for the invariant this slice exists to
   guarantee. Declared in both, `alembic check` is clean and stable (no drop/recreate churn). The
   migration is still hand-written, because autogenerate omits `postgresql_where` when *creating* one.
2. **`register(parser)` rather than `register(subparsers)`.** Annotating a subparsers action means
   naming `argparse._SubParsersAction`, which Pyright strict flags as private usage — and no
   suppression is allowed. `app/cli.py` creates the `manifest` parser and hands the slice a public
   `argparse.ArgumentParser`; the CLI surface is identical.
3. **The CLI sends its logs to stderr** (`app/cli.py::_configure_logging`). Not in the plan. The
   service's stdout sink is right for a service, but a CLI's stdout *is* the review surface, and JSON
   log lines interleaved into `manifest show` corrupt exactly the output the command exists to
   produce. The sink resolves `sys.stderr` per call with `cache_logger_on_first_use=False`, because a
   captured stream object goes stale between tests. Processors are untouched.
4. **`activate(..., licenses: Mapping[str, str] | None = None)`.** The plan says the licence is
   "carried from the source's known licence when the caller supplies one" — nothing on `ManifestSource`
   holds one, so the caller passes a mapping. **The CLI has no `--license` flag**, so a CLI activation
   records `license=None`; FMCSA's CC PDM 1.0 therefore is *not* captured on the row today. Flagging
   rather than inventing a flag the plan did not specify — say the word and it is a two-line addition.
5. **Test database on port 5434**, not the plan's 5433, which is already allocated on this machine. The
   skip message and `CLAUDE.md` still document 5433 as the default.
6. **`tests/core/test_database.py::test_base_carries_metadata_for_alembic` was rewritten.** It asserted
   `Base.metadata.tables == {}` — a T1 snapshot that any slice adding a table breaks, the same shape
   Wave-2 prep inverted in B1. It now asserts what it was really guarding: that a registered slice
   table reaches the metadata `env.py` compares against. Not on the plan's file-update list.
7. **Two support test files added** beyond the plan's list — `tests/manifests/builders.py` and
   `tests/manifests/conftest.py` (reason in *Tests added*).
8. **Duplicate *signal* ids are rejected too.** The plan named rule ids only; a duplicated signal id is
   the same integrity bug and would make the second one unaddressable by T7. One line in the same
   validator.
9. **`parse_accept_terms` lives in `service.py`**, not the CLI, so its seven edge cases (trailing
   commas, whitespace, duplicates, empties, `None`) test without a database.

Plan assumptions 5, 6 and 7 were taken as specified and are now real: `superseded` is a third status,
`RetrievalMethod.manual_research` exists, and `lpe manifest list` exists (without it there is no way to
discover the id `show` needs).

## Issues encountered

- **Two Starlette deprecation warnings**: `HTTP_422_UNPROCESSABLE_ENTITY` in `app/main.py` (T1 code),
  surfaced now because T2 is the first slice to produce a 422. Pre-existing and out of this ticket's
  scope — it lands naturally with deferred review finding #4, which already queues a change to that
  handler before T10.
- **Deferred finding #2 is still open and is due before T5**: `source_url` has no URL-shape validation,
  so `"n/a"` would pass the gate. The seeds all use real `https://` URLs and `test_seeds.py` asserts
  the prefix, but the schema itself still permits it.
- **The dev Supabase password (B2) is still unrotated**, so nothing here was run against hosted dev.
  Every gate above ran against the throwaway container, which is the working rule already recorded in
  `_tasks/todo.md`.
- **The slice's open question is untouched, deliberately**: *who reviews manifest quality beyond terms
  of use?* `activate` catches the legal question; nothing catches a plausible-but-wrong disqualifier or
  a mis-chosen authoritative source. It bites at T12, not here. If a `reviewed_note` recorded at
  activation is wanted, it is a schema change and cheaper now than after two manifests exist.

## Merge surface against T3

`app/main.py` (both mount a router — adjacent lines), `CLAUDE.md` (both edit the architecture map;
kept to three edits), `tests/test_structure.py` (a new test appended, nothing restructured),
`app/shared/provenance.py` (one enum member — if T3 adds one too, keep both), `uv.lock` (changed only
by `[project.scripts]`; regenerate on conflict, never hand-merge). T3 adds no migration, so the chain
does not overlap.
