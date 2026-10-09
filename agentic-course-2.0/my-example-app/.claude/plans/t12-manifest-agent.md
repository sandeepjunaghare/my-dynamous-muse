# Feature: T12 — Manifest-authoring agent (`lpe manifest propose "<brief>"`)

The following plan should be complete, but validate documentation and codebase patterns and task
sanity before implementing. Import `ProvenancedValue` / `RetrievalMethod` from `app.shared.provenance`,
`RunCost` / `BillableKind` from `app.core.cost`, and derive every exception from
`app.manifests.exceptions.ManifestError`.

## Feature Description

Adding a vertical becomes *reviewing a proposal*, not *writing a row*. `lpe manifest propose "<brief>"`
runs a Claude Agent SDK loop that researches a vertical on the open web — the authoritative registry,
the disqualifier rules, the qualifying signals, the ICP band, the vocabulary — and writes a **DRAFT**
`vertical_manifest` row through the existing `ManifestService.create_draft` path. Every field it writes
is a `ProvenancedValue` with `retrieval_method = llm_inference` and `source_url` = a page the agent
actually fetched during this run. A field it cannot cite that way is left **absent**. The per-source
terms-of-use question is *raised* (printed, with where to read the terms) but never answered;
`activate` stays the only place a decision is recorded.

This is the one place in the system where an agent drives control flow at run time (D3).

## User Story

As the founder adding vertical #2
I want to type a one-line brief and get back a cited draft manifest
So that adding a vertical costs a review on the CLI, not a day of research and a hand-written row (M9).

## Problem Statement

T2 made a vertical a row, but the row still has to be researched and written by hand (the seeds in
`0003` are `manual_research`). M9 — vertical #2 in under a founder-day — is an assertion until the
research step is a proposal a person reviews.

## Solution Statement

Two new modules in `app/manifests/` plus a CLI subcommand, no migration, no schema change:

- **`proposal.py`** (pure, no SDK import) — the agent's structured-output contract (`AgentProposal`
  and its `Proposed*` parts, each carrying an optional `Citation{url, quote}`), and
  `build_draft(proposal, reads) -> DraftProposal`: the citation gate. A proposed field survives only if
  its citation URL is one the agent fetched successfully in this run (`reads`); otherwise it is
  recorded as an `OmittedField` with the reason. Field-level shape errors (e.g. a predicate rule with
  no operator) are omissions too, not crashes. If a field `ManifestBody` *requires* (≥1 source,
  `icp_band`, `vocabulary`) does not survive, `build_draft` raises `ManifestProposalIncompleteError`
  naming what is missing — nothing is written.
- **`agent.py`** — the SDK boundary. Builds `ClaudeAgentOptions` (only `WebSearch` + `WebFetch`,
  `permission_mode="dontAsk"`, `setting_sources=[]`, `strict_mcp_config=True`, `output_format` =
  `AgentProposal`'s JSON schema, model/turns/budget from settings), streams messages from an injected
  `AgentRunner` (default: `claude_agent_sdk.query`), records every successful `WebFetch` as a
  `PageRead(url, read_at)` keyed by `tool_use_id`, validates `ResultMessage.structured_output`, and
  records cost on a `RunCost` (`BillableKind.anthropic_tokens`, `usd = total_cost_usd`). SDK errors and
  non-success results become `ManifestAgentError` so the CLI prints one line.
- **`prompts.py`** — the system prompt (cite only fetched pages; leave a field out rather than guess;
  never decide terms of use; vertical is a slug) and the user-prompt builder.
- **`cli.py`** — `propose BRIEF [--vertical SLUG]`: run the agent with no DB session open (a minutes-
  long loop must not hold a pooled connection), then `create_draft` in one short session, then print
  the draft with every citation (reusing `_render`), the omitted fields and why, the terms-of-use
  questions, the per-run cost, and the **manifest-quality note** (open question, not a gate).

## Out of Scope / Non-Goals

- **No manifest quality gate.** The open question stays open (user decision). `propose` prints a note
  telling the activator to check disqualifiers and sources; docs keep the question unchecked.
- **No migration, no `ManifestBody` change.** Terms questions are printed, not persisted; `show`
  already prints `UNDECIDED — blocks activation` per source, which is the persistent form.
- **No live model call in pytest.** The acceptance test replays a recorded (synthetic,
  schema-accurate, keyless) transcript.
- No HTTP route for propose (README: a `POST /manifests` would be a second door), no `--dry-run`,
  no retries of a failed agent run.

## Feature Metadata

**Feature Type**: New Capability · **Complexity**: Medium-High · **Systems**: `app/manifests/`,
`app/core/config.py`, `tests/test_structure.py` · **Dependency**: `claude-agent-sdk` (0.2.165).

## Related Work

**Implements**: T12 (`docs/tickets/local-prospect-engine.md`) · **Epic**: `docs/local-prospect-engine.architecture.md`
**Back-references**: `.claude/plans/t2-vertical-manifest.md` (lifecycle, `create_draft`, CLI conventions),
`.claude/plans/t1-scaffold-core-provenance.md` (`ProvenancedValue`, `RunCost`).

---

## CONTEXT REFERENCES

- `app/manifests/schemas.py` — `ManifestBody` (required: sources ≥1, `icp_band`, `vocabulary`; tuples
  only; frozen leaves), `DisqualifierRule._shape_matches_kind`, `SLUG_PATTERN`.
- `app/manifests/service.py:518` — `create_draft`, the only write path.
- `app/manifests/cli.py` — `register`/`dispatch`/`_with_session`/`_render`; extend, don't fork.
- `app/cli.py` — the one place errors become exit codes; `LocalProspectEngineError` → exit 1.
- `app/shared/provenance.py` — `RetrievalMethod.llm_inference`; `retrieved_at` must be tz-aware.
- `app/core/cost.py` — `RunCost.record(kind, count, usd)`; `BillableKind.anthropic_tokens`.
- `alembic/versions/0003_seed_freight_and_fire_manifests.py` — what a good freight manifest looks like.
- `tests/test_structure.py` — `test_agent_sdk_is_not_carried_unused` must be rewritten (the SDK is now
  used); zero-`Any` AST check; no `vertical ==` literal.
- `tests/manifests/conftest.py` — `cli_database`, commit-and-clean pattern for CLI DB tests.
- SDK (verified from the installed package, 0.2.165): `query(*, prompt, options, transport)` →
  `AsyncIterator[Message]`; `ClaudeAgentOptions(tools, allowed_tools, permission_mode, setting_sources,
  strict_mcp_config, system_prompt, model, max_turns, max_budget_usd, effort, output_format, env)`;
  `AssistantMessage.content: list[ContentBlock]`, `ToolUseBlock(id, name, input)`,
  `UserMessage.content`, `ToolResultBlock(tool_use_id, content, is_error)`,
  `ResultMessage(subtype, is_error, num_turns, total_cost_usd, structured_output, errors, ...)`.
- Model: `claude-opus-5-5` (claude-api skill: latest, most capable default), configurable.

### Patterns to follow

- Logging `manifests.agent.run_started|run_completed|run_failed`, `manifests.agent.page_read`,
  `manifests.proposal.field_omitted`, `manifests.proposal.draft_built`.
- Exceptions carry kwargs as attributes (`ManifestNotFoundError` pattern), status codes set.
- Generics via PEP 695 (`def _cited[T](...)`), as the slice already does.

---

## STEP-BY-STEP TASKS

1. **ADD dependency** `uv add claude-agent-sdk` (done before planning, to read the real API).
   VALIDATE `uv run python -c "import claude_agent_sdk"`.
2. **UPDATE `app/core/config.py`** — `anthropic_api_key: str | None = None`,
   `manifest_agent_model: str = "claude-opus-5-5"`, `manifest_agent_max_turns: int = 40`,
   `manifest_agent_max_budget_usd: float = 5.0`. `.env.example` documents them. GOTCHA: `extra="forbid"`
   means a user who puts `ANTHROPIC_API_KEY` in `.env` needs the field declared.
3. **UPDATE `app/manifests/exceptions.py`** — `ManifestAgentError` (502), `ManifestProposalIncompleteError`
   (422, `missing_fields`).
4. **CREATE `app/manifests/proposal.py`** — contract + `build_draft` + URL normalisation.
5. **CREATE `app/manifests/prompts.py`**.
6. **CREATE `app/manifests/agent.py`** — `AgentRunner` type, `build_options`, `run_agent`, `PageRead`,
   `AgentRun` result.
7. **UPDATE `app/manifests/cli.py`** — `propose` subcommand and its rendering.
8. **UPDATE `tests/test_structure.py`** — SDK may be imported only from the decided call sites.
9. **CREATE tests** `tests/manifests/test_proposal.py`, `test_agent.py`, fixtures
   `tests/manifests/fixtures/freight_proposal_transcript.json` + a replay loader; **UPDATE**
   `test_cli.py` (propose is now offered; propose writes a DRAFT — `requires_db`).
10. **UPDATE docs** — slice README (propose present; quality question still open), CLAUDE.md commands
    line, adding-a-vertical note. Do not tick the quality question.
VALIDATE after each: `uv run ruff check . && uv run mypy . && uv run pyright && uv run pytest -q`.

## TESTING STRATEGY

- **Acceptance (offline):** replay the freight transcript → the draft names `fmcsa` (FMCSA), contains a
  predicate exclusion on asset-based carriers, every field `llm_inference`, every `source_url` ∈ pages
  fetched, `terms == ()`, cost recorded.
- **Citation gate:** null citation → omitted; citation to a URL only *searched*, never fetched →
  omitted; citation to a fetch that errored → omitted; trailing slash / fragment differences still
  match; invalid rule shape → omitted, not crash; required field missing → `ManifestProposalIncompleteError`.
- **Agent boundary:** non-success result / `is_error` → `ManifestAgentError`; missing
  `structured_output` → error; SDK exception wrapped; options restrict tools to WebSearch/WebFetch,
  carry the model from settings, never load filesystem settings.
- **CLI (DB):** propose with an injected replay runner writes exactly one DRAFT, prints citations,
  the terms questions, and the quality note; never ACTIVE.
- **Structure:** SDK import confined; no `Any`; no suppressions.

## ACCEPTANCE CRITERIA

- [ ] `lpe manifest propose "<brief>"` exists and writes a DRAFT only, via `create_draft`.
- [ ] Every written field is cited (`llm_inference`, a fetched URL) or absent; no fabricated citation.
- [ ] Terms question raised, never answered; `terms == ()` on the draft.
- [ ] Freight replay names FMCSA and yields an asset-based-carrier exclusion.
- [ ] Per-run cost logged via `RunCost`.
- [ ] Quality review remains an open question; propose prints the reviewer note.
- [ ] ruff, mypy, pyright, pytest green with DB tests running; zero suppressions, no `Any`.

## OPEN QUESTIONS / ASSUMPTIONS (conservative readings — also in the PR's *Decisions for review*)

1. **"Cited" means fetched in this run.** A citation must point at a page the agent fetched with
   `WebFetch` and that fetch succeeded. Search-result URLs alone do not count (a snippet is not a read).
2. **A required field that cannot be cited aborts the proposal** rather than inventing a placeholder or
   relaxing `ManifestBody`. Nothing is written; the error names what is missing.
3. **`retrieved_at` = when our process observed that page's fetch result**, not run start.
4. **Terms questions are printed, not persisted** (no schema change). The agent may point at a terms
   URL; it may not summarise or judge it.
5. **Vertical slug:** proposed by the agent, overridable with `--vertical`; validated against
   `SLUG_PATTERN`.
6. **Budget:** `max_budget_usd` / `max_turns` from settings as a circuit breaker, mirroring D7's
   Places breaker; defaults are guesses to be re-set from the first real run's logged cost.
7. **Model:** `claude-opus-5-5` default, `MANIFEST_AGENT_MODEL` overrides.

## NOTES

Rejected: verifying the citation `quote` verbatim against page text — `WebFetch` returns a model-
processed extract, not the raw page, so a verbatim check would reject honest citations. The quote is
printed for the reviewer instead. Rejected: the Messages API tool runner — the architecture decided the
Agent SDK, and its built-in WebSearch/WebFetch are exactly the research tools needed.

## AMENDMENTS
