# PR #36 review, round 2: verify by own-domain homepage and record Places misses (T6 follow-up)

**Verdict: mergeable. Fix R1 first if you can; it is small.** There are no Critical or High issues, and
validation passes. GitHub won't let the author approve their own PR, so this verdict is posted as a comment.

Round 1's M2, M3, L3, L4 and L5 are fixed correctly. M1 is fixed for the agreed seven-row table, but one common
DFW address shape still reproduces the round-1 failure (R1). M4, L1 and L2 stay deferred to #37, and the fix
commit did not make them worse.

The deep review was done by the `code-reviewer` agent in a clean context. R1 was then reproduced against the
code at `5a2bb9a`; R2 and R3 were checked by reading the code.

## Validation (head `5a2bb9a`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 211 files formatted |
| mypy strict | ✅ 214 files, no issues |
| pyright strict | ✅ 0 errors, 0 warnings |
| pytest (with the DB tier) | ✅ 1,724 passed, 4 skipped (the paid `live` evals, opt-in) |
| alembic upgrade + check | ✅ no drift |

## Round-1 fixes

| Finding | Status |
|---|---|
| M1 mail drop only at our unit | Fixed for the agreed table. Building + suite shape still wrong (R1) |
| M2 the owner's surname can't satisfy the tie | Fixed. Surname-only names (`RODRIGUEZ LLC`) fail closed off-site; on-site still ties |
| M3 ISP and country webmail | Fixed. `tx.rr.com` and `mail.yahoo.co.uk` are free mail; `notrr.com`, `starr.com` and `rr.com.evil.com` stay business domains |
| L3 `verify_website_only` on a no-op retry | Fixed, with a DB test |
| L4 a count key per `MissKind` | Fixed, pinned by a parametrized test |
| L5 refusal docstrings | Fixed |

## Issues

### Medium

**R1. A shared building number makes a neighbour's suite "our unit".**
`app/sourcing/sources/google_places.py:96` (`_UNIT`) and `:364` (`_our_unit`).
`_UNIT` treats `BLDG`/`BUILDING`/`FL`/`FLOOR` like door designators, and `_our_unit` passes on any shared
number. Reproduced:

- Census `2100 OLYMPIC DR BLDG 3 STE 300` → `_units` = `{3, 300}`.
- A UPS Store (`shipping_service`) at `2100 Olympic Dr Bldg 3 Ste 120, Arlington, TX 76011` → `{3, 120}`.
- The intersection is `{3}`, so `classify_miss` returns `mail_drop`. Routing drops a real tenant and the
  judgment node is told it is a mail drop. That is the round-1 M1 failure on a common office-park shape.

Fix: compare only door-level designators (STE, SUITE, UNIT, APT, ROOM, PMB, `#`) and treat BLDG/FL as locators.
Or key units by designator, so `BLDG 3` only matches `BLDG 3`. Add a `Bldg 3 Ste 300` vs `Bldg 3 Ste 120` row to
the unit table.

**R2. A unit after the first comma, or under an unlisted designator, reads as "the whole building".** *(moderate
confidence)*
`google_places.py:372` reads only up to the first comma, and `_UNIT` has no OFFICE, OFC, SPACE, SPC, LOT or DEPT.
- A result formatted `2100 Olympic Dr, Ste 120, Arlington, TX ...` would read as no unit, so a neighbour's
  mail drop counts against Ste 300. No captured Places sample in the repo shows this form, so it may not occur.
- `2100 Olympic Dr Office 120` would read as no unit for the same reason.

Fix: fail toward "not our unit" when the street line has an unparsed trailing token after the street. Or log
it to #37 next to `other_business`'s unit item and confirm the comma form against a live response during the
Level 4 rerun.

### Low

**R3. The owner tie still passes on two generic words.** `app/sourcing/owner/judgment.py:283`.
`ACME GROUP SERVICES LLC` with an off-site quote naming "Premier Group Services" ties on `GROUP` + `SERVICES`.
This is the matcher's known limit (`schemas.py`), accepted for Places where an address anchors it. The owner
gate has no address anchor. Suggest pinning it with a test and a line in `_tied`'s docstring, or requiring all
words for names of three words or fewer.

**R4. Test gaps.** No free-mail test for `rr.com.evil.com` or `notrr.com` (they behave correctly). No test for
the R1 or R2 shapes, a lowercase or `Ste.` unit, or a hyphenated surname in the owner tie.

## Done well

- Each round-1 fix came with a mutation check (undo the fix, watch its test fail). This is recorded in
  `_tasks/todo.md`.
- M2's surname is subtracted from the business *name*, not the quote. That keeps "Maria Rodriguez of
  Rodriguez Trucking" working, a case the round-1 review missed.
- The free-mail suffix walk is tight: every dotted suffix of two or more labels, so look-alike domains don't
  match.
- D13 holds. The unit read is in memory only, and only the place ID and our verdict are stored or logged.
- `COUNT_KEYS` covers every `places_{kind}` key, and routing's `routing_mail_drop` and
  `routing_unverified_address` reconcile.

## Recommendation

Merge after R1, which is a small fix with a one-row test. Put R2, R3 and R4 on #37. Confirm R2's comma form
during the Level 4 Places rerun, which still waits on the founder's go-ahead.
