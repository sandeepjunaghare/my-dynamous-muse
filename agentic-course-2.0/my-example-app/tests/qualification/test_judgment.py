"""The ``classify_rollup`` judgment node offline: replayed CLI streams through the real ``query``.

These prove the wiring and the citation gate — that a verdict survives only when it rests on a page
the node verifiably read, that one call decides every rule, and that cost is recorded on every exit.
They do **not** prove the model is right: the fixtures are synthetic. Accuracy (M6: the four known
rollups fire, <5% false positives) is ``test_live_eval.py``'s job, on a labelled set.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.core.cost import BillableKind, RunCost
from app.manifests.schemas import ManifestBody, RuleOperator, ScoreAxis, SourceKind
from app.qualification.exceptions import JudgmentNodeError
from app.qualification.judgment import (
    JUDGMENT_TOOLS,
    admit_judgment,
    build_options,
    evidence_reads,
    listing_name,
    run_judgment,
)
from app.qualification.prompts import SYSTEM_PROMPT, VERDICT_PHRASES, build_user_prompt
from app.qualification.schemas import AdmittedJudgment, JudgmentAnswer
from app.shared.page_reads import PageRead
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import (
    CandidateFields,
    HeadcountBand,
    MissKind,
    PlacesEvidence,
    PostalAddress,
    ScoreBand,
)
from tests.manifests.replay import (
    Replay,
    StreamLine,
    http_error_text,
    load_transcript,
    redirect_text,
    result_line,
    web_fetch_call,
    web_fetch_result,
)
from tests.qualification.builders import (
    DOUBLE_BROKERING_RULE,
    ROLLUP_RULE,
    a_body_with,
    judgment,
    predicate,
    signal,
)
from tests.sourcing.builders import CENSUS_URL, RETRIEVED_AT, a_candidate, a_verdict

FIXTURES = Path(__file__).parent / "fixtures"
ROLLUP_FIXTURES = ("impact_fire", "summit_fire", "century_fire", "control_systems")
ABOUT = "https://www.acmefire.test/about"
READ_AT = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)

BODY = a_body_with(
    judgment(ROLLUP_RULE),
    judgment(DOUBLE_BROKERING_RULE, "authority history suggests double-brokering"),
    predicate("outside_dfw_metro", "phy_cnty", RuleOperator.not_in_set, ("113",)),
    sources=(("tx_fire_marshal", SourceKind.registry_api),),
    signals=(
        signal("inspection_backlog", ScoreAxis.intensity),
        signal("owner_reachable", ScoreAxis.intensity),
        signal("field_software", ScoreAxis.automatable),
    ),
)
INIT: StreamLine = {
    "type": "system",
    "subtype": "init",
    "session_id": "judgment-replay",
    "model": "claude-opus-5-5",
    "tools": ["WebSearch", "WebFetch"],
    "mcp_servers": [],
    "permissionMode": "dontAsk",
    "cwd": "/app",
}


def _rule(rule_id: str, fired: bool, url: str | None = ABOUT) -> dict[str, JsonValue]:
    citation: JsonValue = None if url is None else {"url": url, "quote": "a national platform"}
    return {"rule_id": rule_id, "fired": fired, "reason": "ownership", "citation": citation}


def _signal(signal_id: str, score: int, url: str | None = ABOUT) -> dict[str, JsonValue]:
    citation: JsonValue = None if url is None else {"url": url, "quote": "evidence"}
    return {"signal_id": signal_id, "answer": "yes", "axis_score": score, "citation": citation}


def _answer(
    rules: list[dict[str, JsonValue]], signals: list[dict[str, JsonValue]] | None = None
) -> JsonValue:
    rule_items: list[JsonValue] = list(rules)
    signal_items: list[JsonValue] = list(signals or [])
    return {"rules": rule_items, "signals": signal_items}


def _fetched(
    url: str = ABOUT, *, code: int = 200, text: str = "About us.", reported_url: str | None = None
) -> list[StreamLine]:
    return [
        web_fetch_call("toolu_f1", url),
        web_fetch_result("toolu_f1", url, code=code, text=text, reported_url=reported_url),
    ]


async def _judge(
    stream: list[StreamLine], candidate: CandidateFields | None = None, body: ManifestBody = BODY
) -> tuple[AdmittedJudgment, RunCost]:
    cost = RunCost()
    fields = candidate or a_candidate()
    run = await run_judgment(fields, body, cost=cost, runner=Replay(stream), clock=lambda: READ_AT)
    return admit_judgment(run.answer, body, reads=run.reads, evidence=evidence_reads(fields)), cost


class TestTheFourKnownRollups:
    @pytest.mark.parametrize("slug", ROLLUP_FIXTURES)
    async def test_each_classifies_as_a_rollup(self, slug: str) -> None:
        """E7: Impact Fire, Summit Fire, Century Fire, Control Systems — wiring, not accuracy."""
        stream = load_transcript(FIXTURES / f"judgment_rollup_{slug}.json")
        admitted, cost = await _judge(stream)

        assert [rule.rule_id for rule in admitted.fired] == [ROLLUP_RULE]
        reason = admitted.fired[0].reason
        assert reason.retrieval_method is RetrievalMethod.llm_inference
        assert reason.retrieved_at == READ_AT
        assert "national" in reason.value
        assert cost.total_usd(BillableKind.anthropic_tokens) == Decimal("0.0812")


class TestTheCitationGate:
    async def test_a_local_business_fires_nothing(self) -> None:
        stream = [INIT, *_fetched(), result_line(_answer([_rule(ROLLUP_RULE, False)]))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()

    async def test_a_verdict_citing_an_unfetched_page_never_disqualifies(self) -> None:
        unread = "https://www.somewhere-else.test/news"
        stream = [INIT, *_fetched(), result_line(_answer([_rule(ROLLUP_RULE, True, unread)]))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()
        assert admitted.omitted[0].label == f"rules[{ROLLUP_RULE}]"
        assert "never successfully read" in admitted.omitted[0].reason

    async def test_a_verdict_without_a_citation_never_disqualifies(self) -> None:
        stream = [INIT, *_fetched(), result_line(_answer([_rule(ROLLUP_RULE, True, None)]))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()

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
        """Fail-closed: a 403, a cross-host redirect and a host mismatch are not reads."""
        fetch = _fetched(code=code, text=text, reported_url=reported_url)
        stream = [INIT, *fetch, result_line(_answer([_rule(ROLLUP_RULE, True)]))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()

    async def test_the_registry_record_cannot_back_a_fired_rule(self) -> None:
        """PR #18 review M1: a census row cannot show a rollup, and a fired rule is permanent.

        With no page read at all, a rollup verdict citing the candidate's own registry URL would
        otherwise suppress the business for good on an unchecked quote.
        """
        stream = [INIT, result_line(_answer([_rule(ROLLUP_RULE, True, CENSUS_URL)]))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()
        assert "cannot evidence a judgment" in admitted.omitted[0].reason

    async def test_the_registry_record_can_back_a_signal_answer(self) -> None:
        """Already-cited evidence, dated when the census was read — fine for a score."""
        signals = [
            _signal("inspection_backlog", 3, CENSUS_URL),
            _signal("field_software", 4, CENSUS_URL),
        ]
        admitted, _ = await _judge([INIT, result_line(_answer([], signals))])
        assert admitted.priority is not None
        assert admitted.priority.source_url == CENSUS_URL
        assert admitted.priority.retrieved_at == RETRIEVED_AT

    async def test_a_google_maps_page_is_never_admitted(self) -> None:
        """PR #18 review M2 / D13: Places content may not be stored, even as a citation."""
        maps = "https://www.google.com/maps/place/Acme+Fire"
        rules = [_rule(ROLLUP_RULE, True, maps)]
        signals = [_signal("inspection_backlog", 4, maps), _signal("field_software", 4, maps)]
        stream = [INIT, *_fetched(maps), result_line(_answer(rules, signals))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()
        assert admitted.priority is None
        assert all("Google Maps" in o.reason for o in admitted.omitted)

    async def test_the_model_cannot_invent_or_re_decide_a_rule(self) -> None:
        """Undeclared ids and free predicates are not the node's to fire."""
        rules = [_rule("made_up_rule", True), _rule("outside_dfw_metro", True)]
        stream = [INIT, *_fetched(), result_line(_answer(rules))]
        admitted, _ = await _judge(stream)
        assert admitted.fired == ()
        assert {o.reason for o in admitted.omitted} == {"not a judgment rule of this manifest"}

    async def test_one_call_decides_every_judgment_rule(self) -> None:
        rules = [_rule(ROLLUP_RULE, True), _rule(DOUBLE_BROKERING_RULE, True)]
        replay = Replay([INIT, *_fetched(), result_line(_answer(rules))])
        run = await run_judgment(a_candidate(), BODY, cost=RunCost(), runner=replay)
        admitted = admit_judgment(run.answer, BODY, reads=run.reads)

        assert len(replay.prompts) == 1
        assert ROLLUP_RULE in replay.prompts[0] and DOUBLE_BROKERING_RULE in replay.prompts[0]
        assert "outside_dfw_metro" not in replay.prompts[0]
        assert [r.rule_id for r in admitted.fired] == [ROLLUP_RULE, DOUBLE_BROKERING_RULE]

    async def test_a_rule_decided_twice_keeps_the_first_answer(self) -> None:
        rules = [_rule(ROLLUP_RULE, False), _rule(ROLLUP_RULE, True)]
        admitted, _ = await _judge([INIT, *_fetched(), result_line(_answer(rules))])
        assert admitted.fired == ()


class TestThePlacesListingNameIsNeverStored:
    """D13 (PR #34 review M1): the listing name is prompt context; stored text may not repeat it."""

    LISTED = "napco bag & film"

    @staticmethod
    def _admit(
        rules: list[dict[str, JsonValue]], signals: list[dict[str, JsonValue]] | None = None
    ) -> AdmittedJudgment:
        answer = JudgmentAnswer.model_validate(_answer(rules, signals))
        reads = [PageRead(url=ABOUT, read_at=READ_AT)]
        return admit_judgment(
            answer, BODY, reads=reads, listed=TestThePlacesListingNameIsNeverStored.LISTED
        )

    def test_a_fired_rule_whose_reason_repeats_it_is_dropped(self) -> None:
        rule = _rule(ROLLUP_RULE, True) | {"reason": "Maps lists it as NAPCO Bag &  Film"}
        with capture_logs() as captured:
            admitted = self._admit([rule])
        assert captured
        assert admitted.fired == ()
        assert [o.reason for o in admitted.omitted] == [
            "repeats the Places listing name, which may not be stored (D13)"
        ]
        assert "napco" not in repr(captured).casefold()

    def test_a_signal_whose_quote_repeats_it_is_dropped(self) -> None:
        cited = _signal("inspection_backlog", 4)
        quoted: dict[str, JsonValue] = {
            **cited,
            "citation": {"url": ABOUT, "quote": "Napco Bag & Film, Dallas"},
        }
        admitted = self._admit([], [quoted, _signal("field_software", 5)])
        assert [s.signal_id for s in admitted.signals] == ["field_software"]
        assert admitted.priority is None

    def test_an_answer_that_does_not_repeat_it_is_kept(self) -> None:
        admitted = self._admit([_rule(ROLLUP_RULE, True)])
        assert [r.rule_id for r in admitted.fired] == [ROLLUP_RULE]

    @pytest.mark.parametrize(
        ("places", "expected"),
        [
            (None, None),
            (PlacesEvidence(), None),
            (PlacesEvidence(display_name="  "), None),
            (PlacesEvidence(display_name="ACME  FREIGHT"), None),  # the census DBA: a cited fact
            (PlacesEvidence(display_name="Acme Logistics"), None),  # within the legal name
            (PlacesEvidence(display_name="Napco Bag & Film"), "napco bag & film"),
        ],
    )
    def test_only_a_name_the_registry_does_not_give_is_held_back(
        self, places: PlacesEvidence | None, expected: str | None
    ) -> None:
        assert listing_name(places, a_candidate()) == expected


class TestPriority:
    async def test_cited_answers_on_both_axes_give_a_cited_score(self) -> None:
        signals = [_signal("inspection_backlog", 4), _signal("field_software", 5)]
        stream = [INIT, *_fetched(), result_line(_answer([], signals))]
        admitted, _ = await _judge(stream)
        assert admitted.priority is not None
        assert admitted.priority.value.score == 20
        assert admitted.priority.value.band is ScoreBand.live
        assert admitted.priority.source_url == ABOUT
        assert admitted.priority.retrieval_method is RetrievalMethod.llm_inference

    async def test_one_axis_uncited_means_no_score(self) -> None:
        signals = [_signal("inspection_backlog", 4), _signal("field_software", 5, None)]
        admitted, _ = await _judge([INIT, *_fetched(), result_line(_answer([], signals))])
        assert admitted.priority is None

    async def test_an_out_of_range_score_is_dropped(self) -> None:
        signals = [_signal("inspection_backlog", 9), _signal("field_software", 3)]
        admitted, _ = await _judge([INIT, *_fetched(), result_line(_answer([], signals))])
        assert admitted.priority is None
        assert "outside 1-5" in admitted.omitted[0].reason


class TestFailuresStillRecordCost:
    @pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
    async def test_a_capped_run(self, subtype: str) -> None:
        stream = [
            INIT,
            result_line(None, subtype=subtype, is_error=True, total_cost_usd=0.5, num_turns=8),
        ]
        cost = RunCost()
        with pytest.raises(JudgmentNodeError) as exc_info:
            await run_judgment(a_candidate(), BODY, cost=cost, runner=Replay(stream))
        assert exc_info.value.reason == subtype
        assert cost.total_usd() == Decimal("0.5")

    async def test_no_structured_answer(self) -> None:
        cost = RunCost()
        with pytest.raises(JudgmentNodeError) as exc_info:
            await run_judgment(
                a_candidate(), BODY, cost=cost, runner=Replay([INIT, result_line(None)])
            )
        assert exc_info.value.reason == "no_structured_output"
        assert cost.total_usd() == Decimal("0.1")

    async def test_an_answer_off_contract(self) -> None:
        stream = [INIT, result_line({"rules": "rollup, obviously"})]
        with pytest.raises(JudgmentNodeError) as exc_info:
            await run_judgment(a_candidate(), BODY, cost=RunCost(), runner=Replay(stream))
        assert exc_info.value.reason == "invalid_structured_output"

    async def test_no_result_at_all(self) -> None:
        cost = RunCost()
        with pytest.raises(JudgmentNodeError) as exc_info:
            await run_judgment(a_candidate(), BODY, cost=cost, runner=Replay([INIT]))
        assert exc_info.value.reason == "no_result"
        assert cost.total_calls(BillableKind.anthropic_tokens) == 0


class TestIsolationAndPrompt:
    def test_the_node_can_only_search_and_fetch(self) -> None:
        options = build_options(get_settings())
        assert options.tools == list(JUDGMENT_TOOLS)
        assert options.allowed_tools == list(JUDGMENT_TOOLS)
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.permission_mode == "dontAsk"
        assert options.max_turns == get_settings().rollup_classifier_max_turns

    def test_the_prompt_carries_the_cited_identity_and_the_rules(self) -> None:
        prompt = build_user_prompt(a_candidate(), [judgment()], [signal("x", ScoreAxis.intensity)])
        assert "Acme Logistics 1234567 LLC" in prompt
        assert CENSUS_URL in prompt
        assert ROLLUP_RULE in prompt
        assert "Places evidence" not in prompt

    def test_places_evidence_is_context_only(self) -> None:
        places = PlacesEvidence(display_name="Acme Fire", business_status="OPERATIONAL")
        prompt = build_user_prompt(a_candidate(), [judgment()], [], places)
        assert "Places evidence (context only, never cite)" in prompt
        assert "OPERATIONAL" in prompt

    @pytest.mark.parametrize("kind", list(MissKind))
    def test_a_current_verdict_is_our_phrase_and_never_places_content(self, kind: MissKind) -> None:
        """Website-verification B5: the stored kind, in our words; no place ID, type or name."""
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict(kind)})
        prompt = build_user_prompt(fields, [judgment()], [])
        assert "Places verdict (context only, never cite):" in prompt
        assert VERDICT_PHRASES[kind] in prompt
        assert "ChIJ" not in prompt
        assert kind.value not in prompt

    def test_a_verdict_for_an_old_address_is_left_out(self) -> None:
        moved = PostalAddress(
            street="900 Main St", city="Fort Worth", state="TX", postal_code="76102"
        )
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict(address=moved)})
        assert "Places verdict" not in build_user_prompt(fields, [judgment()], [])

    def test_the_system_prompt_says_the_verdict_is_never_cited(self) -> None:
        assert "Places verdict" in SYSTEM_PROMPT

    def test_the_answer_contract_is_lax(self) -> None:
        """An out-of-range score must cost one answer in the gate, not the whole paid call."""
        answer = JudgmentAnswer.model_validate({"signals": [_signal("x", 42)]})
        assert answer.signals[0].axis_score == 42


HOME = "https://www.acmefire.test/"


def _extras(
    *,
    website: str | None = None,
    headcount: tuple[int, int | None] | None = None,
    headcount_url: str | None = HOME,
) -> JsonValue:
    answer: dict[str, JsonValue] = {"rules": [], "signals": []}
    if website is not None:
        answer["website"] = {"url": website, "quote": "Welcome"}
    if headcount is not None:
        citation: JsonValue = (
            None if headcount_url is None else {"url": headcount_url, "quote": "our team"}
        )
        answer["headcount"] = {"low": headcount[0], "high": headcount[1], "citation": citation}
    return answer


class TestWebsiteAndHeadcount:
    async def test_a_cited_homepage_is_admitted_as_its_origin(self) -> None:
        stream = [INIT, *_fetched(HOME), result_line(_extras(website=HOME))]
        admitted, _ = await _judge(stream)
        assert admitted.website is not None
        assert admitted.website.value == "https://www.acmefire.test"
        assert admitted.website.source_url == HOME
        assert admitted.website.retrieved_at == READ_AT
        assert admitted.website.retrieval_method is RetrievalMethod.llm_inference

    async def test_an_about_page_is_not_a_homepage(self) -> None:
        admitted, _ = await _judge([INIT, *_fetched(), result_line(_extras(website=ABOUT))])
        assert admitted.website is None
        assert "not a homepage" in admitted.omitted[0].reason

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.acmefire.test/",  # unfetched: the stream reads ABOUT only
            "https://www.google.com/maps/",
            CENSUS_URL,  # registry evidence never backs the website
        ],
    )
    async def test_an_unread_or_forbidden_homepage_is_omitted(self, url: str) -> None:
        fetched = _fetched("https://www.google.com/maps/") if "maps" in url else _fetched()
        admitted, _ = await _judge([INIT, *fetched, result_line(_extras(website=url))])
        assert admitted.website is None
        assert admitted.omitted[0].label == "website"

    async def test_a_cited_headcount_is_admitted(self) -> None:
        stream = [INIT, *_fetched(HOME), result_line(_extras(headcount=(11, 50)))]
        admitted, _ = await _judge(stream)
        assert admitted.headcount_band is not None
        assert admitted.headcount_band.value == HeadcountBand(low=11, high=50)
        assert admitted.headcount_band.source_url == HOME

    @pytest.mark.parametrize(
        ("band", "url", "reason"),
        [
            ((50, 11), HOME, "headcount band 50-11 is invalid"),
            ((0, 5), HOME, "headcount band 0-5 is invalid"),
            ((11, 50), None, "no citation offered"),
        ],
    )
    async def test_a_bad_or_uncited_headcount_is_omitted(
        self, band: tuple[int, int | None], url: str | None, reason: str
    ) -> None:
        answer = _extras(headcount=band, headcount_url=url)
        admitted, _ = await _judge([INIT, *_fetched(HOME), result_line(answer)])
        assert admitted.headcount_band is None
        assert admitted.omitted[0].reason == reason

    def test_an_open_ended_band_is_valid(self) -> None:
        assert HeadcountBand(low=50, high=None).label == "50+"

    def test_old_answers_without_either_key_still_validate(self) -> None:
        answer = JudgmentAnswer.model_validate({"rules": [], "signals": []})
        assert (answer.website, answer.headcount) == (None, None)

    def test_the_prompt_asks_for_both(self) -> None:
        assert "website:" in SYSTEM_PROMPT
        assert "headcount:" in SYSTEM_PROMPT
        assert "Never estimate" in " ".join(SYSTEM_PROMPT.split())
