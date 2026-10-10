"""``CensusGeocoder``: what it asks, how it reads the answer, and how it fails. Fixtures only."""

import httpx
import pytest

from app.routing.exceptions import GeocoderResponseShapeError, GeocoderUnavailableError
from app.routing.geocoder import CensusGeocoder
from app.routing.schemas import GeocodeMatchStatus
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import PostalAddress
from tests.routing.conftest import (
    Answer,
    MockCensus,
    a_match,
    json_answer,
    load_fixture,
    text_answer,
    transport_error,
)

STREET = "9500 Forest Ln"
ADDRESS = PostalAddress(street=STREET, city="Dallas", state="TX", postal_code="75238")


class TestGeocode:
    async def test_one_match_is_a_cited_point_with_x_as_longitude(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(200, load_fixture("census_match")))

        async with census.geocoder() as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.matched
        assert outcome.point is not None
        assert outcome.point.value.latitude == pytest.approx(32.8839)
        assert outcome.point.value.longitude == pytest.approx(-96.7164)
        assert outcome.point.retrieval_method is RetrievalMethod.web_lookup
        assert httpx.URL(outcome.point.source_url).host == "geocoding.geo.census.gov"

    async def test_the_request_carries_the_address_and_benchmark(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(200, load_fixture("census_match")))

        async with census.geocoder() as geocoder:
            await geocoder.geocode(ADDRESS)

        (request,) = census.requests
        assert dict(request.url.params) == {
            "street": STREET,
            "city": "Dallas",
            "state": "TX",
            "zip": "75238",
            "benchmark": "Public_AR_Current",
            "format": "json",
        }

    async def test_no_match_is_unmatched(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(200, load_fixture("census_no_match")))

        async with census.geocoder() as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.unmatched
        assert outcome.point is None

    async def test_several_matches_are_ambiguous_not_a_pick(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(200, load_fixture("census_ambiguous")))

        async with census.geocoder() as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.ambiguous
        assert outcome.point is None


class TestFailures:
    async def test_a_server_error_is_retried(self) -> None:
        census = MockCensus()
        census.on(
            STREET,
            json_answer(503, {}),
            json_answer(503, {}),
            json_answer(200, load_fixture("census_match")),
        )
        slept: list[float] = []

        async def record_sleep(seconds: float) -> None:
            slept.append(seconds)

        transport = httpx.MockTransport(census.handle)
        async with CensusGeocoder(transport=transport, sleep=record_sleep) as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.matched
        assert len(census.requests) == 3
        assert len(slept) == 2

    async def test_persistent_server_errors_raise_unavailable(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(503, {}))

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderUnavailableError) as raised:
                await geocoder.geocode(ADDRESS)

        assert raised.value.status == 503
        assert len(census.requests) == 3

    async def test_a_client_error_is_not_retried(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(400, {"errors": ["bad request"]}))

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderUnavailableError) as raised:
                await geocoder.geocode(ADDRESS)

        assert raised.value.status == 400
        assert len(census.requests) == 1

    async def test_a_dropped_connection_raises_unavailable_after_retries(self) -> None:
        census = MockCensus()
        census.on(STREET, transport_error())

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderUnavailableError) as raised:
                await geocoder.geocode(ADDRESS)

        assert raised.value.status is None
        assert len(census.requests) == 3

    @pytest.mark.parametrize(
        "answer",
        [json_answer(200, load_fixture("census_bad_shape")), text_answer(200, "<html>oops</html>")],
        ids=["wrong-shape", "not-json"],
    )
    async def test_an_unreadable_body_raises_shape_error(self, answer: Answer) -> None:
        census = MockCensus()
        census.on(STREET, answer)

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderResponseShapeError):
                await geocoder.geocode(ADDRESS)

    async def test_an_unscripted_request_fails_the_test(self) -> None:
        census = MockCensus()

        async with census.geocoder() as geocoder:
            with pytest.raises(AssertionError, match="no answer"):
                await geocoder.geocode(ADDRESS)


class TestReviewFindings:
    """PR #19 review: M2 (escaping exceptions) and L5 (untested retry paths)."""

    async def test_a_corrupt_body_is_retried_then_unavailable(self) -> None:
        def corrupt(request: httpx.Request) -> httpx.Response:
            raise httpx.DecodingError("truncated gzip body", request=request)

        census = MockCensus()
        census.on(STREET, corrupt)

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderUnavailableError):
                await geocoder.geocode(ADDRESS)

        assert len(census.requests) == 3

    async def test_a_coordinate_out_of_range_is_a_shape_error(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(200, a_match(latitude=123.0, longitude=-96.7)))

        async with census.geocoder() as geocoder:
            with pytest.raises(GeocoderResponseShapeError):
                await geocoder.geocode(ADDRESS)

    async def test_a_rate_limit_is_retried(self) -> None:
        census = MockCensus()
        census.on(STREET, json_answer(429, {}), json_answer(200, load_fixture("census_match")))

        async with census.geocoder() as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.matched
        assert len(census.requests) == 2

    async def test_a_dropped_connection_then_success_matches(self) -> None:
        census = MockCensus()
        census.on(STREET, transport_error(), json_answer(200, load_fixture("census_match")))

        async with census.geocoder() as geocoder:
            outcome = await geocoder.geocode(ADDRESS)

        assert outcome.status is GeocodeMatchStatus.matched
        assert len(census.requests) == 2
