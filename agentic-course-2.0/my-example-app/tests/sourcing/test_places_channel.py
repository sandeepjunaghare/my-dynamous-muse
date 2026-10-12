"""The in-run Places channel: one dict per run, shared by every stage, gone with the run (D13).

``StageContext.places`` has a default factory, so a runner that built each context without passing
the run's dict would compile and silently disconnect ``verify_business`` from ``classify_rollup``.
This is the guard against that.
"""

from uuid import uuid4

from app.core.cost import RunCost
from app.manifests.service import ManifestService
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import PlacesEvidence
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult
from tests.conftest import requires_db
from tests.sourcing.builders import a_brief, an_active_manifest
from tests.sourcing.conftest import SessionFactory

pytestmark = requires_db

EVIDENCE = PlacesEvidence(display_name="Acme Freight", business_status="OPERATIONAL")


class Writes:
    """Stands in for ``verify_business``: puts evidence on the channel and remembers the dict."""

    stage = PipelineStage.verify_business

    def __init__(self) -> None:
        self.seen: list[dict[str, PlacesEvidence]] = []

    async def run(self, context: StageContext) -> StageResult:
        self.seen.append(context.places)
        assert context.places == {}, "a run starts with an empty channel"
        context.places["usdot:1"] = EVIDENCE
        return StageResult(counts={"wrote": 1})


class Reads:
    """Stands in for ``classify_rollup``: records what the channel holds when it runs."""

    stage = PipelineStage.classify_rollup

    def __init__(self) -> None:
        self.seen: list[dict[str, PlacesEvidence]] = []

    async def run(self, context: StageContext) -> StageResult:
        self.seen.append(context.places)
        return StageResult(counts={"read": len(context.places)})


class TestThePlacesChannel:
    async def test_a_later_stage_sees_the_same_dict_and_a_new_run_starts_empty(
        self, session_factory: SessionFactory
    ) -> None:
        async with session_factory() as session:
            _, vertical = await an_active_manifest(session)
            await session.commit()
        writes, reads = Writes(), Reads()
        pipeline = SourcingPipeline(session_factory, stages=[writes, reads])

        first = await pipeline.run(a_brief(vertical))
        second = await pipeline.run(a_brief(vertical))

        assert first.counts["read"] == 1
        assert reads.seen[0] is writes.seen[0]
        assert reads.seen[0]["usdot:1"] is EVIDENCE
        assert writes.seen[1] is not writes.seen[0], "each run gets its own channel"
        assert second.counts["read"] == 1

    async def test_a_test_built_context_gets_its_own_empty_channel(
        self, session_factory: SessionFactory
    ) -> None:
        async with session_factory() as session:
            manifest_id, _ = await an_active_manifest(session)
            manifest = await ManifestService(session).get(manifest_id)
            first = StageContext(run_id=uuid4(), manifest=manifest, session=session, cost=RunCost())
            second = StageContext(
                run_id=uuid4(), manifest=manifest, session=session, cost=RunCost()
            )
        assert first.places == {}
        assert first.places is not second.places
