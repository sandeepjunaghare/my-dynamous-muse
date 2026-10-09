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
    RunCostSummary,
    RunOutcome,
    RunStatus,
    RunTotals,
    SourcingBrief,
)
from tests.sourcing.builders import a_brief, a_candidate, sourced

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
        assert candidate.unprovenanced_fields() == ("phone", "website")

    def test_a_fully_cited_candidate_has_none(self) -> None:
        candidate = CandidateFields(
            registry_id=sourced("1"),
            legal_name=sourced("A"),
            dba_name=sourced("B"),
            address=a_candidate().address,
            phone=sourced("1"),
            website=sourced("https://example.com"),
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
