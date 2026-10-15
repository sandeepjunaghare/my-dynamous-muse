# Implementation Report — T9 Promotion slice

**Plan**: `.claude/plans/t9-promotion.md`   **Branch**: `feat/t9-promotion`   **Status**: COMPLETE (code); the
live-portal steps (Level 4) are the human's

## Summary
Promotion is built. `lpe promotion promote --run <id> [--dry-run]` takes a completed or degraded run, refuses
anything not qualified under OQ1-D or missing a required citation, dedupes against the ledger and the portal,
and creates a Company + Contact at `lpe_review_status = pending_review`, recording both ids in the `promotion`
ledger; an interrupted create is resumed on the next pass, never repeated. `lpe promotion sync [--dry-run]`
reads the founder's decision back and hands accepted contacts to `CadenceService.enrol`. Part 1 (gateway
extensions, names, the ledger and its repository, the routing exclusion) was built on 2026-10-12 ahead of T6 PR
A; part 2 (this pass) is everything that reads T6's fields.

## Tasks completed
Part 1 (2026-10-12, see the plan's AMENDMENTS): Tasks 1-6, 9, 10, 14.
Part 2 (2026-10-13):
- Task 7 → `app/promotion/eligibility.py` (CREATE)
- Task 8 → `app/promotion/mapping.py` (CREATE); 8b skipped (OQ5-A)
- Tasks 11-12 → `app/promotion/service.py` (CREATE); report models in `app/promotion/schemas.py` (UPDATE)
- Task 13 → `app/promotion/cli.py` (CREATE), `app/cli.py` (UPDATE)
- Task 15 → `tests/promotion/{builders,crm,scenario}.py`, `test_eligibility.py`, `test_mapping.py`,
  `test_promote.py`, `test_review_sync.py`, `test_cli.py` (CREATE); `tests/promotion/conftest.py` (UPDATE:
  `MockPortal.prepend/extend`, the fake-CRM fixtures)
- Task 16 → `app/promotion/README.md`, `app/promotion/__init__.py`, `app/promotion/exceptions.py`,
  `app/shared/provenance.py`, `.claude/references/hubspot-integration.md`, `CLAUDE.md`,
  `docs/tickets/local-prospect-engine.md`, `_tasks/todo.md` (UPDATE)

## Tests added
- `test_eligibility.py` (18): each reason, the reason order, every missing field named in declaration order,
  optional fields never refuse, **unscored allowed**, dead refused, middle/live allowed, the required set ⊆
  `CandidateFields` in declaration order.
- `test_mapping.py` (28): every property keeps its source field's citation; the address split into four;
  domain from the website host; no `None`; derived `lpe_source_url`/`lpe_sourced_at`; no mapped citation is
  Google Maps; the contact has no email or phone; per-type metadata; `record_url`.
- `test_promote.py` (21, DB tier): happy path (properties, association, route label, owner, URLs), ledger ids,
  properties provisioned before the first create, the score, ambiguous names; re-run promotes nothing then a
  new candidate still promotes (cross-run); refusals send nothing and leave no row; failed/running refused,
  degraded promoted, unknown run; domain and phone hits flagged with nothing written; two candidates sharing a
  phone under search lag → one create; contact failure leaves the company recorded; recovery of an ambiguous
  company create (deferred before 10 min, found by `lpe_source_url` after, one company), of an unambiguous
  company failure, of an ambiguous contact create (association walk, one contact), of a contact failure; a 403
  stops the pass; **a dry run's counts equal the real pass's**, with zero writes and zero rows.
- `test_review_sync.py` (11, DB tier, the real `CadenceService.enrol` against the same fake): accepted →
  one `Cadence 1/3 · call` task on contact and company, a live cadence row, `enrolled`; a second sync enrols
  nothing then a new acceptance does; a failed task create stays `enrolling` and the next sync finishes it; a
  lost task response is reused; a contact already in the cadence counts as enrolled; rejected (no writes),
  waiting, withdrawn (deleted company), unrecognised values (`maybe_later`, `ACCEPTED`); dry run = real.
- `test_cli.py` (14): whole lines for created / ambiguous / duplicates / failures / tally / dry run / sync
  decisions / orphan task; stderr for a stopped pass and unrecognised values; exit 0 / 1 / 2; an error is
  one line, no traceback.

## Validation results
| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean / 200 files formatted |
| `mypy .` (strict) | no issues, 203 files |
| `pyright` (strict) | 0 errors, 0 warnings |
| `pytest` (DB tier, `lpe-test-pg-t9` on 5437) | **1,503 passed, 2 skipped** (the paid live eval); was 1,411 |
| `alembic check` | no new upgrade operations; one head, `0010_promotion` |
| Mutation checks | 9 of 9 caught (below) |

Mutations, each applied alone and reverted: (a) promoting a failed run, (b) dropping the ledger dedupe,
(c) retrying a company create after a 500, (d) sending `None` for an absent website, (e) title-casing names,
(f) enrolling a rejected company, (g) skipping the recovery age guard, plus a dead score allowed and an
unscored candidate refused. All caught. (h), routing disqualified candidates, was checked in part 1. The first
form of (f) was **not** caught, because the `rejected` branch runs before `accepted` and the mutation could
never fire; it was rewritten so a rejected company really reaches the enrol path, and that is caught.

## Deviations from the plan
1. **`check_promotable` takes no `manifest_has_signals`** — the plan says to drop it under OQ1-D. So
   `promote_run` does not read the manifest at all; the vertical comes from the run.
2. **`dedupe.find_existing_company` is not given the ledger's job**: as planned, but the recovery path for a
   row with no company id runs ledger + portal dedupe again before creating, because the first pass may have
   stopped at a dedupe read (an auth failure, say) before any create.
3. **`ensure_properties` runs at the start of every real `promote`** (not in a dry run). Amendment 4 says the
   live `lpe_review_status` is created only through `lpe` at the first real promote; this is where. It is
   read-then-create, so later passes issue only reads.
4. **Counts**: `recovery_pending` added — a dry run does not resume interrupted rows, so it counts them
   instead of pretending. Every key is present every time (`PROMOTE_COUNTS`, `REVIEW_COUNTS`).
5. **Review sync works on `_Row` snapshots, not ORM rows.** `enrol` rolls the session back when it loses a
   race, which expires every loaded object, and an expired attribute in async SQLAlchemy raises. Not in the
   plan; found while writing the sync.
6. **`CompanyWrite` / `ContactWrite` carry two more fields** than the plan's sketch: the dedupe domain, and the
   owner citation URL that contact recovery matches on.
7. **CLI tests stub the service** rather than committing rows like the cadence CLI tests: the service is
   covered end to end in `test_promote` / `test_review_sync`, and the CLI's job is the lines and exit codes.
8. **From the T6 handover** (ticket T9): `email` is not written (the plan's "no contact email"); the headcount
   band is not mapped (the plan's follow-up). Both recorded in the ticket.

## Issues encountered
- The sandbox refuses writes into the worktree (outside the session's directory) and heredoc temp files; uv,
  git and file edits there ran with the sandbox disabled, or through the editor tools.
- One test asserted the first company create by `path.endswith("/companies")`, which also matched the
  property-create path; tightened to the objects path.

## Left for the human (Level 4)
1. `uv run lpe promotion promote --run 732c039e-1ad5-432b-8329-d1dce5922f03 --dry-run` — expect nothing
   promotable (no verified, undisqualified candidate in that run); a run with one is needed for a real create.
2. The first real `promote` creates `lpe_review_status` (one-way door) and the five T3 properties if absent.
3. Read-only: confirm association type `1` is the primary contact→company label in portal 244766495.
4. In HubSpot, accept one company and reject one; `lpe promotion sync --dry-run`, then `sync`.
5. Add `uv run lpe promotion sync` before `lpe cadence sync` in the 07:00 launchd plist.
