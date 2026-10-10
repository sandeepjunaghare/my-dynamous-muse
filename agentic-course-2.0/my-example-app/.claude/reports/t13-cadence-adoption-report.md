# Implementation Report — T13: Adoption of the hand-worked prospects into the cadence

**Plan**: `.claude/plans/t13-cadence-adoption.md`   **Branch**: `feat/cadence-adoption`   **Status**: COMPLETE
(code, tests, docs). The plan's **Level 4 live run is human-gated and not done**.

## Summary
`lpe cadence adopt` puts the founders' hand-worked prospects into the T11 machine at the touch they reached. It
rebuilds each contact's position from contact-logged activity through the sync's own `plan_advance`. A TOML
roster the human writes after a dry run decides adopt or park. Along the way, `enrol` gained `anchor_ref`. The
hard-coded `None` would have made the first sync re-credit the note adoption anchored on. `enrol` also became
safe to retry: it looks for a task with its key before creating one, and maps an `IntegrityError` to
`AlreadyEnrolledError`, which closes pr-9 L1.

## Tasks completed
- `SearchOperator.neq` → `app/promotion/schemas.py` (UPDATE)
- `create(anchor_ref=)` + `create_parked` → `app/cadence/repository.py` (UPDATE)
- `enrol`: `anchor_ref`, look-before-create by key, `IntegrityError` on `uq_cadence_state_contact` → `AlreadyEnrolledError`; new `adopt_parked` → `app/cadence/service.py` (UPDATE)
- `RosterError`, `UnadoptableContactError` → `app/cadence/exceptions.py` (UPDATE)
- `RosterAction`, `HandTask`, `AdoptionPlan`, `CompanyOnlyTask`, `AdoptionReport` → `app/cadence/schemas.py` (UPDATE)
- Roster model + `load_roster`, `parse_position`, pure `reconstruct`, `Adopter` (discover / rehearse / apply under the lock) → `app/cadence/adoption.py` (CREATE)
- `lpe cadence adopt [--roster PATH] [--dry-run]` → `app/cadence/cli.py` (UPDATE)
- Exports → `app/cadence/__init__.py` (UPDATE)
- Fake portal: contacts, companies, hand tasks, task search, task→contact/company and contact→company associations → `tests/cadence/conftest.py` (UPDATE)
- Docs: T13 and T9 tickets, `hubspot-integration.md` *Adoption*, cadence README (*Adoption*, *Enrolment*, *Deferred*), `CLAUDE.md` (map and commands)

## Tests added
- `tests/cadence/test_adoption.py`, 31 pure tests:
  - `parse_position`: 3 accepted, 7 refused.
  - Roster: a valid file; 9 refusals (unknown key, duplicate contact, `start` on `park`, empty, no prospects, non-digit id, unknown action, impossible start, malformed TOML); entry numbering; a missing file.
  - `reconstruct`: no history; two calls → (1, email); three notes → (2, call); the anchor is the last activity; an activity before creation is ignored; an activity at exactly creation counts; a booked meeting is ignored; nine logged → finished; an override.
- `tests/cadence/test_adoption_service.py`, 19 DB-tier tests:
  - Zero-field adoption: no writes besides the task create, no `candidate`, no `touch_done`.
  - Two calls → email.
  - **The anchor regression.** A new note after adoption is the machine's to count.
  - A `start` override.
  - A park entry, then `enrol` refused; nine logged → parked.
  - A re-run is a no-op; an interrupted create is reused on the re-run.
  - A bad entry is isolated; a missing creation date is reported.
  - The lock (apply refuses while a sync runs, rehearse still runs).
  - Discover: a contact appears once, a company-only task is listed, an already-enrolled contact is flagged, a truncated search warns; rehearse writes nothing.
  - Hand tasks never written.
  - Company choice: two companies, roster choice, the only company.
- `tests/cadence/test_service.py`, 4 new: enrol with `anchor_ref` then sync closes nothing; interrupted enrol reuses the task; a concurrent enrol → `AlreadyEnrolledError`; `adopt_parked`.
- `tests/cadence/test_models.py`, 2 new: `anchor_ref` round-trips; `create_parked`.
- `tests/cadence/test_cli.py`, 4 new: no flags → exit 2; dry run prints evidence and writes nothing; a roster adopts and lists hand tasks to close; a malformed roster → one `error:` line.
- **Mutation check.** With `anchor_ref` dropped from `enrol`, 3 tests fail. With look-before-create disabled, 3 tests fail.

## Validation results
- ruff check: pass. ruff format: pass.
- mypy strict: 102 files, 0 issues. pyright strict: 0 errors.
- pytest with the database tier: **664 passed, 0 skipped**, up from 604 at baseline.
- `alembic check`: no new operations. No migration.

## Deviations from the plan
- **`AdoptionReport.adopted` / `parked` are `list[CadenceStateResponse]`, not lists of task ids.** The CLI prints contact, position, task and due date from them.
- **`AdoptionPlan.already_enrolled` was dropped.** An enrolled contact is skipped before any HubSpot read and reported in `AdoptionReport.already_enrolled`. Planning it would spend reads, and would report a deleted contact as a failure rather than as enrolled.
- **`AdoptionPlan.parked_position` was added.** It records where a parked row puts the contact: the reconstructed touch for a `park`, or (3, email) when all nine are done.
- **`UnadoptableContactError`** (codes `contact_not_found`, `contact_created_at_missing`) is the typed carrier for the plan's per-entry failure codes.
- **A lost create reports `hubspot_response_error`**, the gateway's real code for a 500, not the plan's `server_error`.
- **Roster contact ids may be unquoted TOML integers**; they are read as the same id.
- **Validation errors locate the entry as `prospect 2.start`.** A `›` separator tripped ruff RUF001.
- **"Sample-shaped tasks excluded" is not mechanical.** Discovery cannot tell a HubSpot sample task from a real one, so the human leaves samples out of the roster, as the plan's Level 4 says. Discovery does exclude our own keyed tasks, and the tests cover that.
- **The test database runs on port 5434, not 5433.** 5433 is held by an unrelated `langfuse-postgres` container, which was left untouched.

## Follow-up after the first live dry run (2026-10-10)
The live `adopt --dry-run` told the founder to "add a contact in HubSpot" for APS, Central, DSS and Koetter.
But those tasks sit on the company and duplicate the task on the contact named after it, and that contact is
already in the listing. Discovery now reads each company-only task's companies → contacts
(`CompanyOnlyTask.company_contacts`). The CLI then gives one of four messages:
- the company's contact is listed above: adopting it covers this, so close this task by hand
- the contact is already in the cadence: close this task by hand
- the contact has no open hand task: put it in the roster
- the company has no contact at all: add one in HubSpot

There are 2 new tests (DB tier and CLI), and the suite is at 666 passed.

## Issues encountered
- **Not all tests were seen failing first.** The repository, `enrol` and CLI tests were watched failing. `test_adoption.py` and `test_adoption_service.py` were written before running them, so they were never seen red. The mutation check above stands in for that.
- **No live-portal run.** Level 4 (dry run → roster → roster dry run → apply on approval → `sync --dry-run`) still needs the human.
