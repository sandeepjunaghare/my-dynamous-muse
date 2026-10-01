"""The write-gate: what reaches HubSpot, and what is refused before anything is sent.

E18 is the reason this exists — 14 of 34 fire-vertical contacts carry a company name in the
first-name field, and every one of those is a value nobody could cite.
"""

from datetime import timedelta, timezone

import pytest

from app.promotion.client import render_property_value, to_property_payload
from app.promotion.exceptions import UnprovenancedWriteError
from app.shared.provenance import ProvenancedValue, is_promotable
from tests.promotion.conftest import RETRIEVED_AT, MockPortal, cited, make_client


class TestRendering:
    def test_a_cited_string_passes_through(self) -> None:
        assert to_property_payload({"domain": cited("acme.com")}) == {"domain": "acme.com"}

    def test_a_number_renders_without_decoration(self) -> None:
        assert to_property_payload({"lpe_priority_score": cited(42)}) == {
            "lpe_priority_score": "42"
        }

    def test_a_datetime_renders_as_epoch_milliseconds(self) -> None:
        payload = to_property_payload({"lpe_sourced_at": cited(RETRIEVED_AT)})
        assert payload["lpe_sourced_at"] == "1790519400000"

    def test_a_non_utc_datetime_keeps_the_instant_it_named(self) -> None:
        """`ProvenancedValue` normalizes to UTC on construction, so the epoch value is unchanged.

        The same moment expressed in Chicago time must render to the same millisecond — otherwise
        a sourced-at stamp would drift by the developer's timezone.
        """
        chicago = RETRIEVED_AT.astimezone(timezone(timedelta(hours=-5)))
        assert to_property_payload({"lpe_sourced_at": cited(chicago)}) == {
            "lpe_sourced_at": "1790519400000"
        }

    def test_a_bool_renders_as_hubspot_spells_it(self) -> None:
        """`bool` is an `int` at runtime, so a `ProvenancedValue[int]` can hold one.

        Tested against the renderer directly because `bool` is deliberately not a member of the
        `ProvenancedProperty` union — the branch guards the runtime case the type system permits
        but the union does not advertise. Without it the portal would receive "True".
        """
        assert render_property_value(True) == "true"
        assert render_property_value(False) == "false"


class TestRefusal:
    def test_an_uncited_field_is_refused(self) -> None:
        with pytest.raises(UnprovenancedWriteError) as raised:
            to_property_payload({"domain": None})

        assert raised.value.fields == ("domain",)

    def test_every_offender_is_named_in_one_exception(self) -> None:
        """Refusing on the first offender makes a caller fix fields one round-trip at a time."""
        with pytest.raises(UnprovenancedWriteError) as raised:
            to_property_payload({"phone": None, "domain": None, "name": cited("Acme Freight")})

        assert raised.value.fields == ("domain", "phone")
        assert "domain, phone" in str(raised.value)

    def test_the_gate_refuses_exactly_what_the_shared_predicate_refuses(self) -> None:
        """`to_property_payload` delegates to `is_promotable`; this pins them together.

        The gate cannot be allowed to drift from the predicate it claims to enforce — that drift
        is silent, and the symptom is uncitable data in the CRM.
        """
        present = cited("acme.com")
        absent: ProvenancedValue[str] | None = None

        assert is_promotable(present) is True
        assert is_promotable(absent) is False
        assert to_property_payload({"domain": present}) == {"domain": "acme.com"}
        with pytest.raises(UnprovenancedWriteError):
            to_property_payload({"domain": absent})

    async def test_a_refused_write_sends_nothing(self) -> None:
        """No partial payload, and no request at all — the refusal happens before the wire.

        The portal has no routes, so any request would raise a different error naming the path.
        `requests` being empty is the assertion that nothing was attempted.
        """
        portal = MockPortal()
        client = make_client(portal)

        with pytest.raises(UnprovenancedWriteError):
            await client.create_company({"domain": None, "name": cited("Acme Freight")})

        assert portal.requests == []
        await client.aclose()
