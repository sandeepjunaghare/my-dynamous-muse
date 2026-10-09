# Implementation Report — T12 Manifest-authoring agent

**Plan**: `.claude/plans/t12-manifest-agent.md` · **Branch**: `feat/t12-manifest-agent` · **Status**: COMPLETE

## Summary

`lpe manifest propose "<brief>" [--vertical <slug>]` runs a Claude Agent SDK research loop (WebSearch +
WebFetch only) and writes a **DRAFT** `vertical_manifest` row through the existing `create_draft` path.
A citation gate (`proposal.build_draft`) keeps a proposed field only if it cites a page the agent
successfully fetched in this run, stored as `llm_inference`; everything else is left absent and listed
with its reason. Terms of use are raised per source and never answered. Every run prints a note that
manifest quality review is still an open question. No migration, no `ManifestBody` change.

## Meta

- Files added: `app/manifests/{agent,proposal,prompts}.py`, `tests/manifests/{replay,test_agent,test_proposal,test_cli_propose}.py`,
  `tests/manifests/fixtures/freight_proposal_transcript.json`, this report, the plan.
- Files modified: `app/manifests/{cli,exceptions,README.md}`, `app/core/config.py`, `.env.example`,
  `CLAUDE.md`, `tests/manifests/{conftest,test_cli}.py`, `tests/test_structure.py`, `pyproject.toml`, `uv.lock`.
- Lines changed (excluding `uv.lock`): +1964 −16.
- Dependency: `claude-agent-sdk` 0.2.165 (`uv add`, lockfile regenerated, not hand-edited).

## Validation results

- ruff check + format: ✓ clean (65 files)
- mypy `.` (strict): ✓ 64 source files, no issues
- pyright (strict): ✓ 0 errors, 0 warnings
- pytest with `TEST_DATABASE_URL` set: ✓ **278 passed, 0 skipped** (baseline 234)
- Zero suppressions: ✓ no `type: ignore` / `pyright: ignore` / `noqa` in `app/` or `tests/`; no `Any` in our annotations.
- Offline smoke: `SubprocessCLITransport._build_command()` built a valid CLI command from `build_options` —
  every option maps to a real flag (`--tools`, `--allowedTools`, `--permission-mode dontAsk`,
  `--setting-sources=`, `--strict-mcp-config`, `--json-schema`, `--max-budget-usd`, `--model claude-opus-5-5`).
- **Not run:** a live model call (by instruction — no API credits spent).

## What went well

- Reading the installed SDK first (`types.py`) gave exact field names; pyright strict needed no adapters
  beyond a `Callable` type alias for the runner, because the SDK ships `py.typed` dataclasses.
- `functools.partial` as the gate's value builder kept the per-field logic generic and typed.
- Replaying fixtures through the SDK's real dataclasses means the code under test sees exactly what
  `query()` yields; patching `app.manifests.agent.query` lets the CLI tests exercise the production path.

## Challenges

- mypy could not infer default-argument lambdas → replaced with `partial`.
- `dataclasses.replace(**dict[str, object])` fails mypy against the SDK's typed fields → explicit keyword helper.
- The house log-event regex allows one underscore in the last segment; `page_read_failed` failed
  `tests/core/test_logging.py` → renamed to `fetch_succeeded` / `fetch_failed`.
- I briefly introduced a `pyright: ignore` for a private test-helper import; removed by making the helper public in `replay.py`.

## Divergences from plan

**Plan said "replay loader"; added `test_cli_propose.py` as a separate file**
- Planned: extend `test_cli.py`. Actual: usage-error tests updated in `test_cli.py`; propose behaviour in its own file.
- Reason: smaller diff in a shared test file; clearer ownership. Type: Better approach found.

**`throwaway_vertical` / `load_committed_rows` fixtures added to `tests/manifests/conftest.py`**
- Not in plan; needed because `propose` creates rows under a vertical the test chooses. Type: Plan assumption incomplete.

## Skipped items

- `.claude/references/adding-a-vertical.md` left unchanged — it already describes `propose` correctly.
- CLAUDE.md's "Today — T1, T2 and T3 have shipped" sentence left as-is to avoid a guaranteed conflict
  with the parallel T4/T11 branches; integration should update it once.

## Decisions for review (conservative readings)

1. "Cited" = fetched with `WebFetch` in this run, fetch succeeded. Search snippets and remembered URLs do not count.
2. An uncitable **required** field (source, ICP band, vocabulary) aborts the proposal; nothing is written.
3. `retrieved_at` = when this process observed the fetch result.
4. Terms questions are printed, not persisted; the agent can point at a terms URL but has no field to judge it.
5. Vertical slug proposed by the agent, overridable with `--vertical`.
6. `max_turns=40`, `max_budget_usd=5.00` are circuit breakers (D7 stance), to be re-set from the first real run.
7. Default model `claude-opus-5-5`, `effort="high"`; `MANIFEST_AGENT_MODEL` overrides.
8. Quality review stays open: a printed note only, no gate; architecture doc's question left unchecked.

## Recommendations

- CLAUDE.md: note the event-name regex (exactly one underscore in `action_state`) next to the logging rule — it is enforced by a test but not stated.
- Plan skill: for SDK-boundary tickets, require reading the installed package's types before writing the plan (done here; worth making standard).
