---
name: piv-validate
description: Runs the Local Prospect Engine's full validation suite — ruff lint and format, mypy and pyright strict, pytest (with the database tier when a throwaway Postgres is configured) and an alembic drift check — then reports one PASS/FAIL verdict. Use before committing, before opening a PR, after fixing review findings, or after finishing a chunk of work. This is CLAUDE.md's "Done" bar.
---

# Validate

Run every check this project has and report a single verdict. **Done = ruff, mypy, pyright and pytest all
green** (`CLAUDE.md`) — this skill is that bar, run the same way every time.

## Before you run anything

- **Working directory: the app root** — the folder holding `pyproject.toml` and `CLAUDE.md`
  (`agentic-course-2.0/my-example-app/`, or the same path inside a worktree). Git's toplevel is the parent
  `my-dynamous-muse` repo; running from there finds no config and reports nonsense. Check first:
  `test -f pyproject.toml && grep -q 'name = "local-prospect-engine"' pyproject.toml`.
- **Sandbox:** every `uv` command needs the sandbox disabled — `uv` trips on its cache before the command
  starts, and the failure looks like a tool error, not a permission error.
- **The database tier.** Database-backed tests carry `requires_db` and **skip** when `TEST_DATABASE_URL` is
  unset — so a run without it is green while proving nothing about repositories, services or migrations.
  Read the variable and say which tier you are running:
  - **Set** → full run. Refuse to continue if it points at hosted Supabase (`supabase.co` or `pooler.supabase`
    in the URL): it must be a throwaway database, because the suite migrates and writes to it.
  - **Unset** → offline run. Allowed, but the verdict says so (see §6). To get the full tier:
    `docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16` then
    `export TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:5433/postgres`
    (pick another port if 5433 is taken).

Run the checks in order. **Keep going after a failure** so the report covers everything, and capture the
output of any command that fails.

## 1. Lint and format

```bash
uv run ruff check .
uv run ruff format --check .
```

**Expected:** `All checks passed!` and `N files already formatted`. Lint scope is `app/`, `tests/` and
`alembic/env.py`; `pyproject.toml` excludes the AI layer, docs and generated migrations on purpose.

## 2. Types — both checkers, strict

```bash
uv run mypy .
uv run pyright
```

**Expected:** `Success: no issues found` and `0 errors, 0 warnings, 0 informations`. Both are required —
they disagree often enough that one passing proves little. **Zero suppressions** is a ground rule, enforced
by `tests/test_structure.py`, so a `# type: ignore` added to get here turns step 3 red.

## 3. Tests

```bash
uv run pytest
```

**Expected:** all pass. Note the counts from the last line — `passed`, `skipped`, and `warnings`:

- Offline, the skips are the `requires_db` tests; a skip count that grows with every ticket is normal.
- With `TEST_DATABASE_URL` set, **skipped should be 0**. Any skip on a full run is a finding — something
  is not running that the run claims to cover.
- The suite also carries the structure guards — single migration head, no suppressions, no `Any`, no
  `if vertical ==`, decided-out packages absent. Their failures read as decisions being broken, not bugs.

## 4. Migration drift — full run only

Only with `TEST_DATABASE_URL` set. `alembic` reads `DATABASE_URL`, and **`.env` points it at hosted
Supabase** — so override it explicitly for these commands, never rely on the environment:

```bash
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" uv run alembic check
```

**Expected:** `No new upgrade operations detected.` — the models and the migrations describe the same
schema. A diff here means a model changed without a migration, or a migration was hand-edited out of step.

Skip this step on an offline run and say so in the report.

## 5. Optional — live smoke

Only when the change touches startup, middleware or routing; the suite already exercises the app
in-process through `httpx.ASGITransport`. Start it in a second shell, then:

```bash
uv run uvicorn app.main:app --port 8000      # second shell
curl -fsS localhost:8000/health              # expect {"status":"ok",...}
```

Never run anything that writes to HubSpot or the hosted Supabase as part of validation.

## 6. Report

One line per check, then the verdict:

```
✅ ruff check            All checks passed
✅ ruff format           100 files already formatted
✅ mypy                  no issues, 99 files
✅ pyright               0 errors
✅ pytest                538 passed, 0 skipped
✅ alembic check         no drift
Overall: PASS (full — database tier ran)
```

- The verdict names the tier: **`PASS (full — database tier ran)`** or **`PASS (offline — N database tests
  skipped)`**. An offline PASS is not the "Done" bar for a ticket that touches a repository, a service or a
  migration; say that in one line when it applies.
- For every ❌, include the failing command and the relevant output.
- **Report only — do not fix anything here.** Fixing is a separate step (`piv-fix-review-findings`, or the
  implementation loop).

## Notes

- A checker that cannot fail is worthless. When this skill changes, break something on purpose (an unused
  import, a wrong return type) and confirm it reports ❌ — then revert.
- If a step gets slow, fix the slow step; don't drop it from the checker.
