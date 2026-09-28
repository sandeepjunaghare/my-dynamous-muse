"""Dedupe: the check that runs before any create.

The portal already holds 3,118 contacts and 1,040 companies of mixed provenance (E14).
"""

from unittest.mock import patch

import pytest

from app.promotion.dedupe import find_existing_company, find_existing_contact, normalize_phone
from tests.promotion.conftest import MockPortal, json_responder, load_fixture, make_client

COMPANY_SEARCH = ("POST", "companies/search")
CONTACT_SEARCH = ("POST", "contacts/search")


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw",
        [
            "(214) 555-0147",
            "+1 214-555-0147",
            "214.555.0147",
            "2145550147",
            "+12145550147",
            "1-214-555-0147",
            "214 555 0147 ext 3",
            "+1 (214) 555-0147 x12",
        ],
    )
    def test_every_format_the_portal_holds_reduces_to_the_same_key(self, raw: str) -> None:
        assert normalize_phone(raw) == "2145550147"

    @pytest.mark.parametrize("raw", ["555", "", "abc", "0147", "+1"])
    def test_a_number_too_short_to_be_a_key_becomes_empty(self, raw: str) -> None:
        """Empty means *unusable as a match key*, not "matches anything".

        A short or garbled number that fell through to a search would ask HubSpot to match on a
        fragment, and a false dedupe silently drops a real prospect.
        """
        assert normalize_phone(raw) == ""


class TestFindExistingCompany:
    async def test_a_domain_hit_returns_the_match(self) -> None:
        portal = MockPortal(
            [(*COMPANY_SEARCH, json_responder(200, load_fixture("company_search_hit")))]
        )
        client = make_client(portal)

        match = await find_existing_company(client, domain="lonestarfreightbrokers.com")

        assert match is not None
        assert match.id == "7001234567"
        await client.aclose()

    async def test_a_phone_differing_only_in_formatting_still_matches(self) -> None:
        """The stored number is `(214) 555-0147`; the candidate carries `+1 214-555-0147`."""
        portal = MockPortal(
            [(*COMPANY_SEARCH, json_responder(200, load_fixture("company_search_hit")))]
        )
        client = make_client(portal)

        match = await find_existing_company(client, domain=None, phone="+1 214-555-0147")

        assert match is not None
        assert match.id == "7001234567"
        body = portal.bodies("POST", "companies/search")[0]
        assert "2145550147" in str(body), "the country code must be stripped from the query"
        await client.aclose()

    async def test_a_miss_returns_none(self) -> None:
        portal = MockPortal(
            [(*COMPANY_SEARCH, json_responder(200, load_fixture("company_search_miss")))]
        )
        client = make_client(portal)

        assert await find_existing_company(client, domain="nobody.example") is None
        await client.aclose()

    async def test_an_unusable_phone_is_never_searched_on(self) -> None:
        portal = MockPortal()
        client = make_client(portal)

        assert await find_existing_company(client, domain=None, phone="555") is None
        assert portal.requests == []
        await client.aclose()

    async def test_dedupe_alone_never_creates(self) -> None:
        """A matched company must come back, not be created again.

        `create_company` is replaced with something that explodes, so if any path through dedupe
        reached a create the test would say so by name rather than by a duplicate appearing in a
        portal nobody is watching.
        """
        portal = MockPortal(
            [(*COMPANY_SEARCH, json_responder(200, load_fixture("company_search_hit")))]
        )
        client = make_client(portal)

        with patch(
            "app.promotion.client.HubSpotClient.create_company",
            side_effect=AssertionError("dedupe must run before, and instead of, a create"),
        ):
            match = await find_existing_company(client, domain="lonestarfreightbrokers.com")

        assert match is not None
        assert portal.count("POST", "companies/search") == 1
        assert portal.count("POST", "/crm/objects/") == 1, "only the search — no create"
        await client.aclose()


class TestFindExistingContact:
    async def test_an_email_hit_returns_the_match(self) -> None:
        portal = MockPortal(
            [(*CONTACT_SEARCH, json_responder(200, load_fixture("contact_search_hit")))]
        )
        client = make_client(portal)

        match = await find_existing_contact(client, email="marisol@lonestarfreightbrokers.com")

        assert match is not None
        assert match.id == "400112233"
        await client.aclose()

    async def test_phone_and_mobilephone_are_searched_separately(self) -> None:
        """Filters inside one group are ANDed, so one group asking for both would find nothing."""
        portal = MockPortal(
            [(*CONTACT_SEARCH, json_responder(200, load_fixture("company_search_miss")))]
        )
        client = make_client(portal)

        assert await find_existing_contact(client, phone="214-555-0147") is None
        assert portal.count("POST", "contacts/search") == 2

        properties_searched = [str(body) for body in portal.bodies("POST", "contacts/search")]
        assert any("mobilephone" in body for body in properties_searched)
        await client.aclose()
