"""Stage 4 over a real run: free before paid, one call per survivor, retries never pay twice."""

from collections.abc import AsyncIterator, Mapping

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.core.exceptions import CostLimitExceededError
from app.manifests.schemas import ManifestBody, ManifestResponse, RuleOperator, ScoreAxis
from app.qualification import judgment
from app.qualification.schemas import RecordSet
from app.qualification.service import QualificationService
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import SourcingRunResponse
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
    manifest, run, candidates = await a_run_with(session, body, "900", "901", "902")
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
            "not_scored": 1,
            "judgment_skipped": 1,
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

    async def test_the_call_cap_is_a_circuit_breaker(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAX_JUDGMENT_CALLS_PER_RUN", "1")
        runner = _ByRegistryId({"901": ROLLUP, "902": LOCAL})
        monkeypatch.setattr(judgment, "sdk_runner", runner)
        manifest, run = await _setup(db_session)

        with pytest.raises(CostLimitExceededError) as exc_info:
            await STAGE.run(await _context(db_session, manifest, run))

        assert len(runner.prompts) == 1
        assert exc_info.value.cap == 1

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
