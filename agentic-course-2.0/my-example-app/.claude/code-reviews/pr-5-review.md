# Code Review — PR #5: HubSpot gateway, dedupe and write-gate

**Branch:** `feat/t3-hubspot-gateway` → `main` · 34 files, +3757/−6 · commit `a383af5`
**Reviewed by:** `code-reviewer` agent in a clean context (not the session that wrote the code), findings
then re-verified against the source.
**Recommendation: REQUEST CHANGES** — one Critical, in the retry policy.

## Validation

| Check | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | 39 files already formatted |
| `uv run mypy .` | pass — 37 source files, zero suppressions |
| `uv run pyright` (strict) | **0 errors**, 0 warnings |
| `uv run pytest` | **154 passed** (91 pre-existing + 63 new) |
| PR mergeability | `MERGEABLE`, `CLEAN` |
| GitHub CI | **none — this repo has no workflows.** The local suite plus this review is the only gate. |

A green suite is why the Critical below matters: it is a path no test covers, so nothing here caught it.

---

## AGENT FIXES

**1. [CRITICAL] `app/promotion/client.py:342` — the 423 branch retries a create, breaking the slice's
central invariant.**

```python
if status == _LOCKED_STATUS and attempt == 1:   # no retry_on_server_error check
```

Every other retry branch gates on `retry_on_server_error` (`:263` transport, `:347` 5xx). 429 (`:310`)
is the one deliberate exception and the docstring justifies it — *"the request never executed"*. 423
has no such justification anywhere in the code, the plan, or the report.

`create_company`, `create_contact` and `create_task` all pass `retry_on_server_error=False`. A 423 on
any of them — HubSpot locks records during bulk imports and merges, so this is a real condition — sleeps
2 s and resends the identical POST. That is the exact failure `client.py:234` calls the worst bug
available in this slice, reached through a different door. And `dedupe.py`'s own docstring says the
search index lags writes, so a duplicate created this way **cannot be caught within the same run** —
it persists until a human finds it.

*Fix:* gate the branch on `retry_on_server_error`, as the 5xx branch is. If 423 is genuinely
"never processed" on HubSpot's side, that claim needs a comment and a citation, not silence.

*Test gap, same item:* `test_a_locked_response_is_retried_once` (`tests/promotion/test_client.py:272`)
only exercises a **read** (`PROPERTY_GET`). The 5xx and timeout cases each have an explicit
"create is never retried" test; 423 has none. Add the mirror test — it is what would have caught this.

**2. [MEDIUM] `app/promotion/client.py:475,482` — write bodies skip the project's own serialization rule.**

`_write_object` calls bare `body.model_dump()`, while `:406`, `:434`, `:457` and `:545` all pass
`by_alias=True, exclude_none=True`. `schemas.py:11` states the rule and the reason: *"sending `null` for
a property is not the same as omitting it — HubSpot reads the former as 'clear this field'."*

Harmless today — `ObjectWriteRequest` holds one required, unaliased field, so the output is byte-identical.
It becomes a data-loss bug the moment anyone adds an optional or aliased field to that model. One-line,
zero-risk fix now.

**3. [LOW] `app/promotion/client.py:342` — the `attempt == 1` cap is a second undocumented departure**
from the shared `_MAX_ATTEMPTS` budget used at `:310` and `:347`. Bounded, so harmless on its own. Fold
into the fix for item 1: either use `_MAX_ATTEMPTS` or say in one line why 423 gets exactly one retry.

---

## HUMAN DECIDES

**1. Should a 423 on a *create* be retried at all?** `file: app/promotion/client.py:342` · The plan
(`.claude/plans/t3-hubspot-gateway.md`, retry table) says *"423 Locked — once, after ≥2 s"* without
distinguishing creates from reads, so the code followed the plan and the plan is what is underspecified.
The safe reading — never retry a create, because we cannot prove the write did not land — is the one the
rest of the module already takes. Confirm that is what you want; the agent fix assumes it.

**2. The live-portal provisioning run, still not done.** `file: .claude/plans/t3-hubspot-gateway.md`
(Level 4) · It writes five custom properties to portal 244766495, and **property internal names are
permanent in HubSpot** — no rename, no clean undo. AC2's "a second run no-ops" is currently proven by
test only. Needs your explicit go-ahead, and it is a one-way door.

---

## HUMAN READS

**1. `app/promotion/client.py:131-163` — `to_property_payload`, the write-gate.** Every prospect field
that ever reaches the CRM passes through these 30 lines. The reviewer verified it collects all offenders,
sends nothing on refusal, and stays pinned to `is_promotable`. Worth your eyes anyway: it is the
structural fix for E18 and the thing T9 and T11 both inherit.

**2. `app/promotion/client.py:226-370` — the `_request` retry loop.** Load-bearing, and the one place a
defect was found. Read the branch ordering as a whole rather than the diff.

---

## HUMAN TESTS

**1. The second provisioning pass against the real portal** (after the decision above): expect eight
`property_present` and two `group_present` events, and **zero** `property_created`. `file:
app/promotion/properties.py:132`

**2. Search pacing under real concurrency.** `file: tests/promotion/test_client.py:183` · The existing
test issues two sequential `await`s from one coroutine, so it would pass even with `_search_lock`
deleted — a single coroutine serializes itself. It proves the 200 ms floor, not the lock. T9's dedupe
loop over ~120 candidates is the first real concurrent caller; worth watching the first run.

---

## FYI

- **The search lock spans the whole request including retry backoff** (`app/promotion/client.py:401`).
  Correct and conservative — it can never exceed 5 req/s — but one retrying search blocks every other
  pending search behind it, so the README's "~25 seconds of pacing" for 120 candidates is a floor, not
  an estimate.
- **No CI in this repo.** `file: .github/workflows` (absent) · Nothing re-runs the suite on GitHub.

---

## What is genuinely good

- **The write-gate is enforced by type signature, not convention.** A caller holding a raw
  `dict[str, str]` does not compile. `test_the_gate_refuses_exactly_what_the_shared_predicate_refuses`
  pins it to `is_promotable` so the two cannot drift apart silently.
- **The retry tests assert exact call counts, not just "it raised."** For a retry loop that is the right
  level of rigour, and it is why the 429/5xx/timeout paths are trustworthy — the gap is precisely the one
  path that lacks such a test.
- **Secret hygiene was verified, not assumed.** The reviewer independently grepped the fixtures for
  `token`/`bearer`/`authorization`/`pat-` — clean — and confirmed no log call passes headers.
- **Property provisioning treats human edits as authoritative** (drift warns, never PATCHes) and treats a
  409 as success **by status code only**, never by matching an undocumented message string.
- **`normalize_phone` plus the client-side re-check** is a sound defence against a false dedupe silently
  dropping a real prospect — the docstring's reasoning and the implementation actually match.
- **The lifecycle mirrors `app/core/database.py` faithfully**, and `app/shared/provenance.py` is
  untouched, as the plan required.
- **Documented deviations were checked and are genuinely intentional** — the alias split, the `cast` over
  the invariant union, the status constants, the LinkedIn omission. None were counted as findings.

---

**Recommendation: REQUEST CHANGES.** One Critical (item 1) blocks merge; it is a small, well-understood
fix plus the test that should have existed. The two Medium/Low items are cheap and sit in the same
function. Everything else in the slice is in good shape.

Next: `piv-fix-review-findings` on this report, then re-run validation.
