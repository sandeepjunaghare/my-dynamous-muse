"""``route_assignment`` and its repository, against a real Postgres: round trip and constraints."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.models import RouteAssignment
from app.routing.repository import RoutingRepository
from app.routing.schemas import GeoPoint, RouteCluster, RoutePoint
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.routing.builders import a_running_run, a_verified_candidate, record

pytestmark = requires_db

RETRIEVED_AT = datetime(2026, 10, 10, 15, 0, 0, 123456, tzinfo=UTC)


def _cited(latitude: float, longitude: float) -> ProvenancedValue[GeoPoint]:
    return ProvenancedValue(
        value=GeoPoint(latitude=latitude, longitude=longitude),
        source_url="https://geocoding.geo.census.gov/geocoder/locations/address?street=x",
        retrieved_at=RETRIEVED_AT,
        retrieval_method=RetrievalMethod.web_lookup,
    )


async def _one_routed(session: AsyncSession) -> tuple[UUID, UUID, RouteCluster]:
    """A run with one candidate, and a one-door route for it."""
    run_id = await a_running_run(session)
    await record(session, run_id, [a_verified_candidate("r1", "1 Elm St")])
    (candidate,) = await SourcingService(session).list_candidates(run_id)
    member = RoutePoint(
        candidate_id=candidate.id,
        registry_id=candidate.registry_id,
        point=GeoPoint(latitude=32.88, longitude=-96.71),
        postal_code="75238",
    )
    cluster = RouteCluster(
        index=0, label="A · 75238", anchor_postal_code="75238", members=(member,)
    )
    return run_id, candidate.id, cluster


class TestRoutingRepository:
    async def test_the_point_and_its_citation_survive_the_round_trip(
        self, db_session: AsyncSession
    ) -> None:
        run_id, candidate_id, cluster = await _one_routed(db_session)
        repository = RoutingRepository(db_session)

        await repository.replace_run_assignments(
            run_id, [cluster], {candidate_id: _cited(32.88, -96.71)}
        )
        (row,) = await repository.list_run_assignments(run_id)

        assert ProvenancedValue[GeoPoint].model_validate(row.point) == _cited(32.88, -96.71)
        assert (row.cluster_index, row.cluster_label) == (0, "A · 75238")

    async def test_replacing_twice_leaves_one_set_of_rows(self, db_session: AsyncSession) -> None:
        run_id, candidate_id, cluster = await _one_routed(db_session)
        repository = RoutingRepository(db_session)
        points = {candidate_id: _cited(32.88, -96.71)}

        await repository.replace_run_assignments(run_id, [cluster], points)
        await repository.replace_run_assignments(run_id, [cluster], points)

        assert len(await repository.list_run_assignments(run_id)) == 1

    async def test_replacing_with_nothing_clears_the_run(self, db_session: AsyncSession) -> None:
        run_id, candidate_id, cluster = await _one_routed(db_session)
        repository = RoutingRepository(db_session)

        await repository.replace_run_assignments(
            run_id, [cluster], {candidate_id: _cited(32.88, -96.71)}
        )
        await repository.replace_run_assignments(run_id, [], {})

        assert await repository.list_run_assignments(run_id) == []

    async def test_a_candidate_is_on_at_most_one_route(self, db_session: AsyncSession) -> None:
        run_id, candidate_id, _ = await _one_routed(db_session)
        point = _cited(32.88, -96.71).model_dump(mode="json")
        for index in (0, 1):
            db_session.add(
                RouteAssignment(
                    run_id=run_id,
                    candidate_id=candidate_id,
                    cluster_index=index,
                    cluster_label="A · 75238",
                    point=point,
                )
            )

        with pytest.raises(IntegrityError, match="uq_route_assignment_candidate_id"):
            await db_session.flush()

    async def test_a_negative_route_index_is_refused(self, db_session: AsyncSession) -> None:
        run_id, candidate_id, _ = await _one_routed(db_session)
        db_session.add(
            RouteAssignment(
                run_id=run_id,
                candidate_id=candidate_id,
                cluster_index=-1,
                cluster_label="A · 75238",
                point=_cited(32.88, -96.71).model_dump(mode="json"),
            )
        )

        with pytest.raises(IntegrityError, match="ck_route_assignment_cluster_index_non_negative"):
            await db_session.flush()
