"""The sourcing shapes, with no database.

The load-bearing ones: an uncited field is *absent*, never a raw value; ``is_promotable`` refuses an
absent field; and a candidate survives the JSON trip the JSONB column puts it through.
"""

from datetime import UTC
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from app.core.cost import BillableKind, RunCost
from app.manifests.schemas import IcpBand
from app.shared.provenance import ProvenancedValue, RetrievalMethod, is_promotable
from app.sourcing.schemas import (
    CandidateFields,
    HeadcountBand,
    MissKind,
    PlaceCheck,
    PlacesVerdict,
    PostalAddress,
    Principal,
    PriorityScore,
    RunCostSummary,
    RunOutcome,
    RunStatus,
    RunTotals,
    ScoreBand,
    SourceRecord,
    SourceRow,
    SourcingBrief,
    has_entity_designator,
    name_words,
    principal_or_none,
    shares_name,
)
from tests.sourcing.builders import (
    ARLINGTON,
    OWN_DOMAIN,
    PLACES_URL,
    QCMOBILE_URL,
    a_brief,
    a_candidate,
    a_verdict,
    looked_up,
    own_site,
    place_checked,
    sourced,
    website_verified,
)

BAND = IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True)


class TestBrief:
    def test_a_valid_brief(self) -> None:
        brief = a_brief("freight")
        assert (brief.vertical, brief.geography) == ("freight", "dfw")

    @pytest.mark.parametrize("field", ["vertical", "geography"])
    @pytest.mark.parametrize("bad", ["", "DFW", "dfw metro", "1dfw"])
    def test_identifiers_must_be_slugs(self, field: str, bad: str) -> None:
        values = {"vertical": "freight", "geography": "dfw", "icp_band": BAND}
        values[field] = bad
        with pytest.raises(ValidationError):
            SourcingBrief.model_validate(values)

    def test_an_inverted_icp_band_is_refused(self) -> None:
        """Inherited from the manifest's ``IcpBand`` — one definition of a band, one validator."""
        with pytest.raises(ValidationError, match="must not exceed"):
            SourcingBrief.model_validate(
                {
                    "vertical": "freight",
                    "geography": "dfw",
                    "icp_band": {
                        "headcount_min": 200,
                        "headcount_max": 20,
                        "requires_office_function": True,
                    },
                }
            )

    def test_the_brief_is_frozen(self) -> None:
        """Checked through the config, not by assigning — an assignment would need a suppression."""
        assert SourcingBrief.model_config.get("frozen") is True


class TestCandidateFields:
    def test_registry_id_is_required(self) -> None:
        """A candidate exists because a registry named it — its id is the one field never absent."""
        with pytest.raises(ValidationError, match="registry_id"):
            CandidateFields.model_validate({"legal_name": None})

    def test_a_blank_registry_id_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="registry_id must not be blank"):
            CandidateFields(registry_id=sourced("   "))

    def test_the_registry_id_is_stripped(self) -> None:
        """It is the upsert key: " 555 " and "555" are one carrier, so they must be one row."""
        candidate = CandidateFields(registry_id=sourced(" 555\t"))
        assert candidate.registry_id.value == "555"
        assert candidate.registry_id.source_url == sourced("555").source_url

    def test_a_registry_id_longer_than_its_column_is_refused(self) -> None:
        """Refused here, as a ValidationError — not as a DBAPIError that aborts the whole batch."""
        with pytest.raises(ValidationError, match="at most 128 characters"):
            CandidateFields(registry_id=sourced("9" * 129))

    def test_a_registry_id_at_the_limit_is_accepted(self) -> None:
        """The limit counts the stripped value — padding does not push a valid id over it."""
        candidate = CandidateFields(registry_id=sourced(f"  {'9' * 128}  "))
        assert len(candidate.registry_id.value) == 128

    def test_an_unknown_field_is_refused(self) -> None:
        """A key the schema does not know is a typo or a drift — loud, not silently dropped."""
        payload = a_candidate().model_dump(mode="json")
        payload["owner_nickname"] = None
        with pytest.raises(ValidationError, match="owner_nickname"):
            CandidateFields.model_validate(payload)

    def test_a_raw_uncited_value_cannot_be_stored(self) -> None:
        """There is no slot for a value without a citation — absent is the only uncited state."""
        payload = a_candidate().model_dump(mode="json")
        payload["phone"] = "+1-817-555-0100"
        with pytest.raises(ValidationError):
            CandidateFields.model_validate(payload)

    def test_unprovenanced_fields_names_exactly_the_absent_ones(self) -> None:
        candidate = a_candidate(with_phone=False)
        assert candidate.unprovenanced_fields() == (
            "phone",
            "email",
            "website",
            "business_check",
            "places_verdict",
            "owner",
            "headcount_band",
            "priority",
        )

    def test_a_fully_cited_candidate_has_none(self) -> None:
        candidate = CandidateFields(
            registry_id=sourced("1"),
            legal_name=sourced("A"),
            dba_name=sourced("B"),
            address=a_candidate().address,
            phone=sourced("1"),
            email=sourced("jane@example.com"),
            website=sourced("https://example.com"),
            business_check=place_checked(),
            places_verdict=a_verdict(),
            owner=sourced(Principal(name="Jane Doe")),
            headcount_band=sourced(
                HeadcountBand(low=11, high=50),
                "https://example.com/about",
                RetrievalMethod.llm_inference,
            ),
            priority=sourced(
                PriorityScore(intensity=4, automatable=4),
                "https://example.com/about",
                RetrievalMethod.llm_inference,
            ),
        )
        assert candidate.unprovenanced_fields() == ()

    def test_storable_but_not_promotable(self) -> None:
        """The ticket's rule at the type level: an absent field exists and the gate refuses it."""
        candidate = a_candidate(with_phone=False)
        assert candidate.phone is None
        assert is_promotable(candidate.phone) is False
        assert is_promotable(candidate.legal_name) is True
        assert is_promotable(candidate.registry_id) is True

    def test_an_editable_address_cannot_be_cited(self) -> None:
        """``ProvenancedValue`` refuses a mutable value, so the address model must be frozen."""

        class LooseAddress(BaseModel):
            street: str

        with pytest.raises(ValidationError, match="frozen"):
            ProvenancedValue(
                value=LooseAddress(street="2100 Olympic Dr"),
                source_url="https://example.com",
                retrieved_at=sourced("x").retrieved_at,
                retrieval_method=RetrievalMethod.bulk_file,
            )

    def test_json_round_trip_keeps_every_citation(self) -> None:
        """What the JSONB column does to a candidate, without the database."""
        original = a_candidate(with_phone=False)
        restored = CandidateFields.model_validate(
            original.model_dump(mode="json", exclude_none=True)
        )

        assert restored == original
        assert restored.registry_id.retrieved_at.tzinfo is UTC
        assert restored.address is not None
        assert restored.address.value.city == "Arlington"
        assert restored.legal_name is not None
        assert restored.legal_name.retrieval_method is RetrievalMethod.bulk_file
        assert restored.phone is None


class TestBusinessCheck:
    """D13: Places is a check, never a source. The place ID is all a candidate keeps from it."""

    def test_a_verified_address_needs_the_address_and_a_check(self) -> None:
        checked = a_candidate().model_copy(update={"business_check": place_checked()})
        assert checked.has_verified_address() is True

    def test_an_address_never_checked_is_not_verified(self) -> None:
        """The census address alone is cited, but nobody has confirmed the business is there."""
        assert a_candidate().has_verified_address() is False

    def test_a_check_without_an_address_verifies_nothing(self) -> None:
        bare = CandidateFields(registry_id=sourced("1234567"), business_check=place_checked())
        assert bare.has_verified_address() is False

    def test_a_blank_place_id_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            PlaceCheck(place_id="  ", checked_address=ARLINGTON)

    def test_a_check_must_say_which_address_it_checked(self) -> None:
        """A8: a check not tied to an address cannot be told apart from a stale one."""
        with pytest.raises(ValidationError, match="checked_address"):
            PlaceCheck.model_validate({"place_id": "ChIJ-x"})

    def test_a_changed_address_un_verifies_the_check(self) -> None:
        """PR #13 L2: compare values. The business moved, so the check is of somewhere else."""
        moved = PostalAddress(street="1 Main St", city="Dallas", state="TX", postal_code="75201")
        checked = a_candidate().model_copy(
            update={"business_check": place_checked(), "address": sourced(moved)}
        )
        assert checked.has_verified_address() is False

    def test_a_refreshed_citation_of_the_same_address_stays_verified(self) -> None:
        """Every registry refresh bumps ``retrieved_at``; that alone must not un-verify."""
        refreshed = ProvenancedValue(
            value=ARLINGTON,
            source_url="https://data.transportation.gov/d/az4n-8mr2",
            retrieved_at=sourced("x").retrieved_at.replace(year=2027),
            retrieval_method=RetrievalMethod.bulk_file,
        )
        checked = a_candidate().model_copy(
            update={"business_check": place_checked(), "address": refreshed}
        )
        assert checked.has_verified_address() is True

    def test_the_check_survives_the_json_trip(self) -> None:
        checked = a_candidate().model_copy(update={"business_check": place_checked("ChIJ-x")})
        restored = CandidateFields.model_validate(checked.model_dump(mode="json"))
        assert restored.business_check is not None
        assert restored.business_check.value.place_id == "ChIJ-x"
        assert restored.business_check.value.checked_address == ARLINGTON
        assert restored.business_check.source_url == PLACES_URL

    @pytest.mark.parametrize(
        "url",
        [
            PLACES_URL,
            "https://maps.googleapis.com/maps/api/place/details/json?place_id=x",
            "https://www.google.com/maps/place/?q=place_id:ChIJ-x",
            "https://maps.google.com/?cid=123",
            # Review L1: forms the first guard missed.
            "https://maps.google.com./place/x",
            "https://www.google.com/Maps/place/x",
            "HTTPS://MAPS.GOOGLE.COM/x",
            "https://goo.gl/maps/abc123",
            "https://maps.app.goo.gl/abc123",
            "https://example.com@maps.google.com/x",
            "https://maps.google.com:443/x",
        ],
    )
    def test_no_other_field_may_cite_google_maps(self, url: str) -> None:
        """The structure guard D13 asked for: copying Places content into a field fails here."""
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("1234567"),
                phone=sourced("+1-817-555-0199", url, RetrievalMethod.web_lookup),
            )

    @pytest.mark.parametrize("url", ["maps.google.com/place/x", "//maps.google.com/place/x"])
    def test_a_scheme_less_maps_url_is_refused_before_d13_is_reached(self, url: str) -> None:
        """Review L1's scheme-less forms. Since PR #2 finding #2, a citation must be an http(s)
        URL, so these are refused by the citation itself; the D13 guard never sees them. Either
        way, Places content cannot be stored."""
        with pytest.raises(ValidationError, match="http"):
            CandidateFields(
                registry_id=sourced("1234567"),
                phone=sourced("+1-817-555-0199", url, RetrievalMethod.web_lookup),
            )

    def test_an_address_cited_to_places_is_refused(self) -> None:
        address = PostalAddress(
            street="2100 Olympic Dr", city="Arlington", state="TX", postal_code="76011"
        )
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("1234567"),
                address=sourced(address, PLACES_URL, RetrievalMethod.web_lookup),
            )

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.google.com/search?q=acme+freight",
            "https://goo.gl/xyz",
            "https://www.acmefreight.test/maps",
            "https://notgoogle.com/maps",
        ],
    )
    def test_google_urls_that_are_not_maps_are_allowed(self, url: str) -> None:
        fields = CandidateFields(
            registry_id=sourced("1234567"),
            website=sourced("https://acmefreight.test", url, RetrievalMethod.web_lookup),
        )
        assert fields.website is not None

    def test_a_website_from_a_web_search_is_fine(self) -> None:
        """Google the search engine is not Google Maps: only Maps content is barred."""
        fields = CandidateFields(
            registry_id=sourced("1234567"), website=looked_up("https://x.test")
        )
        assert fields.website is not None


class TestPrincipal:
    """The owner T6 fills and T9 writes to a HubSpot Contact."""

    def test_the_name_is_kept_as_the_source_stated_it(self) -> None:
        principal = Principal(name="  JOSEPH JOHN BELOVICH JR ")
        assert principal.name == "JOSEPH JOHN BELOVICH JR"
        assert principal.title is None

    def test_a_title_is_optional_and_stripped(self) -> None:
        assert Principal(name="Jane Doe", title=" President ").title == "President"

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_name_is_refused(self, name: str) -> None:
        with pytest.raises(ValidationError):
            Principal(name=name)

    def test_a_blank_title_is_absent_rather_than_empty(self) -> None:
        assert Principal(name="Jane Doe", title="  ").title is None

    @pytest.mark.parametrize(
        "name",
        [
            # Real census company_officer_1 values (FMCSA, TX brokers, 2026-10-11).
            "KZ TRANS INC",
            "WHITE WOLF LOGISTICS LLC",
            "I.N.R TRUCKING LLC",
            "Acme Freight L.L.C.",
            "Acme Freight, Inc.",
            "ACME CORP",
            "Acme Corporation",
            "Acme Incorporated",
            "Acme Ltd",
            "Acme Partners LLP",
            "Acme PLLC",
            "Acme Holdings LP",
            # PR #31 review M1: dotted forms and the spelled-out word.
            "Acme Partners L.P.",
            "Acme L.L.P.",
            "Acme Freight Limited",
            "José Ramírez INC",
        ],
    )
    def test_a_company_name_in_the_person_field_is_refused(self, name: str) -> None:
        """E18: a company name sitting where a person belongs is a field nobody can cite as one."""
        with pytest.raises(ValidationError, match="E18"):
            Principal(name=name)

    @pytest.mark.parametrize(
        "name",
        [
            "Vincent Corpus",
            "Lincoln Ltdson",
            "Prince Llewellyn",
            "Alp Inceoglu",
            "LLOYD INCE",
            # PR #31 review L3: an accented letter is a letter, not a word boundary.
            "Lpé Martín",
            "Ángel Incé",
        ],
    )
    def test_a_designator_inside_a_word_is_not_one(self, name: str) -> None:
        assert Principal(name=name).name == name

    @pytest.mark.parametrize("name", ["Jason Co", "Mary Lc", "Peter Pc"])
    def test_short_designators_that_are_also_surnames_are_left_out(self, name: str) -> None:
        """CO, LC and PC are deliberately not designators here: "Co" is a surname, and refusing a
        real owner costs more than letting the rare "ACME CO" through to a human at review."""
        assert Principal(name=name).name == name

    def test_a_sole_proprietor_named_for_themselves_is_accepted(self) -> None:
        """480 TX broker census rows have officer = legal name: sole proprietors, really the owner.
        Name equality would refuse every one, which is why the guard is a designator instead."""
        fields = CandidateFields(
            registry_id=sourced("1234567"),
            legal_name=sourced("JAMES OEHLKE"),
            owner=sourced(Principal(name="JAMES OEHLKE")),
        )
        assert fields.owner is not None

    def test_the_principal_is_frozen_so_it_can_be_cited(self) -> None:
        assert Principal.model_config.get("frozen") is True
        assert sourced(Principal(name="Jane Doe")).value.name == "Jane Doe"

    def test_an_owner_cited_to_google_maps_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("1234567"),
                owner=sourced(Principal(name="Jane Doe"), PLACES_URL, RetrievalMethod.web_lookup),
            )

    def test_an_owner_survives_the_json_trip(self) -> None:
        owner = sourced(
            Principal(name="Jane Doe", title="Owner"),
            "https://www.acmefreight.test/about",
            RetrievalMethod.llm_inference,
        )
        fields = CandidateFields(registry_id=sourced("1234567"), owner=owner)
        assert CandidateFields.model_validate(fields.to_stored()).owner == owner


class TestRunStatus:
    def test_every_outcome_is_a_status(self) -> None:
        assert {outcome.value for outcome in RunOutcome} <= {status.value for status in RunStatus}

    def test_running_is_not_an_outcome(self) -> None:
        """Finishing a run "as running" must be unrepresentable, not merely refused."""
        assert "running" not in {outcome.value for outcome in RunOutcome}


class TestRunCostSummary:
    def test_snapshots_calls_and_dollars_per_kind(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=3, usd=Decimal("0.051"))
        cost.record(BillableKind.anthropic_tokens, usd=Decimal("0.0123"))

        summary = RunCostSummary.from_run_cost(cost)

        assert summary.calls[BillableKind.places_lookup] == 3
        assert summary.calls[BillableKind.anthropic_tokens] == 1
        assert summary.total_usd() == Decimal("0.0633")

    def test_every_kind_is_present_even_when_unspent(self) -> None:
        """A stable shape: "zero Places calls" is recorded, not inferred from absence."""
        summary = RunCostSummary.from_run_cost(RunCost())
        assert set(summary.calls) == set(BillableKind)
        assert set(summary.usd) == set(BillableKind)
        assert summary.total_usd() == Decimal("0")

    def test_dollars_survive_json_exactly(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, usd=Decimal("0.017"))
        summary = RunCostSummary.from_run_cost(cost)

        restored = RunCostSummary.model_validate(summary.model_dump(mode="json"))

        assert restored.usd[BillableKind.places_lookup] == Decimal("0.017")


class TestRunTotals:
    def test_a_negative_count_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RunTotals(counts={"sourced": -1}, cost=RunCostSummary())

    def test_a_non_slug_stage_name_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            RunTotals(counts={"Sourced Rows": 3}, cost=RunCostSummary())


class TestPriorityScore:
    @pytest.mark.parametrize(
        ("intensity", "automatable", "band"),
        [
            (4, 4, ScoreBand.live),
            (5, 5, ScoreBand.live),
            (3, 3, ScoreBand.middle),
            (3, 5, ScoreBand.middle),
            (2, 4, ScoreBand.dead),
            (1, 1, ScoreBand.dead),
        ],
    )
    def test_bands_follow_e9(self, intensity: int, automatable: int, band: ScoreBand) -> None:
        """>=16 live, <9 dead (E9); 16 and 9 are the boundaries."""
        assert PriorityScore(intensity=intensity, automatable=automatable).band is band

    def test_axes_are_one_to_five(self) -> None:
        with pytest.raises(ValidationError):
            PriorityScore(intensity=0, automatable=3)
        with pytest.raises(ValidationError):
            PriorityScore(intensity=3, automatable=6)

    def test_a_candidate_holds_a_cited_score(self) -> None:
        fields = a_candidate().model_copy(
            update={
                "priority": sourced(
                    PriorityScore(intensity=4, automatable=5),
                    "https://example.test/about",
                    RetrievalMethod.llm_inference,
                )
            }
        )
        revalidated = CandidateFields.model_validate(fields.model_dump(mode="json"))
        assert revalidated.priority is not None
        assert revalidated.priority.value.score == 20


def _record(
    source: str, *rows: SourceRow, url: str = QCMOBILE_URL
) -> ProvenancedValue[SourceRecord]:
    return sourced(SourceRecord(source=source, rows=rows), url, RetrievalMethod.registry_api)


class TestSourceRecords:
    """T5's generic evidence: each source's raw record, cited, for T7's rules and T6's fill."""

    def test_a_row_is_sorted_by_key(self) -> None:
        row = SourceRow(values=(("power_units", 0), ("carship", "B")))
        assert row.values == (("carship", "B"), ("power_units", 0))

    def test_a_row_refuses_duplicate_and_blank_keys(self) -> None:
        with pytest.raises(ValidationError, match="duplicate"):
            SourceRow(values=(("carship", "B"), ("carship", "C")))
        with pytest.raises(ValidationError, match="blank"):
            SourceRow(values=((" ", "B"),))

    def test_get_returns_the_stated_value_or_none(self) -> None:
        row = SourceRow(values=(("allowedToOperate", "Y"),))
        assert row.get("allowedToOperate") == "Y"
        assert row.get("allowToOperate") is None

    def test_empty_rows_mean_looked_and_found_nothing(self) -> None:
        """Distinct from an absent record, which means never looked."""
        fields = CandidateFields(
            registry_id=sourced("usdot:1"), source_records=(_record("fmcsa_revocations"),)
        )
        assert fields.source_records[0].value.rows == ()

    def test_two_records_for_one_source_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="more than one source record"):
            CandidateFields(
                registry_id=sourced("usdot:1"),
                source_records=(_record("fmcsa_qcmobile"), _record("fmcsa_qcmobile")),
            )

    def test_a_string_and_an_integer_survive_the_json_round_trip_as_themselves(self) -> None:
        """Pydantic's smart union must keep ``"010"`` a string and ``10`` an integer."""
        row = SourceRow(values=(("phy_cnty", "010"), ("power_units", 10)))
        fields = CandidateFields(
            registry_id=sourced("usdot:1"), source_records=(_record("fmcsa_census", row),)
        )
        restored = CandidateFields.model_validate(fields.model_dump(mode="json"))
        assert restored == fields
        values = restored.source_records[0].value.rows[0].values
        assert values == (("phy_cnty", "010"), ("power_units", 10))

    def test_a_source_record_cited_to_google_maps_is_refused(self) -> None:
        """D13 reaches inside tuple fields too."""
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("usdot:1"),
                source_records=(_record("places", url=PLACES_URL),),
            )

    def test_source_records_are_never_reported_as_unprovenanced(self) -> None:
        fields = CandidateFields(registry_id=sourced("usdot:1"))
        assert "source_records" not in fields.unprovenanced_fields()


class TestPrincipalOrNone:
    """PR #31 L1 and M2: one test of "names a citable person", and it never raises."""

    @pytest.mark.parametrize("name", [None, "", "  ", "KZ TRANS INC", "VAB FREIGHT BROKERAGE LLC"])
    def test_absent_blank_or_refused_names_give_none(self, name: str | None) -> None:
        assert principal_or_none(name) is None

    def test_a_person_gives_a_principal(self) -> None:
        principal = principal_or_none("JAMES OEHLKE", "Owner")
        assert principal == Principal(name="JAMES OEHLKE", title="Owner")

    def test_a_person_and_a_company_in_one_string_is_refused_not_split(self) -> None:
        """Never guessed apart: the judgment node runs instead."""
        assert principal_or_none("GUNNER CAPITAL PARTNERS LLC - BRYAN GRIGGS") is None

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("ACME LLC", True),
            ("Acme, Inc.", True),
            ("LLOYD INCE", False),
            ("Vincent Corpus", False),
        ],
    )
    def test_the_designator_helper_is_the_principal_rule(self, name: str, expected: bool) -> None:
        assert has_entity_designator(name) is expected


class TestHeadcountBand:
    """OQ-3: an inclusive range, as the source stated it; bucketing is T9's mapping."""

    def test_an_inverted_range_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="inverted"):
            HeadcountBand(low=50, high=11)

    def test_zero_people_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            HeadcountBand(low=0, high=5)

    @pytest.mark.parametrize(
        ("band", "label"),
        [
            (HeadcountBand(low=11, high=50), "11-50"),
            (HeadcountBand(low=50), "50+"),
            (HeadcountBand(low=12, high=12), "12"),
        ],
    )
    def test_labels(self, band: HeadcountBand, label: str) -> None:
        assert band.label == label

    def test_an_open_band_survives_the_stored_round_trip(self) -> None:
        """``to_stored`` drops ``None`` at the top level only; an open ``high`` stays inside."""
        candidate = a_candidate().model_copy(
            update={
                "headcount_band": sourced(
                    HeadcountBand(low=50),
                    "https://www.acmefreight.test/about",
                    RetrievalMethod.llm_inference,
                )
            }
        )
        restored = CandidateFields.model_validate(candidate.to_stored())
        assert restored.headcount_band is not None
        assert restored.headcount_band.value == HeadcountBand(low=50, high=None)


class TestTheNewFieldsUnderD13:
    """The D13 walk covers every field, the new ones included."""

    def test_an_email_cited_to_google_maps_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("1234567"),
                email=sourced("a@acme.test", PLACES_URL, RetrievalMethod.web_lookup),
            )

    def test_a_headcount_cited_to_google_maps_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields(
                registry_id=sourced("1234567"),
                headcount_band=sourced(
                    HeadcountBand(low=11, high=50), PLACES_URL, RetrievalMethod.web_lookup
                ),
            )


MOVED = PostalAddress(street="900 Main St", city="Fort Worth", state="TX", postal_code="76102")


class TestPlacesVerdict:
    """Our verdict on a Places miss (website-verification OQ-1, OQ-2)."""

    def test_it_holds_a_kind_a_place_id_and_our_own_address_only(self) -> None:
        assert set(PlacesVerdict.model_fields) == {"kind", "place_id", "checked_address"}

    def test_a_blank_place_id_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="place_id"):
            PlacesVerdict(kind=MissKind.mail_drop, place_id="  ", checked_address=ARLINGTON)

    def test_it_may_cite_google_maps(self) -> None:
        """D13's allow-list holds two fields now: each keeps only a place ID."""
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict()})
        assert CandidateFields.model_validate(fields.model_dump(mode="json")).places_verdict

    def test_a_website_cited_to_google_maps_is_still_refused(self) -> None:
        payload = a_candidate().model_dump(mode="json")
        payload["website"] = sourced("https://acme.test", PLACES_URL).model_dump(mode="json")
        with pytest.raises(ValidationError, match="D13"):
            CandidateFields.model_validate(payload)

    def test_a_verdict_for_the_current_address_is_current(self) -> None:
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict()})
        assert fields.has_current_verdict()
        assert fields.has_mail_drop_here()

    def test_a_verdict_for_an_old_address_is_not(self) -> None:
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict(address=MOVED)})
        assert not fields.has_current_verdict()
        assert not fields.has_mail_drop_here()

    @pytest.mark.parametrize("kind", [MissKind.no_listing, MissKind.other_business])
    def test_only_a_mail_drop_is_a_mail_drop(self, kind: MissKind) -> None:
        fields = a_candidate().model_copy(update={"places_verdict": a_verdict(kind)})
        assert not fields.has_mail_drop_here()


class TestWebsiteVerification:
    """B1, B2, OQ-3: the predicates every paid step, routing and T9 ask."""

    def test_a_homepage_on_the_email_domain_verifies(self) -> None:
        fields = website_verified(a_candidate())
        assert fields.has_own_domain_website()
        assert fields.is_verified()

    def test_the_bare_host_matches_too(self) -> None:
        fields = website_verified(a_candidate()).model_copy(
            update={"website": own_site(f"https://{OWN_DOMAIN}")}
        )
        assert fields.has_own_domain_website()

    def test_a_homepage_on_another_domain_does_not(self) -> None:
        fields = website_verified(a_candidate()).model_copy(
            update={"website": own_site("https://www.acme.test")}
        )
        assert not fields.has_own_domain_website()
        assert not fields.is_verified()

    def test_a_homepage_the_judgment_node_found_does_not(self) -> None:
        """Counting ``llm_inference`` would make verification circular for the next stage."""
        fields = website_verified(a_candidate()).model_copy(
            update={"website": own_site(f"https://www.{OWN_DOMAIN}", RetrievalMethod.llm_inference)}
        )
        assert not fields.has_own_domain_website()

    def test_a_free_mail_email_does_not(self) -> None:
        fields = website_verified(a_candidate()).model_copy(
            update={"email": sourced("acme@gmail.com")}
        )
        assert not fields.has_own_domain_website()

    def test_an_isps_own_homepage_does_not(self) -> None:
        """PR #36 M3: ``earthlink.net`` answers 2xx HTML, so the list is the only fence."""
        fields = website_verified(a_candidate()).model_copy(
            update={
                "email": sourced("acme@earthlink.net"),
                "website": own_site("https://www.earthlink.net"),
            }
        )
        assert not fields.has_own_domain_website()
        assert not fields.is_verified()

    def test_no_email_does_not(self) -> None:
        fields = website_verified(a_candidate()).model_copy(update={"email": None})
        assert not fields.has_own_domain_website()

    def test_places_alone_verifies(self) -> None:
        fields = a_candidate().model_copy(update={"business_check": place_checked()})
        assert fields.is_verified()

    def test_neither_does_not(self) -> None:
        assert not a_candidate().is_verified()

    def test_places_verified_is_routable(self) -> None:
        fields = a_candidate().model_copy(update={"business_check": place_checked()})
        assert fields.has_routable_address()

    def test_website_verified_with_no_verdict_is_routable(self) -> None:
        assert website_verified(a_candidate()).has_routable_address()

    @pytest.mark.parametrize("kind", [MissKind.no_listing, MissKind.other_business])
    def test_website_verified_with_a_door_is_routable(self, kind: MissKind) -> None:
        fields = website_verified(a_candidate()).model_copy(
            update={"places_verdict": a_verdict(kind)}
        )
        assert fields.has_routable_address()

    def test_website_verified_at_a_mail_drop_is_not_routable(self) -> None:
        fields = website_verified(a_candidate()).model_copy(update={"places_verdict": a_verdict()})
        assert fields.is_verified()
        assert not fields.has_routable_address()

    def test_a_mail_drop_at_an_old_address_does_not_block(self) -> None:
        fields = website_verified(a_candidate()).model_copy(
            update={"places_verdict": a_verdict(address=MOVED)}
        )
        assert fields.has_routable_address()

    def test_website_verified_with_no_address_is_not_routable(self) -> None:
        fields = website_verified(a_candidate()).model_copy(update={"address": None})
        assert not fields.has_routable_address()


class TestNameWords:
    """The name-word test, moved from the Places adapter so the owner gate can share it."""

    def test_designators_stop_words_and_short_tokens_are_dropped(self) -> None:
        assert name_words("The Acme Freight of TX LLC") == {"ACME", "FREIGHT"}

    def test_a_short_name_shares_all_of_its_words(self) -> None:
        assert shares_name(frozenset({"ACME"}), frozenset({"ACME", "FREIGHT"}))
        assert not shares_name(frozenset({"ACME", "FREIGHT"}), frozenset({"ACME"}))

    def test_an_empty_name_is_vacuously_shared(self) -> None:
        """Documented, so callers that may hold an empty set guard it themselves (OQ-4)."""
        assert shares_name(frozenset(), frozenset({"ACME"}))
