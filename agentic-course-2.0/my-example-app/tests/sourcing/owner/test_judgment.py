"""The ``resolve_owner`` node offline: replayed CLI streams through the real ``query``.

These prove the wiring and the gate (A10): a name survives only when a page the node verifiably
read names that person, and cost is recorded on every exit. They do **not** prove the model finds
the right person: the fixtures are synthetic. Yield and accuracy are ``test_live_enrichment.py``'s
job, on officer-less candidates a founder exports.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLINotFoundError, Message
from pydantic import JsonValue
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.core.cost import BillableKind, RunCost
from app.shared.page_reads import PageRead
from app.shared.provenance import RetrievalMethod
from app.sourcing.exceptions import OwnerJudgmentError
from app.sourcing.owner.judgment import OWNER_TOOLS, admit_owner, build_options, run_owner_judgment
from app.sourcing.owner.schemas import AdmittedOwner, OwnerAnswer
from tests.manifests.replay import (
    Replay,
    StreamLine,
    http_error_text,
    redirect_text,
    result_line,
    web_fetch_call,
    web_fetch_result,
)
from tests.sourcing.builders import CENSUS_URL, a_candidate

ABOUT = "https://www.acme.test/about"
READ_AT = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
INIT: StreamLine = {
    "type": "system",
    "subtype": "init",
    "session_id": "owner-replay",
    "model": "claude-opus-5-5",
    "tools": ["WebSearch", "WebFetch"],
    "mcp_servers": [],
    "permissionMode": "dontAsk",
    "cwd": "/app",
}


def _answer(
    name: str | None = "Bryan Griggs",
    *,
    title: str | None = "Founder",
    url: str | None = ABOUT,
    quote: str = "Bryan Griggs founded Acme in 2009.",
    reason: str = "",
) -> JsonValue:
    citation: JsonValue = None if url is None else {"url": url, "quote": quote}
    return {"name": name, "title": title, "reason": reason, "citation": citation}


def _fetched(
    url: str = ABOUT, *, code: int = 200, text: str = "About us.", reported_url: str | None = None
) -> list[StreamLine]:
    return [
        web_fetch_call("toolu_o1", url),
        web_fetch_result("toolu_o1", url, code=code, text=text, reported_url=reported_url),
    ]


async def _resolve(stream: list[StreamLine]) -> tuple[AdmittedOwner, RunCost]:
    cost = RunCost()
    run = await run_owner_judgment(
        a_candidate(), cost=cost, runner=Replay(stream), clock=lambda: READ_AT
    )
    return admit_owner(run.answer, reads=run.reads), cost


def _read(url: str = ABOUT) -> PageRead:
    return PageRead(url=url, read_at=READ_AT)


class TestAdmitted:
    async def test_a_person_named_on_a_fetched_page_is_kept(self) -> None:
        admitted, cost = await _resolve([INIT, *_fetched(), result_line(_answer())])

        assert admitted.omitted is None
        assert admitted.owner is not None
        assert admitted.owner.value.name == "Bryan Griggs"
        assert admitted.owner.value.title == "Founder"
        assert admitted.owner.source_url == ABOUT
        assert admitted.owner.retrieved_at == READ_AT
        assert admitted.owner.retrieval_method is RetrievalMethod.llm_inference
        assert cost.total_usd(BillableKind.anthropic_tokens) == Decimal("0.1")

    @pytest.mark.parametrize(
        ("name", "quote"),
        [
            ("Vincent Corpus", "Owner Vincent Corpus answers the phone."),
            ("Bryan Griggs", "bryan griggs, founder"),
            ("Bryan Griggs,", "Meet Bryan Griggs."),
            ("Cher", "Founded by Cher."),
            ("Bryan Griggs Jr.", "Bryan Griggs runs Acme."),
            ("Mary-Kate O'Neil", "Owner: Mary-Kate O'Neil"),
        ],
    )
    def test_the_name_check_is_word_and_case_tolerant(self, name: str, quote: str) -> None:
        """A designator inside a word is not one; case and trailing punctuation do not matter."""
        answer = OwnerAnswer.model_validate(_answer(name, quote=quote))
        assert admit_owner(answer, reads=[_read()]).owner is not None

    @pytest.mark.parametrize("name", ["Bryan Griggs,", "Bryan Griggs;", " Bryan Griggs: "])
    def test_trailing_list_punctuation_is_not_stored(self, name: str) -> None:
        """PR #34 review L3: T9 splits the stored name, so "Griggs," would reach HubSpot."""
        owner = admit_owner(OwnerAnswer.model_validate(_answer(name)), reads=[_read()]).owner
        assert owner is not None
        assert owner.value.name == "Bryan Griggs"

    def test_a_final_period_is_kept(self) -> None:
        answer = OwnerAnswer.model_validate(_answer("Bryan Griggs Jr.", quote="Bryan Griggs Jr."))
        owner = admit_owner(answer, reads=[_read()]).owner
        assert owner is not None
        assert owner.value.name == "Bryan Griggs Jr."


class TestOmitted:
    @pytest.mark.parametrize(
        ("answer", "reason"),
        [
            (_answer(None, reason="no page names an owner"), "no name offered"),
            (_answer("   "), "no name offered"),
            (_answer(url=None), "no citation offered"),
            (_answer(url="http://[::1"), "citation URL is not parseable"),
            (_answer(url="https://elsewhere.test/team"), "never successfully read"),
            (_answer("ACME LOGISTICS LLC"), "names a company or nothing"),
            (_answer(quote="Bryan founded Acme."), "the quote does not contain the name"),
            (_answer(title="Registered Agent"), "an agent is not the owner"),
            (_answer(title="Owner / Registered Agent"), "an agent is not the owner"),
            (_answer(title="statutory agent"), "an agent is not the owner"),
            (_answer(title="Agent for Service of Process"), "an agent is not the owner"),
            (
                _answer(title="Owner", quote="Registered Agent: Bryan Griggs"),
                "an agent is not the owner",
            ),
            (_answer("Nicole Cole", quote="Nicole founded Acme."), "does not contain the name"),
            (_answer("Al Lee", quote="Our Leeds office opened."), "does not contain the name"),
            (_answer(",", reason=""), "no name offered"),
        ],
    )
    def test_each_rule_omits_with_its_reason(self, answer: JsonValue, reason: str) -> None:
        admitted = admit_owner(OwnerAnswer.model_validate(answer), reads=[_read()])
        assert admitted.owner is None
        assert admitted.omitted is not None
        assert reason in admitted.omitted

    def test_a_null_name_carries_the_models_reason(self) -> None:
        answer = OwnerAnswer.model_validate(_answer(None, reason="x" * 500))
        omitted = admit_owner(answer, reads=[]).omitted
        assert omitted is not None
        assert omitted.startswith("no name offered: ")
        assert len(omitted) <= len("no name offered: ") + 200

    def test_google_maps_is_never_admitted(self) -> None:
        maps = "https://www.google.com/maps/place/Acme"
        answer = OwnerAnswer.model_validate(_answer(url=maps))
        omitted = admit_owner(answer, reads=[_read(maps)]).omitted
        assert omitted is not None
        assert "Google Maps" in omitted

    def test_the_registry_record_is_not_a_read(self) -> None:
        """No evidence parameter: the census cannot name the owner, which is why the node runs."""
        answer = OwnerAnswer.model_validate(_answer(url=CENSUS_URL))
        omitted = admit_owner(answer, reads=[_read()]).omitted
        assert omitted is not None
        assert "never successfully read" in omitted

    @pytest.mark.parametrize(
        ("code", "text", "reported_url"),
        [
            (403, http_error_text(403, "Forbidden"), None),
            (200, redirect_text(ABOUT, "https://other.test/"), None),
            (200, "About us.", "https://other.test/about"),
        ],
    )
    async def test_a_page_that_was_not_really_read_cannot_be_cited(
        self, code: int, text: str, reported_url: str | None
    ) -> None:
        """Fail-closed through ``ReadTracker``: a 403 or a cross-host redirect is not a read."""
        fetch = _fetched(code=code, text=text, reported_url=reported_url)
        admitted, _ = await _resolve([INIT, *fetch, result_line(_answer())])
        assert admitted.owner is None

    async def test_a_null_answer_is_no_owner_and_no_error(self) -> None:
        stream = [INIT, *_fetched(), result_line(_answer(None, url=None, reason="none named"))]
        admitted, _ = await _resolve(stream)
        assert admitted.owner is None
        assert admitted.omitted == "no name offered: none named"

    def test_no_name_ever_reaches_a_log(self) -> None:
        """A refused name may be a real person's; omissions log the reason only."""
        answer = OwnerAnswer.model_validate(_answer(title="Registered Agent"))
        with capture_logs() as captured:
            admit_owner(answer, reads=[_read()])
        assert captured
        assert "Griggs" not in repr(captured)

    def test_the_models_reason_is_kept_but_never_logged(self) -> None:
        """PR #34 review M2: the reason a name was not offered may itself name the person."""
        why = "Found Bryan Griggs on a directory but could not fetch it"
        answer = OwnerAnswer.model_validate(_answer(None, url=None, reason=why))
        with capture_logs() as captured:
            admitted = admit_owner(answer, reads=[])
        assert admitted.omitted == f"no name offered: {why}"
        assert captured
        assert "Griggs" not in repr(captured)


class TestFailuresStillRecordCost:
    @pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
    async def test_a_capped_run(self, subtype: str) -> None:
        stream = [
            INIT,
            result_line(None, subtype=subtype, is_error=True, total_cost_usd=0.5, num_turns=8),
        ]
        cost = RunCost()
        with pytest.raises(OwnerJudgmentError) as exc_info:
            await run_owner_judgment(a_candidate(), cost=cost, runner=Replay(stream))
        assert exc_info.value.reason == subtype
        assert cost.total_usd() == Decimal("0.5")

    async def test_no_structured_answer(self) -> None:
        cost = RunCost()
        with pytest.raises(OwnerJudgmentError) as exc_info:
            await run_owner_judgment(
                a_candidate(), cost=cost, runner=Replay([INIT, result_line(None)])
            )
        assert exc_info.value.reason == "no_structured_output"
        assert cost.total_usd() == Decimal("0.1")

    async def test_an_answer_off_contract(self) -> None:
        cost = RunCost()
        stream = [INIT, result_line({"name": 5})]
        with pytest.raises(OwnerJudgmentError) as exc_info:
            await run_owner_judgment(a_candidate(), cost=cost, runner=Replay(stream))
        assert exc_info.value.reason == "invalid_structured_output"
        assert cost.total_usd() == Decimal("0.1")

    async def test_no_result_at_all(self) -> None:
        cost = _SpyCost()
        with pytest.raises(OwnerJudgmentError) as exc_info:
            await run_owner_judgment(a_candidate(), cost=cost, runner=Replay([INIT]))
        assert exc_info.value.reason == "no_result"
        assert cost.anthropic_usd == [None]  # recorded as unknown, never invented

    async def test_an_sdk_failure(self) -> None:
        async def broken(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
            raise CLINotFoundError("claude CLI not found")
            yield  # an async generator, like `query`: the raise happens on first iteration

        cost = _SpyCost()
        with pytest.raises(OwnerJudgmentError) as exc_info:
            await run_owner_judgment(a_candidate(), cost=cost, runner=broken)
        assert exc_info.value.reason == "sdk_error"
        assert cost.anthropic_usd == [None]


class _SpyCost(RunCost):
    """A ``RunCost`` that remembers each Anthropic entry's dollars, ``None`` included."""

    def __init__(self) -> None:
        super().__init__()
        self.anthropic_usd: list[Decimal | None] = []

    def record(self, kind: BillableKind, count: int = 1, usd: Decimal | None = None) -> None:
        if kind is BillableKind.anthropic_tokens:
            self.anthropic_usd.append(usd)
        super().record(kind, count, usd)


class TestIsolation:
    def test_the_node_can_only_search_and_fetch(self) -> None:
        settings = get_settings()
        options = build_options(settings)
        assert options.tools == list(OWNER_TOOLS)
        assert options.allowed_tools == list(OWNER_TOOLS)
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.permission_mode == "dontAsk"
        assert options.model == settings.owner_resolver_model
        assert options.max_turns == settings.owner_resolver_max_turns
        assert options.max_budget_usd == float(settings.owner_resolver_max_budget_usd)

    def test_the_answer_contract_is_lax(self) -> None:
        answer = OwnerAnswer.model_validate({"name": "A Person", "confidence": "high"})
        assert answer.citation is None
