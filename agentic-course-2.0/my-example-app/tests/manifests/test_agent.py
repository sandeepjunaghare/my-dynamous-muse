"""The agent boundary, run entirely offline against a recorded transcript.

Includes T12's acceptance test: the freight proposal, replayed from fixtures, names FMCSA and
produces an asset-based-carrier exclusion. No test here calls a model.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError, Message

from app.core.config import Settings, get_settings
from app.core.cost import BillableKind, RunCost
from app.manifests import agent as agent_module
from app.manifests.agent import RESEARCH_TOOLS, build_options, run_agent
from app.manifests.exceptions import ManifestAgentError
from app.manifests.proposal import build_draft
from app.manifests.schemas import RuleKind
from app.shared.provenance import RetrievalMethod
from tests.manifests.replay import Replay, load_transcript, with_result

OBSERVED_AT = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)


def _clock() -> datetime:
    return OBSERVED_AT


class TestFreightAcceptance:
    async def test_the_freight_proposal_names_fmcsa_and_excludes_asset_based_carriers(
        self,
    ) -> None:
        """T12's acceptance criterion, end to end through the agent and the citation gate."""
        cost = RunCost()
        run = await run_agent(
            "freight brokerages, DFW",
            cost=cost,
            runner=Replay(load_transcript()),
            clock=_clock,
        )
        draft = build_draft(run.proposal, run.reads)

        assert "fmcsa" in draft.body.source_names()
        assert "FMCSA" in draft.body.sources[0].value.description
        asset_rules = [
            rule.value
            for rule in draft.body.disqualifier_rules
            if rule.value.id == "asset_based_carrier"
        ]
        assert len(asset_rules) == 1
        assert asset_rules[0].kind is RuleKind.predicate

        assert draft.body.terms == ()
        methods = {cited.retrieval_method for cited in draft.body.sources} | {
            cited.retrieval_method for cited in draft.body.disqualifier_rules
        }
        assert methods == {RetrievalMethod.llm_inference}

    async def test_only_successful_fetches_count_as_reads(self) -> None:
        run = await run_agent("x", cost=RunCost(), runner=Replay(load_transcript()), clock=_clock)
        assert [read.url for read in run.reads] == [
            "https://www.fmcsa.dot.gov/registration/get-mc-number-authority-operate",
            "https://ai.fmcsa.dot.gov/SMS/Tools/Downloads.aspx",
            "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx",
            "https://www.bls.gov/ooh/office-and-administrative-support/cargo-and-freight-agents.htm",
        ]  # the WebSearch result URLs and the 403'd fetch are absent
        assert all(read.read_at == OBSERVED_AT for read in run.reads)

    async def test_the_run_cost_is_recorded(self) -> None:
        cost = RunCost()
        run = await run_agent("x", cost=cost, runner=Replay(load_transcript()), clock=_clock)

        assert run.cost_usd == Decimal("0.8421")
        assert cost.total_usd(BillableKind.anthropic_tokens) == Decimal("0.8421")
        assert cost.total_calls(BillableKind.anthropic_tokens) == 9

    async def test_the_brief_and_vertical_reach_the_prompt(self) -> None:
        replay = Replay(load_transcript())
        await run_agent(
            "collision centers, DFW", cost=RunCost(), vertical="collision", runner=replay
        )
        assert "collision centers, DFW" in replay.prompts[0]
        assert '"collision"' in replay.prompts[0]

    async def test_the_production_runner_goes_through_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No runner given -> the SDK's `query`; replacing it on the module keeps tests offline."""
        replay = Replay(load_transcript())
        monkeypatch.setattr(agent_module, "query", replay.as_query())
        run = await run_agent("x", cost=RunCost())
        assert run.proposal.vertical == "freight"
        assert len(replay.options) == 1


class TestFailedRuns:
    async def test_an_error_result_writes_nothing_but_still_costs(self) -> None:
        cost = RunCost()
        messages = with_result(None, subtype="error_max_budget_usd", is_error=True)
        with pytest.raises(ManifestAgentError) as exc_info:
            await run_agent("x", cost=cost, runner=Replay(messages))

        assert exc_info.value.reason == "error_max_budget_usd"
        assert cost.total_usd() == Decimal("0.8421")  # a failed run still spent money

    async def test_a_result_without_structured_output_is_refused(self) -> None:
        with pytest.raises(ManifestAgentError) as exc_info:
            await run_agent("x", cost=RunCost(), runner=Replay(with_result(None)))
        assert exc_info.value.reason == "no_structured_output"

    async def test_a_proposal_off_contract_is_refused(self) -> None:
        with pytest.raises(ManifestAgentError) as exc_info:
            await run_agent(
                "x",
                cost=RunCost(),
                runner=Replay(with_result({"vertical": "freight"})),
            )
        assert exc_info.value.reason == "invalid_structured_output"

    async def test_a_stream_with_no_result_is_refused(self) -> None:
        with pytest.raises(ManifestAgentError) as exc_info:
            await run_agent("x", cost=RunCost(), runner=Replay(load_transcript()[:-1]))
        assert exc_info.value.reason == "no_result"

    async def test_an_sdk_failure_becomes_a_domain_error(self) -> None:
        async def broken(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
            raise CLINotFoundError("claude CLI not found")
            yield  # an async generator, like `query` — the raise happens on first iteration

        with pytest.raises(ManifestAgentError) as exc_info:
            await run_agent("x", cost=RunCost(), runner=broken)
        assert exc_info.value.reason == "sdk_error"


class TestOptions:
    def test_the_agent_can_only_research_the_web(self) -> None:
        options = build_options(get_settings())
        assert options.tools == list(RESEARCH_TOOLS) == ["WebSearch", "WebFetch"]
        assert options.allowed_tools == ["WebSearch", "WebFetch"]
        assert options.permission_mode == "dontAsk"

    def test_no_filesystem_settings_or_mcp_servers_are_inherited(self) -> None:
        """Otherwise the research agent would load this repository's own `.claude/` AI layer."""
        options = build_options(get_settings())
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.mcp_servers == {}

    def test_model_and_breakers_come_from_settings(self) -> None:
        settings = get_settings().model_copy(
            update={
                "manifest_agent_model": "claude-sonnet-5-5",
                "manifest_agent_max_turns": 7,
                "manifest_agent_max_budget_usd": Decimal("1.25"),
            }
        )
        options = build_options(settings)
        assert options.model == "claude-sonnet-5-5"
        assert options.max_turns == 7
        assert options.max_budget_usd == 1.25

    def test_the_default_model_is_the_latest_opus(self) -> None:
        assert Settings.model_fields["manifest_agent_model"].default == "claude-opus-5-5"

    def test_the_output_format_is_the_proposal_contract(self) -> None:
        output_format = build_options(get_settings()).output_format
        assert output_format is not None
        assert output_format["type"] == "json_schema"
        assert output_format["schema"]["title"] == "ManifestProposal"

    def test_the_api_key_is_passed_only_when_configured(self) -> None:
        settings = get_settings()
        assert build_options(settings.model_copy(update={"anthropic_api_key": None})).env == {}
        keyed = build_options(settings.model_copy(update={"anthropic_api_key": "sk-test"}))
        assert keyed.env == {"ANTHROPIC_API_KEY": "sk-test"}
