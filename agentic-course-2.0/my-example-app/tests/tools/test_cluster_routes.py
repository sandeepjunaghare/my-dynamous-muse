"""The ``cluster_routes`` stage: registered under its name, and run through the stage contract."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.routing.service as routing_service
from app.core.cost import RunCost
from app.manifests.schemas import RuleKind
from app.manifests.service import ManifestService
from app.qualification.repository import QualificationRepository
from app.routing.geocoder import CensusGeocoder
from app.shared.provenance import RetrievalMethod
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.cluster_routes import STAGE
from app.tools.registry import StageContext, StageResult, registered_stages
from tests.conftest import requires_db
from tests.routing.builders import a_verified_candidate, street_for
from tests.routing.conftest import MockCensus, a_match, json_answer, load_fixture
from tests.sourcing.builders import a_brief, an_active_manifest


class TestRegistration:
    def test_the_stage_is_cluster_routes(self) -> None:
        assert STAGE.stage is PipelineStage.cluster_routes

    def test_the_real_registry_finds_it_once(self) -> None:
        stages = [stage.stage for stage in registered_stages()]

        assert stages.count(PipelineStage.cluster_routes) == 1


async def _run_stage(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    census: MockCensus,
    streets: list[str],
    *,
    disqualified: frozenset[int] = frozenset(),
) -> StageResult:
    """Record one verified candidate per street, then run the stage against ``census``.

    The candidates at the indexes in ``disqualified`` get a recorded disqualification first, the
    way the earlier stages would have left them.

    The stage builds its own geocoder; it is pointed at the mock, leaving the contract alone. The
    mock's geocoders are closed whatever the stage does.
    """

    def fake_geocoder(*args: object, **kwargs: object) -> CensusGeocoder:
        return census.geocoder()

    monkeypatch.setattr(routing_service, "CensusGeocoder", fake_geocoder)

    _, vertical = await an_active_manifest(session)
    run = await SourcingService(session).start_run(a_brief(vertical))
    recorded = await SourcingService(session).record_candidates(
        run.id,
        [a_verified_candidate(f"s{i}", street) for i, street in enumerate(streets)],
        stage=PipelineStage.search_registry,
    )
    manifest = await ManifestService(session).get_active(vertical)
    for candidate in recorded:
        if int(candidate.registry_id.removeprefix("s")) in disqualified:
            await QualificationRepository(session).record(
                candidate_id=candidate.id,
                run_id=run.id,
                manifest_id=manifest.id,
                vertical=vertical,
                registry_id=candidate.registry_id,
                rule_id="national_rollup_no_local_owner",
                rule_kind=RuleKind.judgment,
                source_url="https://example.gov/evidence",
                retrieval_method=RetrievalMethod.web_lookup,
                evidence=None,
            )

    try:
        return await STAGE.run(
            StageContext(run_id=run.id, manifest=manifest, session=session, cost=RunCost())
        )
    finally:
        await census.aclose()


@requires_db
class TestRun:
    async def test_running_the_stage_reports_the_routing_counts(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        for street in streets:
            census.on(street, json_answer(200, a_match(32.88, -96.71)))

        result = await _run_stage(db_session, monkeypatch, census, streets)

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

        result = await _run_stage(db_session, monkeypatch, census, streets)

        assert result.counts["routing_geocode_failed"] == 1
        assert result.counts["routing_clustered"] == 2
        assert result.degraded_reason == (
            "1 of 3 geocode(s) failed; those candidates are on no route"
        )

    async def test_unmatched_and_ambiguous_addresses_are_not_degraded(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The Census not knowing an address, or knowing several, is an answer, not a failure:
        # counted, never degraded.
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        census.on(streets[0], json_answer(200, load_fixture("census_no_match")))
        census.on(streets[1], json_answer(200, load_fixture("census_ambiguous")))
        census.on(streets[2], json_answer(200, a_match(32.88, -96.71)))

        result = await _run_stage(db_session, monkeypatch, census, streets)

        assert result.counts["routing_geocode_unmatched"] == 1
        assert result.counts["routing_geocode_ambiguous"] == 1
        assert result.counts["routing_geocode_failed"] == 0
        assert result.degraded_reason is None

    async def test_a_disqualified_candidate_is_on_no_route(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OQ8-B: the stage hands the run's disqualified candidates to routing to leave out."""
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        for street in streets:
            census.on(street, json_answer(200, a_match(32.88, -96.71)))

        result = await _run_stage(
            db_session, monkeypatch, census, streets, disqualified=frozenset({1})
        )

        assert result.counts["routing_disqualified"] == 1
        assert result.counts["routing_clustered"] == 2
        assert len(census.requests) == 2
        assert result.degraded_reason is None

    async def test_a_run_with_no_disqualifications_routes_everyone(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Paired with the exclusion above: nothing disqualified, nothing left out."""
        census = MockCensus()
        streets = [street_for(f"s{i}") for i in range(3)]
        for street in streets:
            census.on(street, json_answer(200, a_match(32.88, -96.71)))

        result = await _run_stage(db_session, monkeypatch, census, streets)

        assert result.counts["routing_disqualified"] == 0
        assert result.counts["routing_clustered"] == 3
