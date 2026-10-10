"""``RoutingService.cluster_run`` end to end: real Postgres, a mock Census Geocoder.

``db_session`` joins an outer transaction with ``create_savepoint``, so the service's ``commit()``
is real and still rolled back at the end of each test.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.exceptions import GeocoderUnavailableError
from app.routing.schemas import RouteCluster
from app.routing.service import RoutingService
from app.sourcing.exceptions import SourcingRunNotFoundError
from app.sourcing.schemas import CandidateFields, PostalAddress
from tests.conftest import requires_db
from tests.routing.builders import (
    a_running_run,
    a_verified_candidate,
    record,
    street_for,
    two_neighbourhoods,
)
from tests.routing.conftest import MockCensus, a_match, json_answer, load_fixture
from tests.sourcing.builders import sourced

pytestmark = requires_db


def _week(census: MockCensus) -> list[CandidateFields]:
    """The E4 week as candidates, with the mock geocoder scripted to place each one."""
    olympic, lake_highlands = two_neighbourhoods()
    candidates: list[CandidateFields] = []
    for point in olympic + lake_highlands:
        street = street_for(point.registry_id)
        census.on(
            street,
            json_answer(200, a_match(point.point.latitude, point.point.longitude)),
        )
        candidates.append(a_verified_candidate(point.registry_id, street, point.postal_code))
    return candidates


def _ids_by_label(clusters: tuple[RouteCluster, ...]) -> dict[str, list[str]]:
    return {cluster.label: [m.registry_id for m in cluster.members] for cluster in clusters}


async def _cluster(session: AsyncSession, run_id: UUID, census: MockCensus) -> RoutingService:
    service = RoutingService(session, geocoder=census.geocoder(), max_doors=12)
    await service.cluster_run(run_id)
    return service


class TestClusterRun:
    async def test_the_e4_week_becomes_two_routes_of_ten(self, db_session: AsyncSession) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)

        assert [c.label for c in result.clusters] == ["A · 76011", "B · 75238"]
        assert [len(c.members) for c in result.clusters] == [10, 10]
        assert result.counts == {
            "routing_eligible": 20,
            "routing_unverified_address": 0,
            "routing_geocode_unmatched": 0,
            "routing_geocode_ambiguous": 0,
            "routing_geocode_failed": 0,
            "routing_clustered": 20,
            "routing_clusters": 2,
        }

    async def test_assignments_are_persisted_with_their_cited_point(
        self, db_session: AsyncSession
    ) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        service = await _cluster(db_session, run_id, census)
        rows = await service.list_assignments(run_id)

        assert len(rows) == 20
        assert {r.cluster_label for r in rows} == {"A · 76011", "B · 75238"}
        assert all(r.point.source_url.startswith("https://geocoding.geo.census.gov/") for r in rows)
        assert [r.cluster_index for r in rows] == sorted(r.cluster_index for r in rows)

    async def test_an_unverified_address_is_never_geocoded(self, db_session: AsyncSession) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        week = _week(census)
        no_check = CandidateFields(
            registry_id=sourced("unchecked"),
            address=sourced(
                PostalAddress(street="1 Unchecked Way", city="Dallas", state="TX", postal_code="1")
            ),
        )
        no_address = CandidateFields(registry_id=sourced("addressless"))
        await record(db_session, run_id, [*week, no_check, no_address])

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)

        assert result.counts["routing_unverified_address"] == 2
        assert result.counts["routing_clustered"] == 20
        assert "1 Unchecked Way" not in census.streets_requested()
        assert len(census.requests) == 20

    async def test_misses_are_left_out_and_counted(self, db_session: AsyncSession) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        census.on("1 Nowhere Rd", json_answer(200, load_fixture("census_no_match")))
        census.on("100 Main St", json_answer(200, load_fixture("census_ambiguous")))
        census.on("9 Flaky Ave", json_answer(503, {}))
        await record(
            db_session,
            run_id,
            [
                *_week(census),
                a_verified_candidate("miss", "1 Nowhere Rd"),
                a_verified_candidate("twice", "100 Main St"),
                a_verified_candidate("flaky", "9 Flaky Ave"),
            ],
        )

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)

        assert result.counts["routing_eligible"] == 23
        assert result.counts["routing_geocode_unmatched"] == 1
        assert result.counts["routing_geocode_ambiguous"] == 1
        assert result.counts["routing_geocode_failed"] == 1
        assert result.counts["routing_clustered"] == 20
        routed = {m.registry_id for c in result.clusters for m in c.members}
        assert not routed & {"miss", "twice", "flaky"}

    async def test_the_geocoder_down_for_everyone_is_an_error(
        self, db_session: AsyncSession
    ) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        streets = [street_for(f"down{i}") for i in range(3)]
        for street in streets:
            census.on(street, json_answer(503, {}))
        await record(
            db_session,
            run_id,
            [a_verified_candidate(f"down{i}", street) for i, street in enumerate(streets)],
        )

        with pytest.raises(GeocoderUnavailableError):
            await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(run_id)

    async def test_a_rerun_replaces_the_runs_routes(self, db_session: AsyncSession) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        service = await _cluster(db_session, run_id, census)
        first = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]
        await service.cluster_run(run_id)
        second = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]

        assert len(second) == 20
        assert second == first

    async def test_the_same_candidates_in_two_runs_get_the_same_routes(
        self, db_session: AsyncSession
    ) -> None:
        census = MockCensus()
        week = _week(census)
        run_a = await a_running_run(db_session)
        run_b = await a_running_run(db_session)
        await record(db_session, run_a, week)
        await record(db_session, run_b, list(reversed(week)))

        service = RoutingService(db_session, geocoder=census.geocoder(), max_doors=12)
        result_a = await service.cluster_run(run_a)
        result_b = await service.cluster_run(run_b)

        assert _ids_by_label(result_a.clusters) == _ids_by_label(result_b.clusters)

    async def test_no_eligible_candidates_is_no_routes_and_no_calls(
        self, db_session: AsyncSession
    ) -> None:
        census = MockCensus()
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, [CandidateFields(registry_id=sourced("bare"))])

        result = await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(run_id)

        assert result.clusters == ()
        assert result.counts["routing_clusters"] == 0
        assert census.requests == []

    async def test_an_unknown_run_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await RoutingService(db_session, geocoder=MockCensus().geocoder()).cluster_run(uuid4())
