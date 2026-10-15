"""``RoutingService.cluster_run`` end to end: real Postgres, a mock Census Geocoder.

``db_session`` joins an outer transaction with ``create_savepoint``, so the service's ``commit()``
is real and still rolled back at the end of each test.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.exceptions import GeocoderResponseShapeError, GeocoderUnavailableError
from app.routing.schemas import RouteCluster
from app.routing.service import RoutingService
from app.sourcing.exceptions import SourcingRunNotFoundError
from app.sourcing.schemas import CandidateFields, MissKind, PostalAddress
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.routing.builders import (
    LAKE_HIGHLANDS,
    a_running_run,
    a_verified_candidate,
    around,
    record,
    street_for,
    two_neighbourhoods,
)
from tests.routing.conftest import MockCensus, a_match, json_answer, load_fixture
from tests.sourcing.builders import a_verdict, sourced, website_verified

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
    async def test_the_e4_week_becomes_two_routes_of_ten(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)

        assert [c.label for c in result.clusters] == ["A · 76011", "B · 75238"]
        assert [len(c.members) for c in result.clusters] == [10, 10]
        assert result.counts == {
            "routing_disqualified": 0,
            "routing_eligible": 20,
            "routing_mail_drop": 0,
            "routing_unverified_address": 0,
            "routing_geocode_unmatched": 0,
            "routing_geocode_ambiguous": 0,
            "routing_geocode_failed": 0,
            "routing_clustered": 20,
            "routing_clusters": 2,
        }

    async def test_assignments_are_persisted_with_their_cited_point(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        service = await _cluster(db_session, run_id, census)
        rows = await service.list_assignments(run_id)

        assert len(rows) == 20
        assert {r.cluster_label for r in rows} == {"A · 76011", "B · 75238"}
        assert all(r.point.source_url.startswith("https://geocoding.geo.census.gov/") for r in rows)
        assert [r.cluster_index for r in rows] == sorted(r.cluster_index for r in rows)

    async def test_an_unverified_address_is_never_geocoded(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
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

    async def test_misses_are_left_out_and_counted(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
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
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
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

    async def test_a_rerun_replaces_the_runs_routes(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        service = await _cluster(db_session, run_id, census)
        first = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]
        await service.cluster_run(run_id)
        second = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]

        assert len(second) == 20
        assert second == first

    async def test_the_same_candidates_in_two_runs_get_the_same_routes(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
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
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, [CandidateFields(registry_id=sourced("bare"))])

        result = await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(run_id)

        assert result.clusters == ()
        assert result.counts["routing_clusters"] == 0
        assert census.requests == []

    async def test_an_unknown_run_is_refused(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(uuid4())


def _website_only(
    registry_id: str, verdict: MissKind | None = None, *, old: bool = False
) -> CandidateFields:
    """A candidate verified by its homepage only, optionally with a Places verdict."""
    street = street_for(registry_id)
    address = PostalAddress(street=street, city="Dallas", state="TX", postal_code="75238")
    checked = (
        PostalAddress(street="900 Main St", city="Fort Worth", state="TX", postal_code="76102")
        if old
        else address
    )
    fields = website_verified(
        CandidateFields(
            registry_id=sourced(registry_id),
            legal_name=sourced(f"Acme Logistics {registry_id} LLC"),
            address=sourced(address),
        )
    )
    if verdict is None:
        return fields
    return fields.model_copy(update={"places_verdict": a_verdict(verdict, address=checked)})


class TestWebsiteVerified:
    """Website-verification OQ-3: a website verifies the business; a mail drop is no door."""

    async def _run(
        self, session: AsyncSession, census: MockCensus, candidate: CandidateFields
    ) -> dict[str, int]:
        census.on(
            candidate.address.value.street if candidate.address else "",
            json_answer(200, a_match(32.87, -96.71)),
        )
        run_id = await a_running_run(session)
        await record(session, run_id, [candidate])
        result = await RoutingService(
            session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)
        return result.counts

    async def test_with_no_verdict_it_is_routed(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        counts = await self._run(db_session, census, _website_only("w1"))
        assert counts["routing_clustered"] == 1

    @pytest.mark.parametrize("kind", [MissKind.no_listing, MissKind.other_business])
    async def test_with_a_door_verdict_it_is_routed(
        self, db_session: AsyncSession, census: MockCensus, kind: MissKind
    ) -> None:
        counts = await self._run(db_session, census, _website_only("w2", kind))
        assert counts["routing_clustered"] == 1

    async def test_at_a_mail_drop_it_is_counted_and_never_geocoded(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        counts = await self._run(db_session, census, _website_only("w3", MissKind.mail_drop))
        assert counts["routing_mail_drop"] == 1
        assert counts["routing_unverified_address"] == 0
        assert counts["routing_eligible"] == 0
        assert census.requests == []

    async def test_a_mail_drop_at_an_old_address_does_not_block(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        counts = await self._run(
            db_session, census, _website_only("w4", MissKind.mail_drop, old=True)
        )
        assert counts["routing_clustered"] == 1
        assert counts["routing_mail_drop"] == 0


class TestExcludedCandidates:
    """OQ8-B: routes are built from the survivors — a disqualified door is on no route."""

    async def test_an_excluded_candidate_is_never_geocoded_and_is_counted(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))
        excluded = [
            c
            for c in await SourcingService(db_session).list_candidates(run_id)
            if c.registry_id in {"oly000", "lkh000"}
        ]

        service = RoutingService(db_session, geocoder=census.geocoder(), max_doors=12)
        result = await service.cluster_run(run_id, exclude=frozenset(c.id for c in excluded))

        assert result.counts["routing_disqualified"] == 2
        assert result.counts["routing_eligible"] == 18
        assert result.counts["routing_clustered"] == 18
        assert len(census.requests) == 18
        assert not {"oly000 Commerce St", "lkh000 Commerce St"} & set(census.streets_requested())
        routed = {row.candidate_id for row in await service.list_assignments(run_id)}
        assert len(routed) == 18
        assert routed.isdisjoint(c.id for c in excluded)

    async def test_routes_balance_over_the_survivors(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        """Four disqualified doors would make 24, two routes of 12; without them it is 10 + 10."""
        run_id = await a_running_run(db_session)
        doomed: list[CandidateFields] = []
        for point in around(LAKE_HIGHLANDS, "dq", 4, "75238"):
            street = street_for(point.registry_id)
            census.on(
                street, json_answer(200, a_match(point.point.latitude, point.point.longitude))
            )
            doomed.append(a_verified_candidate(point.registry_id, street, point.postal_code))
        await record(db_session, run_id, [*_week(census), *doomed])
        excluded = frozenset(
            c.id
            for c in await SourcingService(db_session).list_candidates(run_id)
            if c.registry_id.startswith("dq")
        )

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id, exclude=excluded)

        assert [len(c.members) for c in result.clusters] == [10, 10]
        assert result.counts["routing_disqualified"] == 4

    async def test_unverified_addresses_are_counted_over_the_survivors_only(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        """An excluded candidate is counted once, as disqualified — never also as unverified."""
        run_id = await a_running_run(db_session)
        bare = [CandidateFields(registry_id=sourced(f"bare{i}")) for i in range(3)]
        await record(db_session, run_id, [*_week(census), *bare])
        excluded = frozenset(
            c.id
            for c in await SourcingService(db_session).list_candidates(run_id)
            if c.registry_id in {"bare0", "oly001"}
        )

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id, exclude=excluded)

        assert result.counts["routing_disqualified"] == 2
        assert result.counts["routing_unverified_address"] == 2
        assert result.counts["routing_eligible"] == 19

    async def test_an_id_from_outside_the_run_counts_for_nothing(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        await record(db_session, run_id, _week(census))

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id, exclude=frozenset({uuid4()}))

        assert result.counts["routing_disqualified"] == 0
        assert result.counts["routing_clustered"] == 20


class TestEveryGeocodeFailing:
    """PR #19 review, M1 and M3: an all-failed pass raises, whatever the mix, and erases nothing."""

    async def _run_of(
        self, db_session: AsyncSession, census: MockCensus, answers: list[int | str]
    ) -> UUID:
        """A run whose geocodes all fail: an int answers that status, ``"bad"`` a bad body."""
        run_id = await a_running_run(db_session)
        candidates: list[CandidateFields] = []
        for i, answer in enumerate(answers):
            street = street_for(f"fail{i}")
            if answer == "bad":
                census.on(street, json_answer(200, load_fixture("census_bad_shape")))
            else:
                census.on(street, json_answer(int(answer), {}))
            candidates.append(a_verified_candidate(f"fail{i}", street))
        await record(db_session, run_id, candidates)
        return run_id

    async def test_every_body_unreadable_raises(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await self._run_of(db_session, census, ["bad", "bad"])

        with pytest.raises(GeocoderResponseShapeError):
            await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(run_id)

    async def test_a_mix_of_failures_raises(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await self._run_of(db_session, census, [503, "bad", 503])

        with pytest.raises((GeocoderUnavailableError, GeocoderResponseShapeError)):
            await RoutingService(db_session, geocoder=census.geocoder()).cluster_run(run_id)

    async def test_an_unreadable_body_alone_counts_as_failed(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        census.on("1 Garbled Way", json_answer(200, load_fixture("census_bad_shape")))
        await record(
            db_session,
            run_id,
            [*_week(census), a_verified_candidate("garbled", "1 Garbled Way")],
        )

        result = await RoutingService(
            db_session, geocoder=census.geocoder(), max_doors=12
        ).cluster_run(run_id)

        assert result.counts["routing_geocode_failed"] == 1
        assert result.counts["routing_clustered"] == 20

    async def test_a_failed_rerun_keeps_the_earlier_routes(
        self, db_session: AsyncSession, census: MockCensus
    ) -> None:
        run_id = await a_running_run(db_session)
        week = _week(census)
        await record(db_session, run_id, week)
        service = RoutingService(db_session, geocoder=census.geocoder(), max_doors=12)
        await service.cluster_run(run_id)
        before = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]

        for candidate in week:
            assert candidate.address is not None
            census.on(candidate.address.value.street, json_answer(200, {"result": {}}))
        with pytest.raises(GeocoderResponseShapeError):
            await service.cluster_run(run_id)

        after = [(r.candidate_id, r.cluster_label) for r in await service.list_assignments(run_id)]
        assert after == before
        assert len(after) == 20
