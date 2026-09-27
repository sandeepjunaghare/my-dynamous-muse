# Code Review — PR #2: `feat: scaffold the Local Prospect Engine service (T1)`

**Reviewed**: `feat/local-prospect-engine-t1` → `main` · 44 files, +4,240 / −57
**Verdict**: **Request changes** — 1 High, 4 Medium, 3 Low, 0 Critical
**Method**: fresh-eyes pass by the `code-reviewer` agent over every changed file in full, against CLAUDE.md,
`.claude/references/`, the T1 acceptance criteria and the plan. The two most consequential findings (#1, #3)
were then reproduced independently before being written up here.

The ten deviations documented in `.claude/reports/t1-scaffold-core-provenance-report.md` were treated as
intentional decisions and are **not** re-litigated. Every finding below is either undocumented, or a case
where the documented reasoning turns out to be incomplete.

---

## Validation

No CI is configured on this repo (`.github/workflows` does not exist), so this local run is the only
mechanical gate on the PR.

| | Command | Result |
|---|---|---|
| Lint | `ruff check .` | **pass** |
| Format | `ruff format --check .` | **pass** — 25 files |
| Types | `mypy .` | **pass** — 24 files, 0 issues |
| Types | `pyright` | **pass** — 0 errors, 0 warnings |
| Tests | `pytest` | **pass** — 68 passed |

A green suite is not the finding here. Two of the issues below are things a green suite cannot see.

---

## Issues

### High

**1. `app/shared/provenance.py:60-72` — `frozen=True` is shallow, so a cited value can be edited after the gate approves it.**

`ConfigDict(frozen=True)` prevents reassigning `ProvenancedValue`'s own fields. It does not freeze a mutable
`T`. Reproduced:

```
is_promotable before : True
cited value          : first_name='Dana' last_name='Reyes'
p.value.first_name = 'SOMEONE ELSE'     # raises nothing
cited value AFTER    : first_name='SOMEONE ELSE' last_name='Reyes'
is_promotable after  : True
```

This matters more than a typical immutability nit, because it is **the exact failure mode the primitive
exists to prevent**. E18 is 14 of 34 contacts carrying a company name in the first-name field — a value
nobody could cite. Here the citation survives while the value it vouches for silently changes, and
`is_promotable()` still returns `True`. The module docstring's claim that the failure mode moves to "code
that does not type-check" does not hold for a structured `T`.

It is also not hypothetical: T6's `resolve_owner` produces exactly a structured owner object, and the PR's
own test file already constructs `ProvenancedValue[Owner]` with a non-frozen `Owner`
(`tests/shared/test_provenance.py:109-134`).

*Fix*: add a `model_validator(mode="after")` that rejects `list`/`dict`/`set` values and requires any nested
`BaseModel` to declare `frozen=True`; state the caller obligation in the class docstring; add a regression
test proving a non-frozen nested model is rejected. Scalar `T` is already protected and stays so.

### Medium

**2. `app/shared/provenance.py:65-66` — `source_url` is validated as non-blank but not as a URL.**
`"n/a"` or `"verified manually"` passes the gate, which weakens "every field carries the URL it came from"
(module docstring, line 3). The plan's own IMPORTS line named `HttpUrl`; choosing bare `str` is a divergence
that is not in the deviations list. *Fix*: validate URL shape, or record the choice as a deviation with its
reason.

**3. `tests/core/test_health.py:60-84` — the exception handlers' *wiring* is untested; only their bodies are.**
Both error tests call `handle_known_error` / `handle_unexpected_error` directly as plain functions, so they
would pass unchanged if the `@app.exception_handler` registrations were deleted from `app/main.py`.

Verified separately that the wiring is in fact correct — through the real middleware stack the generic
handler returns `{"error":"InternalServerError","detail":"an unexpected error occurred","code":"internal_error"}`
and does not leak the exception text. So this is a **test-coverage gap, not a live defect**.

There is a related trap for later tickets: the shared `client` fixture (`tests/conftest.py:29-36`) uses
`ASGITransport`'s default `raise_app_exceptions=True`, and Starlette re-raises after responding. Confirmed
that a route raising under that fixture propagates the exception into the test instead of returning a
response — so the first person in T3/T9/T11 to test an error path the natural way will hit a confusing
failure. *Fix*: add a fixture with `raise_app_exceptions=False` plus one genuine end-to-end error-path test.

**4. `app/main.py:83-97` — 422 responses carry no field-level detail, which the rubric requires.**
`backend-api-best-practices.md` asks for "field-level error details on invalid input". The handler logs
`exc.errors()` server-side but returns only `"request payload failed validation"`. Every future route
inherits this. The content is user-supplied input, not server state, so returning a sanitized
`loc`/`msg`/`type` is safe. Not in the deviations list.

**5. `app/core/database.py:30-31` — the engine/sessionmaker globals are never reset between tests.**
`get_settings` has an autouse `_clear_settings_cache` fixture; `_engine`/`_sessionmaker` have no equivalent.
Harmless in T1 (no test opens a connection), but from T4 a test that monkeypatches `DATABASE_URL` after an
earlier test built the singleton will silently reuse the stale engine. *Fix*: autouse fixture calling
`dispose_engine()`, mirroring the settings one.

### Low

**6. `app/core/cost.py:42-102` — `record()` and `check_cap()` are decoupled**, so nothing structurally
prevents spending without counting. Relies on caller discipline in T6/T12. Consider a combined
`guard_and_record(...)` before consumers copy the two-call pattern.

**7. `tests/test_structure.py:86-116` — the `Any` guard has a known hole**: it inspects `AnnAssign` and
function signatures, so it misses `cast(Any, ...)` and string/forward-ref annotations. Since deviation #10
deliberately declines mypy's `disallow_any_explicit`, this test is the only backstop. Either extend it to
flag `cast` or note the residual risk in a comment.

**8. `tests/conftest.py:10` — `os.environ.setdefault` won't override an exported `DATABASE_URL`.**
Harmless now; from T4 a developer with a real hosted URL exported would run tests against real
infrastructure. *Fix*: assign rather than `setdefault`, or isolate as `tests/core/test_config.py`'s `env`
fixture already does.

---

## What's genuinely good

- **`app/shared/provenance.py`** is well built apart from finding #1: all fields required, blank source
  rejected and stripped, naive datetimes rejected, UTC normalisation that preserves the instant (verified —
  no information is lost, only the displayed offset changes), `RetrievalMethod` as a `StrEnum` so a typo
  cannot pass, and `manual_hubspot_entry` deliberately kept for T13.
- **`RunCost.check_cap` boundary semantics are correct** — 499 recorded passes, 500 recorded raises. No
  off-by-one. Specifically checked because it was flagged as a risk.
- **No race in the lazy-singleton engine** — no `await` between the `is None` check and the assignment, so
  it is effectively atomic under asyncio's cooperative model.
- **`GET /health` is genuinely database-free**, and proven by a test that makes engine creation explode
  rather than by inspection.
- **Ground rules are enforced by tests, not comments** — zero suppressions and the deliberately-absent list
  are executable assertions in `tests/test_structure.py`. That pattern is worth keeping as the project grows.
- Docstrings tie decisions back to specific tickets and evidence, which made this review materially faster.

---

## Recommendation

**Request changes.** No critical blockers, and the scaffold is sound — the acceptance criteria are met as
tested. But finding #1 should close before nine tickets build on `provenance.py`, because it undercuts the
one guarantee the whole ticket exists to establish, and tightening it later is far more expensive than now.
Findings #3 and #5 are worth taking in the same pass: both are about test infrastructure that T3/T9/T11 will
inherit unchanged.

Findings #2 and #4 are quick. #6–#8 are fine to defer with a note.

Suggested next step: `piv-fix-review-findings` on this report, then re-run validation.
