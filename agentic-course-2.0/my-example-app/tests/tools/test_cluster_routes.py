"""The ``cluster_routes`` stage: registered under its name, and run through the stage contract."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.routing.service as routing_service
from app.core.cost import RunCost
from app.manifests.service import ManifestService
from app.routing.geocoder import CensusGeocoder
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.cluster_routes import STAGE
from app.tools.registry import StageContext, registered_stages
from tests.conftest import requires_db
from tests.routing.builders import a_verified_candidate, record, street_for
from tests.routing.conftest import MockCensus, a_match, json_answer, load_fixture
from tests.sourcing.builders import a_brief, an_active_manifest


class TestRegistration:
    def test_the_stage_is_cluster_routes(self) -> None:
        assert STAGE.stage is PipelineStage.cluster_routes

    def test_the_real_registry_finds_it_once(self) -> None:
        stages = [stage.stage for stage in registered_stages()]

        assert stages.count(PipelineStage.cluster_routes) == 1


@requires_db
class TestRun:
    async def test_running_the_stage_reports_the_routing_counts(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        for street in streets:
            census.on(street, json_answer(200, a_match(32.88, -96.71)))

        # The stage builds its own geocoder; point that at the mock, leaving the contract alone.
        def fake_geocoder(*args: object, **kwargs: object) -> CensusGeocoder:
            return census.geocoder()

        monkeypatch.setattr(routing_service, "CensusGeocoder", fake_geocoder)

        _, vertical = await an_active_manifest(db_session)
        run = await SourcingService(db_session).start_run(a_brief(vertical))
        await record(
            db_session,
            run.id,
            [a_verified_candidate(f"s{i}", street) for i, street in enumerate(streets)],
        )
        manifest = await ManifestService(db_session).get_active(vertical)

        try:
            result = await STAGE.run(
                StageContext(run_id=run.id, manifest=manifest, session=db_session, cost=RunCost())
            )
        finally:
            await census.aclose()

        assert result.counts["routing_clustered"] == 3
        assert result.counts["routing_clusters"] == 1
        assert result.degraded_reason is None
        assert len(census.requests) == 3

    async def test_a_failed_geocode_finishes_the_stage_degraded(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One door missing from the routes is not a clean run: the runner must say so.
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        census.on(streets[0], json_answer(503, {}))
        for street in streets[1:]:
            census.on(street, json_answer(200, a_match(32.88, -96.71)))

        def fake_geocoder(*args: object, **kwargs: object) -> CensusGeocoder:
            return census.geocoder()

        monkeypatch.setattr(routing_service, "CensusGeocoder", fake_geocoder)

        _, vertical = await an_active_manifest(db_session)
        run = await SourcingService(db_session).start_run(a_brief(vertical))
        await record(
            db_session,
            run.id,
            [a_verified_candidate(f"s{i}", street) for i, street in enumerate(streets)],
        )
        manifest = await ManifestService(db_session).get_active(vertical)

        try:
            result = await STAGE.run(
                StageContext(run_id=run.id, manifest=manifest, session=db_session, cost=RunCost())
            )
        finally:
            await census.aclose()

        assert result.counts["routing_geocode_failed"] == 1
        assert result.counts["routing_clustered"] == 2
        assert result.degraded_reason == (
            "1 of 3 geocode(s) failed; those candidates are on no route"
        )

    async def test_an_unmatched_address_is_not_degraded(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Census not knowing an address is an answer, not a failure: counted, never degraded.
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(2)]
        census.on(streets[0], json_answer(200, load_fixture("census_no_match")))
        census.on(streets[1], json_answer(200, a_match(32.88, -96.71)))

        def fake_geocoder(*args: object, **kwargs: object) -> CensusGeocoder:
            return census.geocoder()

        monkeypatch.setattr(routing_service, "CensusGeocoder", fake_geocoder)

        _, vertical = await an_active_manifest(db_session)
        run = await SourcingService(db_session).start_run(a_brief(vertical))
        await record(
            db_session,
            run.id,
            [a_verified_candidate(f"s{i}", street) for i, street in enumerate(streets)],
        )
        manifest = await ManifestService(db_session).get_active(vertical)

        try:
            result = await STAGE.run(
                StageContext(run_id=run.id, manifest=manifest, session=db_session, cost=RunCost())
            )
        finally:
            await census.aclose()

        assert result.counts["routing_geocode_unmatched"] == 1
        assert result.degraded_reason is None
