"""The runner end to end — the T5 acceptance tests — against a real Postgres and the mock FMCSA.

``session_factory`` hands out fresh sessions on one rolled-back connection, so the crash path
("finish a failed run on a *fresh* session") is exercised for real. Every HTTP request goes to
:class:`MockFmcsa`, which raises on anything it does not know.
"""

from collections.abc import Mapping
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update

from app.manifests.exceptions import ActiveManifestNotFoundError
from app.sourcing.models import SourcingRun
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import CandidateResponse, RunStatus, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult
from app.tools.search_registry import SearchRegistryStage
from tests.conftest import requires_db
from tests.manifests.builders import a_vertical
from tests.sourcing.builders import a_brief, a_freight_body, an_active_manifest_with
from tests.sourcing.conftest import TEST_WEBKEY, MockFmcsa, SessionFactory, a_clock
from tests.sourcing.test_pool import BEST_FIRST

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _webkey(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here has a webKey unless it removes it; never whatever ``.env`` holds."""
    monkeypatch.setenv("FMCSA_WEBKEY", TEST_WEBKEY)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "")


def registry_stage(mock: MockFmcsa, batch_size: int = 3) -> SearchRegistryStage:
    return SearchRegistryStage(
        transport=mock.transport(),
        clock=a_clock(),
        batch_size=batch_size,
        backoff_seconds=0.0,
        throttle=False,
    )


class Boom:
    """A later stage that fails — the way T6/T7/T8 will, one day."""

    def __init__(self, stage: PipelineStage = PipelineStage.verify_business) -> None:
        self._stage = stage

    @property
    def stage(self) -> PipelineStage:
        return self._stage

    async def run(self, context: StageContext) -> StageResult:
        raise RuntimeError("places is down")


class AbortsTheTransaction(Boom):
    """A stage whose SQL fails, leaving its session's transaction aborted."""

    async def run(self, context: StageContext) -> StageResult:
        await context.session.execute(text("select 1 / 0"))
        return StageResult(counts={})


class Counts:
    """A stage that only reports counts."""

    def __init__(self, stage: PipelineStage, counts: Mapping[str, int]) -> None:
        self._stage, self._counts = stage, counts

    @property
    def stage(self) -> PipelineStage:
        return self._stage

    async def run(self, context: StageContext) -> StageResult:
        return StageResult(counts=self._counts)


async def _freight(factory: SessionFactory) -> str:
    async with factory() as session:
        _, vertical = await an_active_manifest_with(session, a_freight_body())
        await session.commit()
    return vertical


async def _candidates(factory: SessionFactory, run: SourcingRunResponse) -> list[CandidateResponse]:
    async with factory() as session:
        return await SourcingService(session).list_candidates(run.id)


async def _runs(factory: SessionFactory, vertical: str) -> list[SourcingRunResponse]:
    async with factory() as session:
        rows = await session.scalars(
            select(SourcingRun)
            .where(SourcingRun.vertical == vertical)
            .order_by(SourcingRun.started_at)
        )
        return [SourcingRunResponse.model_validate(row) for row in rows.all()]


class TestDeterminism:
    async def test_same_fixtures_give_identical_candidates_in_the_same_order(
        self, session_factory: SessionFactory
    ) -> None:
        """Two separate pools (two verticals, one manifest body), the same fixtures, the same
        pinned clock: the stored candidates are identical, field for field and in order."""
        outcomes: list[list[tuple[str, object]]] = []
        for _ in range(2):
            vertical = await _freight(session_factory)
            run = await SourcingPipeline(
                session_factory, stages=[registry_stage(MockFmcsa(), batch_size=8)]
            ).run(a_brief(vertical))
            outcomes.append(
                [
                    (candidate.registry_id, candidate.fields.to_stored())
                    for candidate in await _candidates(session_factory, run)
                ]
            )
        assert outcomes[0] == outcomes[1]
        assert len(outcomes[0]) == 8


class TestBacklog:
    async def test_successive_runs_take_disjoint_batches_best_first(
        self, session_factory: SessionFactory
    ) -> None:
        """D12's test, as the ticket words it, repeated on a second pool to show the order holds."""
        for _ in range(2):
            vertical = await _freight(session_factory)
            batches: list[set[str]] = []
            for _ in range(3):
                run = await SourcingPipeline(
                    session_factory, stages=[registry_stage(MockFmcsa())]
                ).run(a_brief(vertical))
                assert run.status is RunStatus.completed
                batches.append({c.registry_id for c in await _candidates(session_factory, run)})

            assert batches == [set(BEST_FIRST[0:3]), set(BEST_FIRST[3:6]), set(BEST_FIRST[6:8])]

    async def test_a_failed_run_is_finished_with_its_counts_and_gives_its_batch_back(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _freight(session_factory)
        pipeline = SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa()), Boom()])
        with pytest.raises(RuntimeError, match="places is down"):
            await pipeline.run(a_brief(vertical))

        (failed,) = await _runs(session_factory, vertical)
        assert failed.status is RunStatus.failed
        assert failed.finished_at is not None
        assert failed.status_detail == "RuntimeError: places is down"
        assert failed.counts["batched"] == 3
        assert set(failed.cost.calls) == {"places_lookup", "anthropic_tokens"}

        retry = await SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa())]).run(
            a_brief(vertical)
        )
        assert {c.registry_id for c in await _candidates(session_factory, retry)} == set(
            BEST_FIRST[0:3]
        )

    async def test_a_stage_that_aborts_the_transaction_is_still_recorded_as_failed(
        self, session_factory: SessionFactory
    ) -> None:
        """Finishing on the aborted session would raise ``PendingRollbackError`` instead."""
        vertical = await _freight(session_factory)
        pipeline = SourcingPipeline(
            session_factory, stages=[registry_stage(MockFmcsa()), AbortsTheTransaction()]
        )
        with pytest.raises(Exception, match="division by zero"):
            await pipeline.run(a_brief(vertical))

        (failed,) = await _runs(session_factory, vertical)
        assert failed.status is RunStatus.failed
        assert failed.status_detail is not None and "division by zero" in failed.status_detail

    async def test_a_stale_running_run_is_reaped_before_the_next_starts(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _freight(session_factory)
        async with session_factory() as session:
            killed = await SourcingService(session).start_run(a_brief(vertical))
            await session.execute(
                update(SourcingRun)
                .where(SourcingRun.id == killed.id)
                .values(started_at=text("now() - interval '7 hours'"))
            )
            await session.commit()

        await SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa())]).run(
            a_brief(vertical)
        )
        runs = {run.id: run for run in await _runs(session_factory, vertical)}
        assert runs[killed.id].status is RunStatus.failed
        assert runs[killed.id].status_detail == "abandoned: still running after 6h"


class TestEvidence:
    async def test_every_field_is_cited_and_every_bound_source_answered(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _freight(session_factory)
        mock = MockFmcsa()
        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock, 8)]).run(
            a_brief(vertical)
        )

        assert run.status is RunStatus.completed
        assert run.counts == {
            "pool_seen": 8,
            "pool_new": 8,
            "pool_available": 8,
            "pool_rules_pushed_down": 5,
            "pool_rules_not_pushed_down": 0,
            "batched": 8,
            "lookup_found": 8,
            "lookup_not_found": 0,
            "lookup_failed": 0,
            "join_matched": 2,
        }
        candidates = {c.registry_id: c.fields for c in await _candidates(session_factory, run)}
        for fields in candidates.values():
            sources = [cited.value.source for cited in fields.source_records]
            assert sources == ["fmcsa_company_census", "fmcsa_qcmobile", "fmcsa_revocations"]
            singles = (
                fields.registry_id,
                fields.legal_name,
                fields.dba_name,
                fields.address,
                fields.phone,
                fields.website,
                fields.business_check,
            )
            urls = [cited.source_url for cited in singles if cited is not None]
            urls += [cited.source_url for cited in fields.source_records]
            assert all(url.startswith("https://") for url in urls)
            assert not any(TEST_WEBKEY in url for url in urls)
        revoked = candidates["usdot:1000003"].source_records[2].value
        assert revoked.source == "fmcsa_revocations" and len(revoked.rows) == 2
        # One QCMobile call per batch candidate; one census pull; one revocations chunk.
        assert len(mock.calls_to("/carriers/")) == 8
        assert len(mock.calls_to("az4n-8mr2")) == 1
        assert len(mock.calls_to("wb4f-neki")) == 1

    async def test_no_webkey_is_degraded_and_candidates_are_still_recorded(
        self, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FMCSA_WEBKEY", "")
        vertical = await _freight(session_factory)
        mock = MockFmcsa()
        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )

        assert run.status is RunStatus.degraded
        assert run.status_detail is not None and "FMCSA_WEBKEY is not set" in run.status_detail
        candidates = await _candidates(session_factory, run)
        assert len(candidates) == 3
        for candidate in candidates:
            sources = {cited.value.source for cited in candidate.fields.source_records}
            assert "fmcsa_qcmobile" not in sources
        assert mock.calls_to("/carriers/") == []

    async def test_a_refused_key_mid_batch_stops_lookups_and_degrades(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _freight(session_factory)
        mock = MockFmcsa()
        mock.carriers["1000005"] = (401, {})
        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )
        assert run.status is RunStatus.degraded
        assert run.status_detail is not None and "refused the webKey" in run.status_detail
        # Batch order is 1000001, 1000005, 1000002: the second refused, the third never asked.
        assert [r.url.path.rsplit("/", 1)[1] for r in mock.calls_to("/carriers/")] == [
            "1000001",
            "1000005",
        ]
        assert run.counts["lookup_found"] == 1
        assert len(await _candidates(session_factory, run)) == 3


class TestRefusals:
    async def test_clashing_count_names_fail_the_run(self, session_factory: SessionFactory) -> None:
        vertical = await _freight(session_factory)
        pipeline = SourcingPipeline(
            session_factory,
            stages=[
                registry_stage(MockFmcsa()),
                Counts(PipelineStage.cluster_routes, {"batched": 1}),
            ],
        )
        with pytest.raises(ValueError, match="already reported"):
            await pipeline.run(a_brief(vertical))
        (failed,) = await _runs(session_factory, vertical)
        assert failed.status is RunStatus.failed

    async def test_no_active_manifest_opens_no_run(self, session_factory: SessionFactory) -> None:
        vertical = a_vertical()
        with pytest.raises(ActiveManifestNotFoundError):
            await SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa())]).run(
                a_brief(vertical)
            )
        assert await _runs(session_factory, vertical) == []

    async def test_an_empty_pull_completes_with_nothing_batched(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _freight(session_factory)
        mock = MockFmcsa(census=[])
        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )
        assert run.status is RunStatus.completed
        assert run.counts["batched"] == 0
        assert await _candidates(session_factory, run) == []
        assert mock.calls_to("wb4f-neki") == []


def test_the_stale_window_is_the_setting() -> None:
    from app.core.config import get_settings

    assert timedelta(hours=get_settings().sourcing_stale_run_hours) == timedelta(hours=6)
