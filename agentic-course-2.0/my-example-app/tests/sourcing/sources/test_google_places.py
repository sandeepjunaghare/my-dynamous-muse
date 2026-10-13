"""The Places check's client, matcher and binding — against recorded-shape fixtures, no network.

Fixture shapes follow the Text Search (New) reference and the invalid-key body read live on
2026-10-11; names and addresses are made up.
"""

import json
from typing import cast

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.manifests.schemas import SourceKind
from app.shared.provenance import RetrievalMethod
from app.sourcing.exceptions import SourceAuthError, SourceRequestError
from app.sourcing.schemas import CandidateFields, PostalAddress, is_google_maps
from app.sourcing.sources.fmcsa import FMCSA_ADAPTERS
from app.sourcing.sources.google_places import (
    CLOSED_PERMANENTLY,
    PLACES_FIELD_MASK,
    PLACES_SEARCH_URL,
    PlaceResult,
    PlacesClient,
    declared_places_source,
    match_place,
    place_url,
    places_unavailable,
    search_query,
)
from tests.manifests.builders import a_body
from tests.sourcing.builders import (
    CENSUS_SOURCE_URL,
    PLACES_SOURCE_URL,
    a_candidate,
    a_fire_body,
    a_freight_body,
    a_source,
    sourced,
)
from tests.sourcing.conftest import TEST_PLACES_KEY, MockPlaces, a_settings, load_fixture

FREIGHT_V9_PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
"""What freight v9 (DRAFT ``e0d32bec``) declares."""


async def _search(
    mock: MockPlaces, query: str = "Acme Freight, 2100 Olympic Dr"
) -> tuple[PlaceResult, ...]:
    async with httpx.AsyncClient(transport=mock.transport()) as client:
        places = PlacesClient(
            client, api_key=TEST_PLACES_KEY, source="places", backoff_seconds=0.0, throttle=False
        )
        return await places.search_text(query)


class TestTheRequest:
    async def test_it_carries_the_mask_the_key_in_a_header_and_page_size_five(self) -> None:
        mock = MockPlaces()
        await _search(mock)
        (request,) = mock.requests
        assert str(request.url) == PLACES_SEARCH_URL
        assert request.headers["X-Goog-FieldMask"] == ",".join(PLACES_FIELD_MASK)
        assert request.headers["X-Goog-Api-Key"] == TEST_PLACES_KEY
        assert TEST_PLACES_KEY not in str(request.url)
        body = cast(dict[str, object], json.loads(request.content))
        assert body == {"textQuery": "Acme Freight, 2100 Olympic Dr", "pageSize": 5}

    async def test_the_key_never_reaches_a_log_line(self) -> None:
        with capture_logs() as logs:
            await _search(MockPlaces())
        assert TEST_PLACES_KEY not in str(logs)

    @pytest.mark.parametrize(
        "field",
        [
            "places.websiteUri",
            "places.nationalPhoneNumber",
            "places.internationalPhoneNumber",
            "places.reviews",
            "places.rating",
            "places.userRatingCount",
            "places.regularOpeningHours",
        ],
    )
    def test_no_enterprise_field_is_ever_requested(self, field: str) -> None:
        assert field not in PLACES_FIELD_MASK


class TestTheResponse:
    async def test_results_parse_in_googles_order(self) -> None:
        mock = MockPlaces(default=(200, load_fixture("places_second_matches")))
        results = await _search(mock)
        assert [result.id for result in results] == ["ChIJ-neighbour", "ChIJ-acme-second"]
        second = results[1]
        assert second.display_name == "ACME FREIGHT"
        assert second.business_status == "OPERATIONAL"

    async def test_an_empty_answer_is_no_results(self) -> None:
        assert await _search(MockPlaces(default=(200, load_fixture("places_empty")))) == ()

    async def test_a_bad_key_is_an_auth_error(self) -> None:
        """Verified live: a bad key is a 400 whose reason is API_KEY_INVALID, not a 401."""
        mock = MockPlaces(default=(400, load_fixture("places_bad_key")))
        with pytest.raises(SourceAuthError):
            await _search(mock)

    async def test_a_403_is_an_auth_error(self) -> None:
        mock = MockPlaces(default=(403, {"error": {"status": "PERMISSION_DENIED"}}))
        with pytest.raises(SourceAuthError):
            await _search(mock)

    async def test_any_other_400_is_a_request_error(self) -> None:
        body = {"error": {"status": "INVALID_ARGUMENT", "details": [{"reason": "BAD_QUERY"}]}}
        mock = MockPlaces(default=(400, body))
        with pytest.raises(SourceRequestError) as raised:
            await _search(mock)
        assert not isinstance(raised.value, SourceAuthError)

    async def test_a_5xx_is_retried(self) -> None:
        answers: list[tuple[int, object]] = [(503, {}), (200, load_fixture("places_match"))]

        def handler(request: httpx.Request) -> httpx.Response:
            status, body = answers.pop(0)
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            places = PlacesClient(
                client, api_key=TEST_PLACES_KEY, source="places", backoff_seconds=0.0
            )
            results = await places.search_text("q")
        assert len(results) == 1
        assert answers == []

    async def test_a_body_that_is_not_json_is_a_request_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>proxy</html>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            places = PlacesClient(client, api_key=TEST_PLACES_KEY, source="places")
            with pytest.raises(SourceRequestError):
                await places.search_text("q")


class TestTheCitation:
    def test_the_place_url_is_maps_so_only_business_check_may_cite_it(self) -> None:
        url = place_url("ChIJ-x")
        assert url == "https://places.googleapis.com/v1/places/ChIJ-x"
        assert is_google_maps(url)
        assert TEST_PLACES_KEY not in url


class TestBinding:
    """A3: Places is called only for a manifest that declares it (terms accepted at activate)."""

    def test_the_fire_seeds_docs_url_binds(self) -> None:
        assert declared_places_source(a_fire_body()) == "places"

    def test_freight_v9s_api_url_binds(self) -> None:
        body = a_freight_body()
        body = body.model_copy(
            update={
                "sources": (
                    *body.sources,
                    a_source("places", SourceKind.web_lookup, FREIGHT_V9_PLACES_URL),
                )
            }
        )
        assert declared_places_source(body) == "places"

    def test_a_census_only_manifest_declares_none(self) -> None:
        assert declared_places_source(a_freight_body()) is None
        assert declared_places_source(a_body()) is None

    def test_the_first_declared_wins(self) -> None:
        body = a_body().model_copy(
            update={
                "sources": (
                    a_source("census", SourceKind.bulk_file, CENSUS_SOURCE_URL),
                    a_source("places_docs", SourceKind.web_lookup, PLACES_SOURCE_URL),
                    a_source("places_api", SourceKind.web_lookup, FREIGHT_V9_PLACES_URL),
                )
            }
        )
        assert declared_places_source(body) == "places_docs"

    def test_no_registry_adapter_ever_binds_places(self) -> None:
        """Not a LookupSource: a lookup's answer is stored, and storing Places content is D13."""
        for url in (PLACES_SOURCE_URL, FREIGHT_V9_PLACES_URL):
            source = a_source("places", SourceKind.web_lookup, url).value
            assert not any(factory.matches(source) for factory in FMCSA_ADAPTERS)

    @pytest.mark.parametrize("key", [None, "", "   "])
    def test_no_key_or_a_blank_one_is_unavailable(self, key: str | None) -> None:
        settings = a_settings(google_places_api_key=SecretStr(key) if key is not None else None)
        assert places_unavailable(settings) == "GOOGLE_PLACES_API_KEY is not set"

    def test_a_key_is_available(self) -> None:
        settings = a_settings(google_places_api_key=SecretStr(TEST_PLACES_KEY))
        assert places_unavailable(settings) is None


def _result(
    name: str | None = "Acme Freight",
    address: str | None = "2100 Olympic Dr, Arlington, TX 76011, USA",
    place_id: str = "ChIJ-r",
) -> PlaceResult:
    return PlaceResult(id=place_id, display_name=name, formatted_address=address)


def _named(legal_name: str) -> CandidateFields:
    return a_candidate().model_copy(update={"legal_name": sourced(legal_name), "dba_name": None})


def _at(street: str, postal_code: str = "76011") -> CandidateFields:
    return a_candidate().model_copy(
        update={
            "address": sourced(
                PostalAddress(street=street, city="Arlington", state="TX", postal_code=postal_code)
            )
        }
    )


class TestSearchQuery:
    def test_the_dba_and_the_census_address(self) -> None:
        assert search_query(a_candidate()) == "Acme Freight, 2100 Olympic Dr, Arlington, TX 76011"

    def test_the_legal_name_when_there_is_no_dba(self) -> None:
        fields = a_candidate().model_copy(update={"dba_name": None})
        assert search_query(fields) == (
            "Acme Logistics 1234567 LLC, 2100 Olympic Dr, Arlington, TX 76011"
        )

    def test_uncheckable_without_an_address_or_a_name(self) -> None:
        assert search_query(a_candidate().model_copy(update={"address": None})) is None
        nameless = a_candidate().model_copy(update={"dba_name": None, "legal_name": None})
        assert search_query(nameless) is None

    def test_uncheckable_without_a_street_number(self) -> None:
        """A PO box can never match, so no call is paid for a certain miss."""
        assert search_query(_at("PO BOX 1234")) is None

    def test_uncheckable_without_a_five_digit_zip(self) -> None:
        """`match_place` needs the 5-digit ZIP, so a call without one is a certain miss."""
        assert search_query(_at("2100 Olympic Dr", postal_code="L5T 1A1")) is None

    def test_uncheckable_without_a_distinguishing_name_word(self) -> None:
        """`KC LLC` has no word `match_place` could compare, so it is never paid for."""
        fields = a_candidate().model_copy(
            update={"legal_name": sourced("KC LLC"), "dba_name": None}
        )
        assert search_query(fields) is None


class TestMatchPlace:
    """A4: street number, 5-digit ZIP and a shared distinguishing word. Deterministic, no model."""

    def test_an_exact_match(self) -> None:
        result = _result()
        assert match_place(a_candidate(), [result]) is result

    def test_a_suite_number_in_the_result_still_matches(self) -> None:
        result = _result(address="2100 Olympic Dr Ste 300, Arlington, TX 76011, USA")
        assert match_place(a_candidate(), [result]) is result

    def test_the_right_business_second_in_the_list(self) -> None:
        neighbour = _result(name="Olympic Dental Care", place_id="ChIJ-dentist")
        ours = _result(name="ACME FREIGHT BROKERAGE", place_id="ChIJ-acme")
        assert match_place(a_candidate(), [neighbour, ours]) is ours

    def test_the_same_address_with_an_unrelated_name_is_no_match(self) -> None:
        """A shared office building: the address alone cannot tell neighbours apart."""
        assert match_place(a_candidate(), [_result(name="Olympic Dental Care")]) is None

    def test_the_right_name_in_the_wrong_zip_is_no_match(self) -> None:
        moved = _result(address="2100 Olympic Dr, Fort Worth, TX 76102, USA")
        assert match_place(a_candidate(), [moved]) is None

    def test_the_wrong_street_number_is_no_match(self) -> None:
        assert match_place(a_candidate(), [_result(address="12100 Olympic Dr, TX 76011")]) is None

    def test_a_neighbour_sharing_one_word_in_the_same_building_is_no_match(self) -> None:
        """PR #32 H3: `ACME LOGISTICS` is not `Zenith Logistics` one floor up."""
        fields = _named("ACME LOGISTICS LLC")
        zenith = _result(
            name="Zenith Logistics", address="2100 Olympic Dr Suite 500, Arlington, TX 76011, USA"
        )
        assert match_place(fields, [zenith]) is None

    def test_a_suite_number_is_not_the_street_number(self) -> None:
        """PR #32 H3: the census number must start the address, not appear anywhere in it."""
        result = _result(
            name="Acme Freight", address="5000 Legacy Dr, Suite 2100, Arlington, TX 76011"
        )
        assert match_place(a_candidate(), [result]) is None

    def test_a_short_name_needs_every_distinguishing_word(self) -> None:
        assert (
            match_place(_named("ACME LOGISTICS LLC"), [_result(name="Acme Logistics")]) is not None
        )
        assert match_place(_named("ACME LOGISTICS LLC"), [_result(name="Acme Dental")]) is None

    def test_a_longer_name_matches_on_two_distinguishing_words(self) -> None:
        """Storefronts shorten long legal names; two shared words still rule out a neighbour."""
        fields = _named("ACME FREIGHT BROKERAGE SERVICES LLC")
        assert match_place(fields, [_result(name="Acme Brokerage")]) is not None
        assert match_place(fields, [_result(name="Zenith Freight")]) is None

    def test_a_live_match_after_a_closed_one_wins(self) -> None:
        """PR #32 M2: a closed former listing must not hide the live one behind it."""
        closed = PlaceResult(
            id="ChIJ-old",
            display_name="Acme Freight",
            formatted_address="2100 Olympic Dr, Arlington, TX 76011, USA",
            business_status=CLOSED_PERMANENTLY,
        )
        live = _result(place_id="ChIJ-live")
        assert match_place(a_candidate(), [closed, live]) is live

    def test_a_closed_match_is_returned_when_it_is_the_only_one(self) -> None:
        closed = PlaceResult(
            id="ChIJ-old",
            display_name="Acme Freight",
            formatted_address="2100 Olympic Dr, Arlington, TX 76011, USA",
            business_status=CLOSED_PERMANENTLY,
        )
        assert match_place(a_candidate(), [closed]) is closed

    def test_a_zip_plus_four_in_the_census_matches_on_five_digits(self) -> None:
        result = _result()
        assert match_place(_at("2100 Olympic Dr", "76011-4421"), [result]) is result

    def test_a_dba_only_overlap_matches(self) -> None:
        """The legal name shares nothing ("Zeta Holdings"), the DBA does."""
        fields = a_candidate().model_copy(
            update={"legal_name": sourced("Zeta Holdings LLC"), "dba_name": sourced("Acme Freight")}
        )
        result = _result(name="Acme Freight")
        assert match_place(fields, [result]) is result

    def test_designators_and_stop_words_never_carry_a_match(self) -> None:
        fields = a_candidate().model_copy(
            update={"legal_name": sourced("The Zeta LLC"), "dba_name": None}
        )
        assert match_place(fields, [_result(name="The Omega LLC")]) is None

    def test_a_census_street_with_no_number_never_matches(self) -> None:
        """A PO box is no door: ZIP and name alone would verify a business nobody can visit."""
        result = _result(address="Arlington, TX 76011, USA")
        assert match_place(_at("PO BOX 1234"), [result]) is None

    def test_an_empty_result_list(self) -> None:
        assert match_place(a_candidate(), []) is None

    def test_a_result_with_no_address_or_name(self) -> None:
        assert match_place(a_candidate(), [_result(address=None), _result(name=None)]) is None

    def test_a_retrieval_method_is_irrelevant_to_the_match(self) -> None:
        """The matcher reads values only; a citation's method never changes the answer."""
        fields = a_candidate().model_copy(
            update={"dba_name": sourced("Acme Freight", method=RetrievalMethod.registry_api)}
        )
        assert match_place(fields, [_result()]) is not None
