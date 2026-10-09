# Code Review — PR #8: T12 manifest-authoring agent (`lpe manifest propose`)

**Reviewed**: `feat/t12-manifest-agent` → `main` at `fd8dd49` · 21 files, +2,700/−16 (≈+1,960 excluding `uv.lock`)
**Method**: a fresh-eyes pass in a clean context plus an independent deep pass by the `code-reviewer` agent (both reached findings 1–3 separately), a full validation run, and the
SDK boundary checked against the **installed** `claude-agent-sdk` 0.2.165 and the CLI it bundles (2.1.294), not
against memory. The two load-bearing findings were reproduced offline by feeding CLI-shaped stream-json through
the SDK's real `query()` and message parser (a fake `Transport`; no model, no network, no database).

**Recommendation: request changes.** Validation is green and the design is right. But the PR's central
guarantee, *"every written field cites a page the agent actually fetched"*, does not hold against the CLI this
PR ships with. A 403, a 404 or a cross-host redirect counts as a successful read today, and a field citing it
is written with that URL as its provenance. The cost record also misses exactly the runs that hit a cap. Both
fixes are small, and both pass the current suite only because the replay fixture models SDK behaviour the real
CLI doesn't have.

## Validation

| Gate | Result |
|---|---|
| `uv run ruff check .` | pass |
| `uv run ruff format --check .` | pass, 65 files |
| `uv run mypy .` | pass, 64 source files |
| `uv run pyright` | 0 errors, 0 warnings |
| `uv run pytest` (no DB) | 232 passed, 46 skipped |
| `TEST_DATABASE_URL=…/lpe_t12 uv run pytest` | **278 passed**, 0 skipped (2 warnings: the known Starlette `HTTP_422` deprecation) |
| `alembic heads` | one head (`0003_seed_freight_and_fire`); no migration in this PR, as claimed |
| Suppression scan (`app/`, `tests/`, `alembic/`) | **zero** `type: ignore` / `pyright: ignore` / `noqa`; `Any` appears only in prose and in the structure test that forbids it |
| Live model call | not run, by instruction |

## What the review verified, rather than took on trust

- **The DRAFT-only guarantee holds.** `propose` reaches the database through exactly one call,
  `ManifestService.create_draft` (`app/manifests/cli.py:182-185`), and the repository hard-codes
  `status=draft` (`repository.py:107`). `activate` is never imported on that path. `ManifestBody` is built with
  explicit keyword arguments (`proposal.py:373-379`) and `terms` is never passed, so it is always `()`. The
  model can't inject `terms` either: `AgentProposal` has no such field and every value object is built through
  `partial(...)` with named fields only, so extra keys in the structured output are dropped before the gate.
- **The tool surface is what the PR says.** `--tools WebSearch,WebFetch` replaces the base tool set (no Bash,
  Read, Write, Edit, Agent or Skill), `--setting-sources=` (empty) stops user and project settings, hooks,
  plugins and CLAUDE.md from loading, and `--strict-mcp-config` with no `mcp_servers` gives no MCP. Verified in
  `subprocess_cli.py:591-730`.
- **Turn and budget caps reach the CLI** as `--max-turns 40` / `--max-budget-usd 5.0`, and the CLI ends the run
  with `error_max_turns` / `error_max_budget_usd`.
- **The gate itself (`build_draft`) is sound for the reads it is given**: null citation, an unread URL, a
  schema-refused shape and duplicates each become an `OmittedField` with a reason. A missing required field
  raises before anything is written. `IcpBand`'s min ≤ max validator surfaces as an omission, not a crash.

## Findings

### 1 · Critical — a failed fetch or a redirect notice counts as "successfully fetched", so fabricated provenance is written

`app/manifests/agent.py:121-132` · fixture `tests/manifests/fixtures/freight_proposal_transcript.json:46`

`_ReadTracker` treats a `WebFetch` result as a read unless `ToolResultBlock.is_error` is truthy. The bundled CLI
(2.1.294) does **not** throw on an HTTP error or a cross-host redirect. It *returns* them as ordinary tool
results, so `is_error` is never set:

- HTTP 4xx/5xx → `{data: {code: 403, result: "The server returned HTTP 403 Forbidden.\nThe response body was not retrieved. …"}}`
- cross-host redirect → `{data: {code: 301, result: "REDIRECT DETECTED: The URL redirects to a location that was not fetched automatically. …"}}`

Both are in the bundled binary's WebFetch implementation. The fixture models the 403 as
`"is_error": true, "content": "Request failed with status code 403"`, which this CLI doesn't produce. That is
why `test_only_successful_fetches_count_as_reads` and the "403 is dropped" case pass.

**Reproduced offline** through the SDK's real `query()` and parser. A WebFetch whose result is the CLI's HTTP-403
shape, followed by a proposal citing that URL for the source, ICP band and vocabulary, produces:

```
manifests.agent.fetch_succeeded url=https://registry.example.gov/licensees
manifests.proposal.draft_built cited_fields=3 omitted_fields=0
draft written with source_url: https://registry.example.gov/licensees | omitted: ()
```

So a hallucinated-but-plausible URL that 404s, a regulator page behind a 403, or `fmcsa.dot.gov` →
`www.fmcsa.dot.gov` all become `llm_inference` citations to pages that weren't read. That is E18 coming back
through the tool built to stop it. The reviewer can't tell this from a real read: `show` prints the same
`cited:` line. The README (`app/manifests/README.md:83`, *"a fetch that errored is not a read"*) and the
`agent.py:11-12` docstring state the opposite.

**Fix**: the SDK already carries the truth. `UserMessage.tool_use_result` is the tool's structured output and
includes `code` and `url`. Count a read only when **all** of these hold:

- `block.is_error` is falsy;
- `message.tool_use_result` is a dict with `200 <= code < 300`;
- the result text doesn't start with `REDIRECT DETECTED`.

If `tool_use_result` is absent, fail closed and treat the result as not a read. Also fail closed when one
`UserMessage` carries more than one `tool_result`, because the structured result can't be attributed then. The
real CLI sends one tool result per user message; the fixture packs three into one (lines 29-31, 45-47).
Correct the fixture to match. Add regression cases for a 403, a 404 and a redirect in the CLI's real shape.

### 2 · High — a run that hits the turn or budget cap records no cost and is reported as `sdk_error`

`app/manifests/agent.py:169-184`

When the CLI ends on an error result (`error_max_turns`, `error_max_budget_usd`,
`error_max_structured_output_retries`, an API failure), it yields the `ResultMessage` and **then exits
non-zero**. The SDK turns that exit into `ResultError` (a `ProcessError`, so a `ClaudeSDKError`), raised from
the iterator *after* the result (`claude_agent_sdk/_internal/query.py:510-545`). `run_agent` catches
`ClaudeSDKError` inside the loop and raises `ManifestAgentError(reason="sdk_error")`. The `_record_cost` call
on line 184 is never reached.

**Reproduced offline**: an `error_max_budget_usd` result with `total_cost_usd: 5.07`, followed by the
transport's exit-code `ProcessError`:

```
manifests.agent.run_failed  reason=sdk_error  error='Claude Code returned an error result: Reached maximum budget ($5) (exit code: 1)'
reason: sdk_error | cost recorded: 0
```

No `core.cost.call_recorded` is logged. The capped runs are the most expensive ones, and their spend is the
signal the PR says should set the caps (*"its logged cost should set the turn and budget caps"*). It also breaks
the module's own promise (*"It always logs what the run cost — on failure too"*) and the acceptance criterion
*"Per-run cost logged via RunCost"*. `test_an_error_result_writes_nothing_but_still_costs` and
`test_a_failed_agent_run_exits_one` pass only because `Replay` stops after the result instead of raising.

**Fix**: call `_record_cost` the moment the `ResultMessage` is seen, inside the loop. In the `except` branch, if
a result was already captured, raise with `reason=result.subtype` instead of `sdk_error`. A
`except ResultError` clause ahead of the generic one makes that explicit. Have the test replay raise
`ResultError` after an error result, the way the real stream does.

### 3 · Medium — the replay harness reproduces the SDK's types, not its behaviour

`tests/manifests/replay.py:81-116, 141-167`

The harness builds `AssistantMessage` / `UserMessage` / `ResultMessage` dataclasses by hand. That is honest
about *types*: `run_agent` and `build_draft` run for real, and nothing in the agent path is mocked. But it skips
the three behaviours findings 1 and 2 depend on:

- The SDK's message parser, which is what sets `tool_use_result`.
- The CLI's actual WebFetch error and redirect shapes.
- The `ResultError` raised after an error result.

A green acceptance test therefore says nothing about either defect. The PR body's line *"replays a recorded
transcript through the SDK's own dataclasses"* is accurate. *"The code under test sees exactly the objects
`query()` would yield"* (report, *What went well*) is not.

**Fix**: store fixtures as raw stream-json and drive them through the public `query(prompt=…, options=…,
transport=…)` with a small fake `Transport`. The `Transport` ABC is exported. The fake answers the
`initialize` control request and then streams the fixture, and optionally raises `ProcessError` at the end the
way `SubprocessCLITransport` does. About 30 lines, and the existing tests keep their shape. When the first live
`propose` runs, capture its stream and replace the synthetic fixture with that recording.

### 4 · Medium — one malformed citation URL crashes the CLI after the run is paid for

`app/manifests/proposal.py:188` (`normalize_url`), called from `admit` at line 216

`urlsplit` raises `ValueError` on some malformed URLs. `normalize_url` runs on every **model-supplied**
citation URL, and nothing catches the error. It isn't a `LocalProspectEngineError`, so `app/cli.py` lets it
through as a traceback. The whole proposal is lost, after the spend, because of one bad field. Reproduced:

```
>>> build_draft(proposal_whose_vocabulary_cites("http://[x"), reads)
ValueError Invalid IPv6 URL
```

That breaks the gate's own rule that a field it can't use becomes an omission, never a crash.

**Fix**: catch `ValueError` in `admit` and record an omission (`"citation URL is not parseable"`). In
`_Gate.__init__`, skip unparseable reads. Add a test case.

### Low / advisory

- **A paid run can be lost to the database write.** `vertical` is `String(64)` (`models.py:44`), but
  `SLUG_PATTERN` has no length bound (`cli.py:49-55`, `proposal.py:257-263`). An agent-proposed slug over 64
  characters raises `DataError`, which `app/cli.py` doesn't catch (only `LocalProspectEngineError` and
  `IntegrityError`), so the user gets a traceback after paying for the run. The same happens if
  `DATABASE_URL` is wrong. Fix: cap the slug at 64 in `_slug` and `build_draft`. Consider printing
  `_print_proposal_review` before the write, so a failed write still leaves the evidence on screen.
- **`anthropic_tokens` is recorded with `count=num_turns`** (`agent.py:137`). That is turns under a kind named
  tokens. It is harmless today because the CLI prints "turn(s)", but whoever builds per-run cost reporting will
  misread it. Use a `manifest_agent_turns` kind, or say so in the docstring.
- **A source's `base_url` isn't tied to its citation** (`proposal.py:272-283`). A fetched page can be cited for
  a source whose `base_url` is on another host, and that `base_url` is what T5 will call after activation. This
  sits inside the documented open quality question, so it isn't a defect. Printing a `base_url host ≠ cited
  host` warning in the review output would be a cheap aid for the activator.
- **Fetched text reaches the terminal raw.** Quotes and descriptions are printed unescaped
  (`cli.py:193-196`, `_render`). A hostile page could plant ANSI escape sequences. Low risk on a single-user CLI;
  stripping control characters before printing is one line.
- **An unknown cost prints as `$0`** (`cli.py:210`). When `total_cost_usd` is `None`, `RunCost.total_usd()`
  is `0`. Print "unknown" when `AgentRun.cost_usd is None`.
- **The circuit breakers have no bounds** (`app/core/config.py:55-56`). A typo of `0` or a negative value is
  accepted. Add `Field(gt=0)` to both.
- **The terms URL is unverified but reads as authoritative** (`cli.py:206`). It is the link a person follows to
  make a legal decision. Mark it "not fetched by the agent" unless it is among the run's reads.
- **The system prompt doesn't say fetched content is untrusted** (`prompts.py`). Add one line saying that
  instructions inside fetched pages are data and are never to be followed. It costs nothing and helps.
- **Some assertions are weak.** `test_agent.py:164` asserts that a default equals its own literal. The two
  omission tests at `test_proposal.py:153-163` and `184-195` don't assert the *reason*, so they would pass if a
  field were dropped for the wrong cause.
- **Some refusals aren't logged.** `build_draft` raises `ManifestProposalIncompleteError` at lines 258 and 365
  with no `manifests.proposal.draft_rejected` event. `agent.py:210` logs `invalid_structured_output` without
  `turns` or `usd`.
- **The structure guard covers `claude_agent_sdk` imports, not agent use** (`tests/test_structure.py:51-72`). A
  pipeline slice importing `app.manifests.agent.run_agent` would grow an agent loop without tripping it. If the
  intent is D1 ("the weekly run is not an agent loop"), add `app.manifests.agent` to the guarded module set,
  allowed only from `app/manifests/cli.py`.

## Focus areas

- **Citation integrity.**
  - Hallucinated URLs, search snippets and dropped fields: handled correctly.
  - Malformed citation URLs: crash the run (finding 4).
  - Failed fetches and redirects: **not handled** (finding 1).
  - Field injection: the model can't add fields past the gate. Everything is built from named fields, and
    `terms` is unreachable.
  - Inherent limit, not a defect: "fetched" doesn't mean "supports". A hostile page that was fetched can be
    cited for anything. That belongs to the open quality question.
- **The DRAFT-only guarantee.** Holds. There is no route from `propose` to ACTIVE or to a terms decision.
- **Prompt injection.** The worst a hostile page can do is steer the agent's searches and fetches and poison
  the proposal's content.
  - It can't write files, run shell commands, load skills or plugins, or reach MCP servers: the tools are
    `--tools WebSearch,WebFetch`, settings are off and there is no MCP config.
  - It *can* make the agent fetch arbitrary URLs. That is an exfiltration channel, but the only data in context
    is the brief and public web text.
  - WebFetch binary downloads are saved under the CLI's own tool-results directory, not to a path the agent
    chooses.
- **Cost and turn caps.**
  - Enforced: the flags reach the CLI. The budget is checked between turns, so the last turn can overshoot $5.
  - Cost is logged via `RunCost` on success. It is **not** logged on capped or failed runs (finding 2).
- **Replay honesty.** It exercises the real `run_agent` → `build_draft` → `create_draft` path. But its
  SDK-behaviour model is wrong in exactly the two places that matter (finding 3).
- **`CLAUDE.md` / `test_structure.py` edits.**
  - The `CLAUDE.md` edits are accurate. The map entry (line 26) and the commands entry (lines 106-107) match
    the code, and removing `manifests/agent` from *Planned* is right. The *Today: T1, T2 and T3* sentence was
    left alone on purpose, as the report documents.
  - The structure test is a fair successor. The old "SDK not a dependency" check had to go once the SDK became
    a dependency. The new check confines imports to `app/manifests/agent.py` and catches `import x as y` and
    submodule imports (see the Low note on agent use).

## Documented decisions — a view on each

- **The quote isn't stored.** Agree. WebFetch returns a secondary model's extract made with an agent-chosen
  prompt, so storing the quote would give a model paraphrase the weight of a verbatim record. Printing it for
  the reviewer is the honest level.
- **Terms questions are printed, not persisted.** Agree. `show` already renders `UNDECIDED — blocks activation`
  per source, and that is the persistent form. The one thing lost is the `terms_of_use_url` pointer after the
  terminal scrolls away. If it's wanted later, it belongs in `ManifestSource`, not in a terms record.
- **The 40-turn / $5 caps.** Reasonable as circuit breakers in the D7 stance. Re-setting them from the first run
  depends on finding 2: as written, a run that hits the cap leaves no cost record to re-set from.
- **Quality review left open with only a printed note.** Agree that this PR shouldn't answer it. The note is
  explicit and is printed on every run. Finding 1 matters more *because* quality review is open: the citations
  are the activator's main evidence, so they have to be real reads.
- **Others** are all reasonable and none is flagged:
  - Model `claude-opus-5-5` at `effort="high"` (a valid id and effort level in this SDK).
  - `retrieved_at` = when the result was observed.
  - An uncitable required field aborts the proposal.
  - The slug can be overridden.
  - `Decimal` budget in place of the plan's `float`.

No **undocumented** divergences from the plan were found, other than the overstated claims noted in findings 1
and 3.

## What's good

- **The architecture is right.** The pure `proposal.py` gate is separate from the SDK boundary in `agent.py`.
  The `AgentRunner` seam means production and tests share one path, and no DB session is held across a
  minutes-long loop.
- **The isolation options were chosen deliberately**, and the docstring explains why `setting_sources=[]` and
  `strict_mcp_config` matter as much as the tool list. Running inside this repo, the research agent would
  otherwise have inherited the project's own `.claude/` layer.
- **Failures fail closed and say why.** Omissions carry reasons, a missing required field writes nothing, and
  the CLI holds its one-line, no-traceback contract for every domain error.
- **Strict typing without escape hatches** at an SDK boundary: no `Any` and no suppressions, with
  `partial`-based builders instead of lambdas.
- **The system prompt names no vertical.** The "vertical is data" principle is applied to the prompt as well.
- **The PR body is candid** about what wasn't run (a live call) and lists its own judgement calls for review.

## Recommendation

**Request changes for findings 1, 2 and 4.** Each is a few lines in `_ReadTracker`, `run_agent` or `_Gate.admit`. Fix finding 3
alongside them, because it is the test change that would have caught both and will keep them caught. The Low
items can go to the deferred log. After the fixes, the first live `propose` should still be treated as the real
acceptance test: record its stream and make it the fixture.

Posted as a comment rather than a formal review: GitHub doesn't allow approving or requesting changes on your
own pull request, and the human merge decision is the real gate.

## Fixes applied

Applied on `feat/t12-manifest-agent` in commit `afb8ddd` (on top of `fd8dd49`). Posted to the PR as https://github.com/sandeepjunaghare/my-dynamous-muse/pull/8#issuecomment-6074025314. The reviewer's offline probes (`probe_403.py`, `probe_budget.py`) now show the 403 run writing nothing (`ManifestProposalIncompleteError`) and the capped run reporting `error_max_budget_usd` with $5.07 recorded.

Every fix has a test that fails on `fd8dd49` and passes now. The read and cost tests run through the **corrected replay**: raw CLI stream-json goes through the SDK's real `query()` and parser.

| # | Finding | Status | Proving test(s) |
|---|---|---|---|
| 1 | Critical: 403/404/cross-host redirect counted as a read | **Fixed.** A read needs `tool_use_result` with a 2xx `code` and the requested host in `url`, plus no `REDIRECT DETECTED` text. A missing, unrecognised or unattributable result fails closed. | `test_agent.py::TestWhatCountsAsARead` (403, 404, redirect, redirect text with 2xx, host mismatch, no structured result, packed results, `test_a_403_cannot_become_a_citation`) · `test_only_successful_fetches_count_as_reads` |
| 2 | High: capped run records no cost, reported as `sdk_error` | **Fixed.** Cost is recorded through `RunCost` as soon as the result arrives. The `ResultError` that follows is reported by its subtype, with the cap, turns and spend. | `test_agent.py::TestFailedRuns::test_a_capped_run_writes_nothing_but_still_costs[budget, turns]` · `test_an_api_failure_result_is_reported_with_its_errors` · `test_cli_propose.py::test_a_capped_run_exits_one_and_says_so[turns, budget]` |
| 3 | Medium: replay models types, not behaviour | **Fixed.** A fake `Transport` drives the public `query()` and raises `ProcessError` after an error result, as `SubprocessCLITransport` does. The fixture is now raw stream-json: one tool result per message, `tool_use_result` on each, and the 403 in the CLI's real shape. | The 3 existing tests it turned red before the app fixes (`test_only_successful_fetches_count_as_reads`, the error-result cost test, the CLI failed-run test) |
| 4 | Medium: malformed citation URL crashes after the run | **Fixed.** The field is omitted with `citation URL '…' is not parseable`, and an unparseable read is skipped. | `test_proposal.py::test_a_malformed_citation_url_is_omitted_not_fatal[2 cases]` · `test_an_unparseable_read_is_skipped_not_fatal` |

**Low, fixed (small and in scope):**
- Slug capped at 64 (`test_a_vertical_slug_too_long_for_its_column_is_refused`).
- Breakers `gt=0` (`test_a_breaker_that_could_never_trip_is_refused`).
- An unknown cost prints "unknown", not `$0` (`test_an_unreported_cost_is_shown_as_unknown_not_zero`, DB).
- The prompt now says fetched content is untrusted (`test_the_prompt_says_fetched_content_is_untrusted`).
- `draft_rejected` is logged, and `invalid_structured_output` is logged with turns and USD.
- The two omission tests now assert the reason.
- `count=num_turns` is documented on `_record_cost`.

**Low, deferred:**
- Printing the review before the DB write: it reorders the CLI output contract and needs its own change.
- Renaming the `anthropic_tokens` kind: it's a `core/` enum shared with T6, so this PR documents it instead.
- The `base_url` host ≠ cited host warning: this is a review aid inside the open quality question.
- Stripping ANSI from fetched text: it touches every print path, and the risk is low on a single-user CLI.
- Marking the terms URL as "not fetched": a review-output change, better done with the quality-review work.
- The structure guard on `app.manifests.agent` use: a test-policy change that belongs with T5, the first pipeline slice.
- `test_the_default_model_is_the_latest_opus`: it pins the default on purpose. Won't fix.

**Validation:**
- ruff check and format: clean.
- mypy: 64 files, no issues. pyright: 0 errors.
- pytest without the DB: 252 passed, 47 skipped. With the DB: 299 passed.
- No live model call.

**Before merge:** the fixture is still synthetic, and only the WebFetch shapes were checked against the bundled CLI 2.1.294. The first live `propose` run should be recorded and turned into the fixture.
