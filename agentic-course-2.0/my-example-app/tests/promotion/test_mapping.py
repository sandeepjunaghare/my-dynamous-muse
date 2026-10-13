"""Candidate → company and contact writes. Pure: every mapped value keeps its source's citation."""

import pytest

from app.promotion.client import to_property_payload
from app.promotion.mapping import company_write, contact_write, record_url, website_domain
from app.promotion.schemas import ObjectType, ReviewStatus
from app.shared.provenance import ProvenancedValue
from app.sourcing.schemas import CandidateFields, Principal, is_google_maps
from tests.promotion.builders import a_promotable, a_score, an_owner, census_row_url
from tests.sourcing.builders import ARLINGTON, QCMOBILE_URL, sourced


def _company(fields: CandidateFields, route: str | None = "A · 76011") -> dict[str, str]:
    write = company_write(fields, vertical="freight", route_label=route, owner_id="77")
    return to_property_payload(write.properties)


class TestCompany:
    def test_the_identity_fields_are_mapped(self) -> None:
        properties = _company(a_promotable("1000001"))

        assert properties["name"] == "Acme Logistics 1000001 LLC"
        assert properties["phone"] == "+1-817-555-0100"
        assert (
            properties["address"],
            properties["city"],
            properties["state"],
            properties["zip"],
        ) == (
            ARLINGTON.street,
            ARLINGTON.city,
            ARLINGTON.state,
            ARLINGTON.postal_code,
        )

    def test_each_address_part_carries_the_address_citation(self) -> None:
        fields = a_promotable()
        assert fields.address is not None
        write = company_write(fields, vertical="freight", route_label=None, owner_id=None)

        for name in ("address", "city", "state", "zip"):
            cited = write.properties[name]
            assert (cited.source_url, cited.retrieved_at, cited.retrieval_method) == (
                fields.address.source_url,
                fields.address.retrieved_at,
                fields.address.retrieval_method,
            )

    def test_every_value_keeps_its_source_fields_citation(self) -> None:
        fields = a_promotable(website="https://www.Acme-Logistics.test/").model_copy(
            update={"priority": a_score(4, 4)}
        )
        write = company_write(fields, vertical="freight", route_label=None, owner_id=None)
        sources = {
            "name": fields.legal_name,
            "phone": fields.phone,
            "domain": fields.website,
            "lpe_priority_score": fields.priority,
            "lpe_source_url": fields.registry_id,
            "lpe_sourced_at": fields.registry_id,
        }

        for name, source in sources.items():
            assert source is not None
            assert write.properties[name].source_url == source.source_url, name
            assert write.properties[name].retrieval_method == source.retrieval_method, name

    def test_the_provenance_properties_are_derived_from_the_registry_citation(self) -> None:
        fields = a_promotable("1000001")
        write = company_write(fields, vertical="freight", route_label=None, owner_id=None)
        properties = to_property_payload(write.properties)

        assert properties["lpe_source_url"] == census_row_url("1000001")
        epoch_ms = int(fields.registry_id.retrieved_at.timestamp() * 1000)
        assert properties["lpe_sourced_at"] == str(epoch_ms)
        assert write.key == census_row_url("1000001")

    def test_the_domain_comes_from_the_website_host(self) -> None:
        fields = a_promotable(website="https://www.Acme-Logistics.test/x")
        write = company_write(fields, vertical="freight", route_label=None, owner_id=None)

        assert to_property_payload(write.properties)["domain"] == "acme-logistics.test"
        assert write.domain == "acme-logistics.test"

    def test_without_a_website_there_is_no_domain(self) -> None:
        write = company_write(a_promotable(), vertical="freight", route_label=None, owner_id=None)

        assert "domain" not in write.properties
        assert write.domain is None

    def test_the_score_is_mapped_only_when_cited(self) -> None:
        assert "lpe_priority_score" not in _company(a_promotable())
        scored = a_promotable().model_copy(update={"priority": a_score(4, 5)})
        assert _company(scored)["lpe_priority_score"] == "20"

    def test_no_value_is_none(self) -> None:
        write = company_write(a_promotable(), vertical="freight", route_label=None, owner_id=None)

        assert all(value is not None for value in write.properties.values())

    def test_nothing_mapped_cites_google_maps(self) -> None:
        """D13: ``business_check`` is the one field that may, and it is never mapped."""
        fields = a_promotable(website="https://acme-logistics.test/").model_copy(
            update={"priority": a_score(4, 4)}
        )
        company = company_write(fields, vertical="freight", route_label="A", owner_id=None)
        contact = contact_write(fields, vertical="freight", owner_id=None)

        for cited in (*company.properties.values(), *contact.properties.values()):
            assert not is_google_maps(cited.source_url)

    def test_the_metadata_is_pending_review_with_the_route(self) -> None:
        write = company_write(
            a_promotable(), vertical="freight", route_label="A · 76011", owner_id="77"
        )

        assert write.meta.to_properties(ObjectType.companies) == {
            "lpe_vertical": "freight",
            "lpe_review_status": ReviewStatus.pending_review.value,
            "lpe_route_cluster": "A · 76011",
            "hubspot_owner_id": "77",
        }

    def test_an_unrouted_company_has_no_route(self) -> None:
        write = company_write(a_promotable(), vertical="freight", route_label=None, owner_id=None)

        assert "lpe_route_cluster" not in write.meta.to_properties(ObjectType.companies)

    def test_a_missing_required_field_is_a_bug(self) -> None:
        fields = a_promotable().model_copy(update={"phone": None})

        with pytest.raises(ValueError, match="missing a required field"):
            company_write(fields, vertical="freight", route_label=None, owner_id=None)


class TestContact:
    def test_the_name_is_split_and_cited_to_the_owner(self) -> None:
        fields = a_promotable(owner="MICHAEL J RICKETTS")
        assert fields.owner is not None
        write = contact_write(fields, vertical="freight", owner_id="77")
        properties = to_property_payload(write.properties)

        assert (properties["firstname"], properties["lastname"]) == ("MICHAEL J", "RICKETTS")
        assert write.properties["firstname"].source_url == fields.owner.source_url
        assert write.properties["lastname"].source_url == fields.owner.source_url
        assert not write.name.ambiguous

    def test_an_ambiguous_split_is_flagged_and_drops_nothing(self) -> None:
        write = contact_write(
            a_promotable(owner="JUAN JOSE DE LEON"), vertical="freight", owner_id=None
        )
        properties = to_property_payload(write.properties)

        assert (properties["firstname"], properties["lastname"]) == ("JUAN", "JOSE DE LEON")
        assert write.name.ambiguous

    def test_a_single_word_is_a_last_name_only(self) -> None:
        write = contact_write(a_promotable(owner="MADONNA"), vertical="freight", owner_id=None)

        assert "firstname" not in write.properties
        assert to_property_payload(write.properties)["lastname"] == "MADONNA"

    def test_a_stated_title_is_the_job_title(self) -> None:
        fields = a_promotable().model_copy(update={"owner": an_owner("DAVE MUSGRAVES", "OWNER")})

        properties = to_property_payload(
            contact_write(fields, vertical="freight", owner_id=None).properties
        )

        assert properties["jobtitle"] == "OWNER"

    def test_there_is_no_email_and_no_phone(self) -> None:
        fields = a_promotable().model_copy(
            update={"email": sourced("dispatch@acme-logistics.test")}
        )

        properties = contact_write(fields, vertical="freight", owner_id=None).properties

        assert not {"email", "phone", "mobilephone"} & properties.keys()

    def test_the_provenance_properties_come_from_the_owner_citation(self) -> None:
        owner: ProvenancedValue[Principal] = sourced(Principal(name="DAVE MUSGRAVES"), QCMOBILE_URL)
        fields = a_promotable().model_copy(update={"owner": owner})
        write = contact_write(fields, vertical="freight", owner_id=None)

        assert to_property_payload(write.properties)["lpe_source_url"] == QCMOBILE_URL
        assert write.source_url == QCMOBILE_URL

    def test_the_metadata_carries_no_review_status_or_route(self) -> None:
        write = contact_write(a_promotable(), vertical="freight", owner_id="77")

        assert write.meta.to_properties(ObjectType.contacts) == {
            "lpe_vertical": "freight",
            "hubspot_owner_id": "77",
        }

    def test_no_owner_is_a_bug(self) -> None:
        with pytest.raises(ValueError, match="has no owner"):
            contact_write(a_promotable(owner=None), vertical="freight", owner_id=None)


@pytest.mark.parametrize(
    ("website", "domain"),
    [
        ("https://www.Acme.com/x", "acme.com"),
        ("https://acme.com", "acme.com"),
        ("https://xn--bcher-kva.de/", "xn--bcher-kva.de"),
        ("https://www.acme.com./", "acme.com"),
        ("not a url", None),
    ],
)
def test_website_domain(website: str, domain: str | None) -> None:
    assert website_domain(website) == domain


class TestRecordUrl:
    def test_a_url_needs_the_portal_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUBSPOT_PORTAL_ID", "244766495")
        monkeypatch.setenv("HUBSPOT_UI_DOMAIN", "app-na2.hubspot.com")

        assert record_url(ObjectType.companies, "7001") == (
            "https://app-na2.hubspot.com/contacts/244766495/record/0-2/7001"
        )
        assert record_url(ObjectType.contacts, "4001") == (
            "https://app-na2.hubspot.com/contacts/244766495/record/0-1/4001"
        )

    @pytest.mark.parametrize("portal", ["", "  "])
    def test_a_blank_portal_id_gives_no_url(
        self, monkeypatch: pytest.MonkeyPatch, portal: str
    ) -> None:
        monkeypatch.setenv("HUBSPOT_PORTAL_ID", portal)

        assert record_url(ObjectType.companies, "7001") is None
