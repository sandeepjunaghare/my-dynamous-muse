# Code Review — PR #6: T2 vertical manifest slice

**Reviewed**: `feat/t2-vertical-manifest` → `main` · 31 files, +4001/−13
**Method**: fresh-eyes pass by the `code-reviewer` agent in a clean context (not the context that wrote the
code), plus a full validation run and two findings verified empirically against a live Postgres 16.

**Recommendation: approve with two Medium follow-ups.** No Critical, no High. Validation is green on every
gate. The two Medium findings are both worth fixing *before* T4 and T11 inherit this table shape, but neither
blocks the claims this PR makes.

## Validation

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 45 files |
| `uv run mypy .` | pass, 45 source files |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | 124 passed, 41 skipped — skips print the command that un-skips them |
| `TEST_DATABASE_URL=… uv run pytest` | **165 passed**, 2 warnings |
| `alembic check` | no pending operations |
| `alembic heads` | one head (`0003_seed_freight_and_fire`) |
| Suppression scan | **zero** `# type: ignore` / `# pyright: ignore` / `# noqa` in `app/`, `tests/`, `alembic/` |

The 2 warnings are Starlette's `HTTP_422_UNPROCESSABLE_ENTITY` deprecation in T1's `app/main.py`, already
tracked as deferred finding #4.

## What the review verified, rather than took on trust

Each load-bearing claim was traced through the code, not read off the PR body:

- **The terms gate has exactly one door.** `ManifestService.activate` is the only writer of `status="active"`
  — traced through the CLI, the routes, the repository and both migrations. Both seeds ship DRAFT.
- **Supersede-then-activate is correctly ordered inside one transaction.** Postgres checks a non-deferrable
  unique index immediately, so the order matters, and the repository flushes while the service commits once.
- **Citations really are carried through activation**, not rebuilt: `activate()` reuses the very
  `ProvenancedValue` objects from the loaded body, and the test proves it through a real JSONB round trip.
- **Transaction discipline holds everywhere** — no `commit()` in the repository, none missing in the service.
- **Handlers carry no `try/except`**, so slice exceptions render through the centralized handler as intended.
- **Tests prove their claims rather than restate the implementation** — e.g. the two-ACTIVE-rows test writes
  through the repository twice on purpose, to prove the database catches what a forgetful service would not.

## Findings

### 1 · Medium — `status` has no CHECK constraint, so the one-ACTIVE invariant can be walked past

`app/manifests/models.py:37` · `alembic/versions/0002_vertical_manifest.py:37`

The column is a bare `String(16)`. Nothing in the database restricts it to `{draft, active, superseded}`, and
the partial unique index keys on the literal `status = 'active'`.

**Verified against a live database, not reasoned about.** Two rows for one vertical, both `status = 'Active'`,
insert cleanly:

```
INSERT 0 2
    vertical    | version | status
----------------+---------+--------
 probe_vertical |       1 | Active
 probe_vertical |       2 | Active
```

The partial index never sees them, and every reader filtering `status == 'active'` treats them as not active —
so the row is simultaneously invisible to the application and exempt from the invariant. This undercuts the
exact argument the plan used for putting the invariant in the database at all: *still true when a future ticket
writes to the table through a path nobody anticipated.* That argument covers uniqueness among rows that spell
`active` correctly, and nothing else.

**Fix**: `sa.CheckConstraint("status in ('draft','active','superseded')", name="ck_vertical_manifest_status")`
in `0002`, and the matching constraint in `models.py` — the same both-places pattern the partial index already
uses, for the same `alembic check` reason.

**Why now**: T4 and T11 both add tables and will copy this table's shape. Cheap today; a data migration once
real rows exist across dev and prod.

### 2 · Medium — a losing activation race surfaces as a raw traceback, breaking the CLI's stated contract

`app/manifests/service.py:187-194` · `app/cli.py:73-80`

The plan names "two activations racing for one vertical" as an edge case the partial index must refuse. The
index does refuse it — but nothing translates the resulting `IntegrityError` into a domain error. It is not a
`LocalProspectEngineError`, and `app/cli.py` catches only that, so the loser gets a Python traceback on stderr.
The CLI's own docstring states the contract unconditionally: *"`1` a deliberate failure, printed as one line,
**never a traceback**."*

**One correction to the agent's finding**: it also reported this returning a generic 500 from the API. That is
not reachable — the routes are read-only and `activate()` has exactly one caller, the CLI. Verified:

```
$ grep -rn '\.activate(' app/ | grep -v 'def activate'
app/manifests/cli.py:134:        return await ManifestService(session).activate(...)
```

So the blast radius is the CLI alone, and only under genuine concurrency — two people, or two shells, activating
the same vertical within the same instant. On a single-user internal tool that is remote. The contract, however,
is written without a qualifier.

**Also a test gap**: `test_two_active_rows_for_one_vertical_are_refused_by_the_database` proves the constraint
with two sequential writes in *one* session. That is not the same as two sessions racing through
`ManifestService.activate()`, and it does not exercise the caller-facing failure mode the plan's edge-case list
actually asked about.

**Fix (two sizes, pick one)**:
- *Full*: catch `IntegrityError` around the commit in `activate()` and re-raise an `ActivationConflictError`
  (409) naming the vertical; add a test running two concurrent activations against separate sessions.
- *Minimal*: catch `IntegrityError` in `app/cli.py` alongside `LocalProspectEngineError`, so the traceback
  contract holds even if the domain error is judged not worth modelling.

### Low / advisory

- **argparse `Namespace` access is annotated but not verified.** `args.manifest_command`, `args.vertical` and
  friends are annotated as concrete types and pass both checkers, but typeshed types `Namespace.__getattr__` as
  returning `Any` — so these are declarations, not narrowings the checker proved. Sound today; worth knowing if
  the CLI surface grows.
- **Defence-in-depth on the repository** (`assert body.undecided_sources() == ()` in `mark_active`) was raised
  and is deliberately *not* recommended: the plan puts the rule in the service on purpose, and a duplicated
  check in two layers is how the two drift apart.

## Documented deviations — checked, not flagged

All seven deviations in `.claude/reports/t2-vertical-manifest-report.md` were read as intentional decisions and
none is counted as an issue. The partial index being declared in the model as well as the migration (deviation
1) was independently re-verified: `alembic check` is clean with it, and reports the index as *removed* without
it, which would put a `drop_index` into the next autogenerated migration.

No **undocumented** divergences from the plan were found.

## Recommendation

**Approve, with finding 1 fixed before merge and finding 2 decided explicitly.** Finding 1 is four lines and
gets materially harder after T4/T11 copy the shape. Finding 2 is a judgement call the author should make
consciously rather than inherit: model the conflict, or take the minimal CLI catch, or log it as deferred the
way `source_url` validation already is — any of the three is defensible, silently leaving it is not.

Posted as a comment rather than a formal approval: GitHub does not allow approving your own pull request, and
the human merge decision is the real gate here regardless.
