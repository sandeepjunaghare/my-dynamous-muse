"""Stage 4 over a real run: free before paid, one call per survivor, retries never pay twice."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.manifests.schemas import ManifestBody, ManifestResponse, RuleOperator, ScoreAxis
from app.qualification import judgment
from app.qualification.schemas import AdmittedJudgment, JudgmentAnswer, RecordSet
from app.qualification.service import QualificationService
from app.shared.page_reads import PageRead
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import HeadcountBand, PlacesEvidence, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.classify_rollup import STAGE
from app.tools.registry import StageContext, registered_stages
from tests.conftest import requires_db
from tests.manifests.replay import Replay, StreamLine, result_line, web_fetch_call, web_fetch_result
from tests.qualification.builders import (
    ROLLUP_RULE,
    a_body_with,
    a_run_with,
    census_row,
    predicate,
    signal,
)
from tests.qualification.builders import (
    judgment as a_judgment_rule,
)
from tests.sourcing.builders import VERIFY, a_candidate, looked_up

ABOUT = "https://www.acme.test/about"
BODY = a_body_with(
    predicate("no_broker_entity_type", "carship", RuleOperator.not_contains, "B"),
    a_judgment_rule(ROLLUP_RULE),
    signals=(signal("backlog", ScoreAxis.intensity), signal("software", ScoreAxis.automatable)),
)


def _stream(answer: JsonValue) -> list[StreamLine]:
    return [web_fetch_call("t1", ABOUT), web_fetch_result("t1", ABOUT), result_line(answer)]


ROLLUP: list[StreamLine] = _stream(
    {
        "rules": [
            {
                "rule_id": ROLLUP_RULE,
                "fired": True,
                "reason": "one branch of a national platform",
                "citation": {"url": ABOUT, "quote": "a national platform"},
            }
        ],
        "signals": [],
    }
)
LOCAL: list[StreamLine] = _stream(
    {
        "rules": [
            {"rule_id": ROLLUP_RULE, "fired": False, "reason": "owner-run", "citation": None}
        ],
        "signals": [
            {
                "signal_id": signal_id,
                "answer": "yes",
                "axis_score": 4,
                "citation": {"url": ABOUT, "quote": "q"},
            }
            for signal_id in ("backlog", "software")
        ],
    }
)
ROLLUP_SCORED: list[StreamLine] = _stream(
    {
        "rules": [
            {
                "rule_id": ROLLUP_RULE,
                "fired": True,
                "reason": "one branch of a national platform",
                "citation": {"url": ABOUT, "quote": "a national platform"},
            }
        ],
        "signals": [
            {
                "signal_id": sid,
                "answer": "yes",
                "axis_score": 5,
                "citation": {"url": ABOUT, "quote": "q"},
            }
            for sid in ("backlog", "software")
        ],
    }
)
FAILED: list[StreamLine] = [result_line(None, subtype="error_max_turns", is_error=True)]


class _ByRegistryId:
    """A runner that replays a different recording per candidate, found by its id in the prompt."""

    def __init__(self, streams: Mapping[str, list[StreamLine]]) -> None:
        self._streams = streams
        self.prompts: list[str] = []

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompts.append(prompt)
        registry_id = next(rid for rid in self._streams if f"registry id: {rid}" in prompt)
        return Replay(self._streams[registry_id])(prompt, options)


async def _context(
    session: AsyncSession, manifest: ManifestResponse, run: SourcingRunResponse
) -> StageContext:
    return StageContext(run_id=run.id, manifest=manifest, session=session, cost=RunCost())


async def _setup(
    session: AsyncSession, body: ManifestBody = BODY
) -> tuple[ManifestResponse, SourcingRunResponse]:
    """Three candidates; 900 is removed by the free predicate before any judgment."""
    manifest, run, candidates = await a_run_with(session, body, "900", "901", "902", verified=True)
    records: RecordSet = {"fmcsa": census_row(carship="C")}
    await QualificationService(session).disqualify_by_predicates(
        candidates[0], run, manifest, records
    )
    return manifest, run


def test_the_stage_is_registered_in_pipeline_order() -> None:
    stages = [stage.stage for stage in registered_stages()]
    assert PipelineStage.classify_rollup in stages
    assert stages == sorted(stages, key=list(PipelineStage).index)


@requires_db
class TestTheStage:
    async def test_free_before_paid_and_one_call_per_survivor(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"901": ROLLUP, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        assert len(runner.prompts) == 2
        assert not any("registry id: 900" in prompt for prompt in runner.prompts)
        assert dict(result.counts) == {
            "classified": 2,
            "judgment_disqualified": 1,
            "judgment_failed": 0,
            "scored": 1,
            "not_scored": 0,
            "judgment_skipped": 1,
            "judgment_unverified_skipped": 0,
            "judgment_capped": 0,
            "judgment_website_filled": 0,
            "judgment_headcount_filled": 0,
        }

        candidates = {
            c.registry_id: c for c in await SourcingService(db_session).list_candidates(run.id)
        }
        rows = await QualificationService(db_session).list_for_candidate(candidates["901"].id)
        assert [row.rule_id for row in rows] == [ROLLUP_RULE]
        priority = candidates["902"].fields.priority
        assert priority is not None
        assert priority.value.score == 16
        assert priority.retrieval_method is RetrievalMethod.llm_inference
        assert candidates["901"].fields.priority is None

    async def test_a_retry_pays_nothing(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"901": ROLLUP, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)
        await STAGE.run(await _context(db_session, manifest, run))

        again = await STAGE.run(await _context(db_session, manifest, run))

        assert len(runner.prompts) == 2
        assert again.counts["classified"] == 0
        assert again.counts["judgment_skipped"] == 3

    async def test_one_failed_call_does_not_sink_the_batch(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"901": FAILED, "902": LOCAL}))
        manifest, run = await _setup(db_session)
        result = await STAGE.run(await _context(db_session, manifest, run))
        assert result.counts["judgment_failed"] == 1
        assert result.counts["scored"] == 1

    async def test_the_call_cap_ends_the_stage_degraded(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Amendment 3: a breaker, never a raise; a raise would fail a run already paid for."""
        monkeypatch.setenv("MAX_JUDGMENT_CALLS_PER_RUN", "1")
        runner = _ByRegistryId({"901": ROLLUP, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        assert len(runner.prompts) == 1
        assert result.counts["judgment_capped"] == 1
        assert result.degraded_reason == "judgment cap of 1 calls reached; 1 candidate(s) unjudged"

    async def test_places_evidence_reaches_the_prompt(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({"901": LOCAL, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)
        context = await _context(db_session, manifest, run)
        context.places["901"] = PlacesEvidence(display_name="Acme Freight Dallas")

        await STAGE.run(context)

        prompt_901 = next(p for p in runner.prompts if "registry id: 901 " in p)
        prompt_902 = next(p for p in runner.prompts if "registry id: 902 " in p)
        assert "Places evidence" in prompt_901
        assert "Acme Freight Dallas" in prompt_901
        assert "Places evidence" not in prompt_902

    async def test_a_verdict_repeating_the_listing_name_is_never_stored(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D13 (PR #34 review M1): Places context reaches the prompt, never a disqualification."""
        echoed = _stream(
            {
                "rules": [
                    {
                        "rule_id": ROLLUP_RULE,
                        "fired": True,
                        "reason": "Maps lists it as Napco Bag & Film, a national platform",
                        "citation": {"url": ABOUT, "quote": "a national platform"},
                    }
                ],
                "signals": [],
            }
        )
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"901": echoed, "902": LOCAL}))
        manifest, run = await _setup(db_session)
        context = await _context(db_session, manifest, run)
        context.places["901"] = PlacesEvidence(display_name="NAPCO Bag & Film")

        result = await STAGE.run(context)

        assert result.counts["judgment_disqualified"] == 0
        candidates = {
            c.registry_id: c for c in await SourcingService(db_session).list_candidates(run.id)
        }
        assert await QualificationService(db_session).list_for_candidate(candidates["901"].id) == []
        assert "napco" not in candidates["901"].fields.model_dump_json().casefold()

    async def test_an_unverified_candidate_is_never_sent(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OQ-5: T9 cannot promote it, so it is not paid for."""
        runner = _ByRegistryId({"911": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run, _ = await a_run_with(db_session, BODY, "911")

        result = await STAGE.run(await _context(db_session, manifest, run))

        assert runner.prompts == []
        assert result.counts["judgment_unverified_skipped"] == 1
        assert result.counts["classified"] == 0

    async def test_a_manifest_with_nothing_to_judge_makes_no_call(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = _ByRegistryId({})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        body = a_body_with(predicate("no_broker", "carship", RuleOperator.not_contains, "B"))
        manifest, run = await _setup(db_session, body)
        result = await STAGE.run(await _context(db_session, manifest, run))
        assert runner.prompts == []
        assert dict(result.counts) == {"classified": 0}


@requires_db
class TestReviewFixes:
    async def test_a_rejected_business_keeps_no_priority(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #18 review L5: a rollup scored 25 must not read as "live" on its candidate row."""
        monkeypatch.setattr(
            judgment, "sdk_runner", _ByRegistryId({"901": ROLLUP_SCORED, "902": LOCAL})
        )
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        candidates = {
            c.registry_id: c for c in await SourcingService(db_session).list_candidates(run.id)
        }
        assert candidates["901"].fields.priority is None
        assert (result.counts["judgment_disqualified"], result.counts["scored"]) == (1, 1)

    async def test_a_refused_priority_write_does_not_stop_the_batch(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #18 review M2: a candidate-model refusal (here D13) is one failure, not a crash."""
        real_admit = judgment.admit_judgment
        maps = "https://www.google.com/maps/place/Acme"

        def admit_with_maps_anchor(
            answer: JudgmentAnswer,
            body: ManifestBody,
            *,
            reads: Sequence[PageRead],
            evidence: Sequence[PageRead] = (),
            listed: str | None = None,
        ) -> AdmittedJudgment:
            admitted = real_admit(answer, body, reads=reads, evidence=evidence, listed=listed)
            if admitted.priority is None:
                return admitted
            return replace(
                admitted, priority=admitted.priority.model_copy(update={"source_url": maps})
            )

        monkeypatch.setattr(judgment, "admit_judgment", admit_with_maps_anchor)
        runner = _ByRegistryId({"901": LOCAL, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        assert len(runner.prompts) == 2
        assert (result.counts["judgment_failed"], result.counts["scored"]) == (2, 0)


HOME = "https://www.acme.test/"


def _with_extras(*, fired: bool) -> list[StreamLine]:
    """A homepage read backing everything: signals, the website, and an 11-50 headcount."""
    rule_citation: JsonValue = {"url": HOME, "quote": "a national platform"} if fired else None
    answer: JsonValue = {
        "rules": [
            {"rule_id": ROLLUP_RULE, "fired": fired, "reason": "r", "citation": rule_citation}
        ],
        "signals": [
            {
                "signal_id": sid,
                "answer": "yes",
                "axis_score": 4,
                "citation": {"url": HOME, "quote": "q"},
            }
            for sid in ("backlog", "software")
        ],
        "website": {"url": HOME, "quote": "Welcome to Acme"},
        "headcount": {"low": 11, "high": 50, "citation": {"url": HOME, "quote": "11-50 staff"}},
    }
    return [web_fetch_call("t1", HOME), web_fetch_result("t1", HOME), result_line(answer)]


@requires_db
class TestWebsiteAndHeadcount:
    async def test_a_cited_homepage_and_headcount_are_stored(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extras = _with_extras(fired=False)
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"901": extras, "902": LOCAL}))
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        fields = {
            c.registry_id: c.fields
            for c in await SourcingService(db_session).list_candidates(run.id)
        }["901"]
        assert fields.website is not None
        assert fields.website.value == "https://www.acme.test"
        assert fields.website.retrieval_method is RetrievalMethod.llm_inference
        assert fields.headcount_band is not None
        assert fields.headcount_band.value == HeadcountBand(low=11, high=50)
        assert fields.priority is not None
        assert result.counts["judgment_website_filled"] == 1
        assert result.counts["judgment_headcount_filled"] == 1
        assert result.counts["scored"] == 2

    async def test_verify_business_website_is_never_replaced(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extras = _with_extras(fired=False)
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"901": extras, "902": LOCAL}))
        manifest, run = await _setup(db_session)
        sourcing = SourcingService(db_session)
        verified = a_candidate("901").model_copy(update={"website": looked_up("https://acme.test")})
        await sourcing.record_candidates(run.id, [verified], stage=VERIFY)

        result = await STAGE.run(await _context(db_session, manifest, run))

        fields = {c.registry_id: c.fields for c in await sourcing.list_candidates(run.id)}["901"]
        assert fields.website is not None
        assert fields.website.value == "https://acme.test"
        assert fields.website.retrieval_method is RetrievalMethod.web_lookup
        assert result.counts["judgment_website_filled"] == 0
        assert fields.headcount_band is not None

    async def test_a_disqualified_candidate_stores_neither(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extras = _with_extras(fired=True)
        monkeypatch.setattr(judgment, "sdk_runner", _ByRegistryId({"901": extras, "902": LOCAL}))
        manifest, run = await _setup(db_session)

        result = await STAGE.run(await _context(db_session, manifest, run))

        fields = {
            c.registry_id: c.fields
            for c in await SourcingService(db_session).list_candidates(run.id)
        }["901"]
        assert (fields.website, fields.headcount_band, fields.priority) == (None, None, None)
        assert result.counts["judgment_disqualified"] == 1
