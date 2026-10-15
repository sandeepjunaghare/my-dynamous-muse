# PR #34 review: `resolve_owner` (stage 4) and the T7 node edits (T6 PR B)

**Verdict: fix M1 and M2 before merging.** There are no Critical or High issues, and validation passes.
GitHub won't let the author approve their own PR, so this verdict is posted as a comment. Each of the two
Mediums breaks a ground rule this PR is responsible for:
- M1: the Places evidence now in the judgment prompt can reach a stored row (D13).
- M2: a person's name can reach the log (AC 2).

Both are small fixes. M3 is a known residual risk in the plan and can be deferred.

The deep review was done by the `code-reviewer` agent in a clean context. M1, M2 and L1 were then checked
against the code at `dab0c4b`, and each failure scenario is reachable as described.

## Validation (head `dab0c4b`)

| Check | Result |
|---|---|
| ruff check / format | ✅ clean / 211 files formatted |
| mypy strict | ✅ 214 files, no issues |
| pyright strict | ✅ 0 errors, 0 warnings |
| pytest (DB tier, Postgres 16 on port 5434) | ✅ 1,585 passed, 4 skipped (all paid `live` evals) |
| alembic upgrade head / check | ✅ no new operations; head `0010_promotion` |

The 5 deviations documented in `.claude/reports/t6-enrichment-pr-b-report.md` are intentional and are not
flagged here.

## Issues

### Medium

**M1. Places listing text can reach `disqualification.evidence` (D13).**
- Where: `app/qualification/prompts.py:87-94` now puts the Places `display_name`, `business_status` and `types`
  into the judgment prompt. A fired rule's free-text reason is stored verbatim as evidence:
  `app/qualification/service.py:160-171` writes `evidence=rule.reason.value`. Cited signal answers are stored
  the same way.
- Failure: the model writes `reason: "Maps lists it as 'Napco Bag & Film', a packaging company"` and cites a
  page it fetched. The gate checks only the citation URL, so a Places name is stored. Only the prompt's "never
  cite" instruction stands in the way.
- Test gap: the E2E D13 scan (`tests/sourcing/test_end_to_end.py:236-237`) covers `candidate.fields` only, not
  `disqualification` rows.
- Fix: in admission, refuse or redact any reason or answer containing `places.display_name`
  (case-insensitive). Extend the E2E scan to the `disqualification` table.

**M2. A person's name can be logged through the "no name offered" reason.**
- Where: `app/sourcing/owner/judgment.py:232-234` builds `f"no name offered: {why}"` from the model's free
  text, and `_omit` (`:222-224`) logs it as `reason=`.
- Failure: `name: null, reason: "Found Bryan Griggs on a directory but could not fetch it"` writes the name to
  the log. A rejected name is the most likely one to show up in a reason, so this is the case the rule
  exists for.
- The report does not list this as a deviation. The plan asks for the reason to be kept, but its AC 2 says
  never to log a name. The test at `tests/sourcing/owner/test_judgment.py:176-182` checks only the agent-title
  path.
- Fix: log a fixed reason (`"no name offered"`). Keep the free text only on the returned
  `AdmittedOwner.omitted` if it's wanted, and add a log-capture test for this path.

**M3. Nothing ties the cited page to *this* business (residual risk, can be deferred).**
- Where: `app/sourcing/owner/judgment.py:244-265`.
- Failure: the cited page is about a different company with a similar name, and its quote says "Dana Reyes,
  President". The surname check passes, so the owner is stored and cited to that page.
- This matches the plan's "directories are leads" residual, so it is not a divergence. It is the one way
  another business's person can still get through.
- Fix later: require a core token of the legal name or DBA in the quote, or check the quote against a hash
  of the fetched text (T7 deferred #23). File it on the follow-up ticket.

### Low

- **L1. The surname check is a substring match** (`judgment.py:252`, `_last_token` `:217-219`).
  - "Nicole Cole" passes on a quote with only "Nicole"; "Lee" matches "Leeds".
  - "Bryan Griggs Jr." reduces to `jr`, which wrongly drops a valid owner but fails closed.
  - Fix: use a word-boundary regex and skip suffixes (`jr`, `sr`, `ii`, `iii`).
- **L2. The agent check reads only the title** (`judgment.py:254-256`).
  - "Registered Agent: Bryan Griggs" in the quote, labelled "Owner" by the model, is admitted.
  - Fix: also match `AGENT_TITLES` against the quote, and add "agent for service of process".
- **L3. The stored owner name keeps trailing punctuation.**
  - `"Bryan Griggs,"` is kept, because `Principal` strips whitespace only. T9 would then write the last name
    `Griggs,` to HubSpot.
  - Fix: strip trailing `,;:` before building the `Principal`.
- **L4. A bad optional `headcount` discards a paid call** (`app/qualification/schemas.py:108-113`).
  - `low` is required, so `{"low": null}` fails the whole `JudgmentAnswer`, along with rules and signals
    already paid for.
  - Fix: a lenient `mode="before"` validator that turns bad input into `None`.
- **L5. `resolve_owner` pays for candidates the judgment never screened** (`app/tools/resolve_owner.py:59-76`).
  - A `judgment_failed` or `judgment_capped` candidate has no disqualification row, so it is still sent to
    the owner node.
  - Fix: skip candidates that weren't judged, or document this as accepted.
- **L6. Test gaps:**
  - `owner_write_refused` (`resolve_owner.py:101-110`) is untested.
  - No test covers both caps at 1 in one run, which should end `degraded` with two reasons. The plan lists
    it as an edge case.

## Done well

- The owner node copies T7's control flow, isolation and SDK options exactly. It imports nothing from
  `qualification` and accepts no registry evidence.
- Fail-closed reads hold on every path traced: an unread page, a 403, a cross-host redirect and a host
  mismatch are all refused. The eight A10 rules run in plan order, with the Maps check on the read's URL.
- Every write carries `source_url=read.url`, `retrieved_at=read.read_at` and `llm_inference`. The website is
  written only if empty, and `resolve_owner` replaces only an owner it wrote itself, both checked against the
  repository's ownership rules.
- Cost is recorded on every exit, with `usd=None` when there was no result. Both caps count and continue
  instead of raising. A retry pays nothing (`owner_kept`).
- The website admission requires a root path and rebuilds the origin, so the quote never leaks into the value.
- The five-stage E2E test runs the real runner over mocked sources and checks Places and Anthropic cost
  separately. The 9 mutation checks show the tests actually catch these failures.

## Recommendation

**Fix M1 and M2** in this PR. Each takes a few lines plus a test. Then re-run `/piv-validate`.
**Defer M3** to the website-verification follow-up ticket. Fixing L1–L3 is cheap and worth doing alongside
M2, since they're in the same function. L4–L6 can be fixed or logged at your discretion.
