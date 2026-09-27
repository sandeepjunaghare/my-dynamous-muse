"""The load-bearing suite.

Nine tickets consume ``ProvenancedValue``. Everything asserted here is a property later slices are
entitled to assume, so a failure in this file is a failure of the write-gate itself.

Invalid constructions go through ``model_validate`` rather than the constructor. Both run the same
validation, but the constructor is statically typed — passing it a deliberately wrong value would
be a type error that only a suppression could silence, and this project allows none.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.shared.provenance import ProvenancedValue, RetrievalMethod, is_promotable

SOURCE = "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx?USDOT=123456"


def _now() -> datetime:
    return datetime.now(UTC)


def _fields(**overrides: object) -> dict[str, object]:
    """A complete, valid set of fields, with any of them overridden or removed by the caller."""
    fields: dict[str, object] = {
        "value": "Acme Freight Brokerage",
        "source_url": SOURCE,
        "retrieved_at": _now(),
        "retrieval_method": RetrievalMethod.registry_api,
    }
    fields.update(overrides)
    return fields


def _value(**overrides: object) -> ProvenancedValue[str]:
    return ProvenancedValue[str].model_validate(_fields(**overrides))


class TestConstruction:
    def test_a_fully_provenanced_value_constructs(self) -> None:
        """The happy path, built through the typed constructor rather than `model_validate`."""
        retrieved_at = _now()
        provenanced = ProvenancedValue[str](
            value="Acme Freight Brokerage",
            source_url=SOURCE,
            retrieved_at=retrieved_at,
            retrieval_method=RetrievalMethod.registry_api,
        )

        assert provenanced.value == "Acme Freight Brokerage"
        assert provenanced.source_url == SOURCE
        assert provenanced.retrieved_at == retrieved_at
        assert provenanced.retrieval_method is RetrievalMethod.registry_api


class TestRequiredFields:
    """Omitting any one provenance field must make construction impossible."""

    @pytest.mark.parametrize("missing", ["value", "source_url", "retrieved_at", "retrieval_method"])
    def test_missing_field_raises(self, missing: str) -> None:
        fields = _fields()
        del fields[missing]

        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue[str].model_validate(fields)

        assert missing in str(exc_info.value)

    def test_blank_source_url_raises(self) -> None:
        """A blank source is no source — the gate would otherwise pass on an empty citation."""
        with pytest.raises(ValidationError):
            _value(source_url="   ")

    def test_source_url_is_stripped(self) -> None:
        assert _value(source_url=f"  {SOURCE}  ").source_url == SOURCE


class TestFrozen:
    """A citation is a fact about how a value was obtained; it is not editable afterwards."""

    def test_mutating_value_raises(self) -> None:
        provenanced = _value()
        with pytest.raises(ValidationError):
            provenanced.value = "Something Else"

    def test_mutating_provenance_raises(self) -> None:
        provenanced = _value()
        with pytest.raises(ValidationError):
            provenanced.source_url = "https://example.invalid/"


class TestRetrievedAt:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _value(retrieved_at=datetime(2026, 9, 26, 12, 0, 0))
        assert "timezone-aware" in str(exc_info.value)

    def test_non_utc_timezone_is_accepted_and_normalised(self) -> None:
        """A non-UTC timestamp is normalised, not rejected — sources report in local time."""
        central = timezone(timedelta(hours=-5))
        provenanced = _value(retrieved_at=datetime(2026, 9, 26, 12, 0, 0, tzinfo=central))

        assert provenanced.retrieved_at.tzinfo is UTC
        assert provenanced.retrieved_at == datetime(2026, 9, 26, 17, 0, 0, tzinfo=UTC)


class Owner(BaseModel):
    """A stand-in for the kind of model T6's resolve_owner will provenance.

    Frozen, because a ProvenancedValue refuses to hold anything editable.
    """

    model_config = ConfigDict(frozen=True)

    first_name: str
    last_name: str


class MutableOwner(BaseModel):
    """The same shape without `frozen=True` — the thing that must now be refused."""

    first_name: str
    last_name: str


class TestGenericParameterisation:
    """The point of the generic is that ``T`` is actually validated."""

    def test_wrong_type_for_int_parameter_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProvenancedValue[int].model_validate(_fields(value="abc"))

    def test_nested_model_is_validated(self) -> None:
        with pytest.raises(ValidationError):
            ProvenancedValue[Owner].model_validate(_fields(value={"first_name": "Dana"}))

    def test_nested_model_round_trips(self) -> None:
        provenanced = ProvenancedValue[Owner](
            value=Owner(first_name="Dana", last_name="Reyes"),
            source_url=SOURCE,
            retrieved_at=_now(),
            retrieval_method=RetrievalMethod.llm_inference,
        )
        assert provenanced.value.last_name == "Reyes"


class TestRetrievalMethod:
    def test_manual_hubspot_entry_exists(self) -> None:
        """T13 adopts hand-typed records through this member. Asserted so it survives a cleanup."""
        assert RetrievalMethod.manual_hubspot_entry == "manual_hubspot_entry"

    def test_every_documented_method_exists(self) -> None:
        expected = {
            "registry_api",
            "bulk_file",
            "web_lookup",
            "llm_inference",
            "manual_hubspot_entry",
        }
        assert expected <= {member.value for member in RetrievalMethod}

    def test_unknown_method_is_rejected(self) -> None:
        """A bare string would let this typo through; the StrEnum does not."""
        with pytest.raises(ValidationError):
            _value(retrieval_method="registry_apo")


class TestIsPromotable:
    def test_none_is_not_promotable(self) -> None:
        assert is_promotable(None) is False

    def test_fully_provenanced_value_is_promotable(self) -> None:
        assert is_promotable(_value()) is True

    def test_docstring_records_the_field_writes_not_tasks_distinction(self) -> None:
        """The distinction the whole cadence line depends on must stay written down.

        T13 adopts records with zero citable fields. If someone reads this gate as governing task
        creation, adoption is refused outright and the 22 stranded prospects stay stranded.
        """
        doc = is_promotable.__doc__
        assert doc is not None
        assert "field writes, not task creation" in doc


class TestValueImmutability:
    """Finding #1 from the PR #2 review: `frozen=True` alone is shallow.

    It stops this model's fields being reassigned but would happily hold a mutable `value` whose
    contents change after `is_promotable()` approved them — E18 recurring through the very
    primitive built to prevent it. These tests pin the fix.
    """

    def test_unfrozen_nested_model_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue[MutableOwner].model_validate(
                _fields(value=MutableOwner(first_name="Dana", last_name="Reyes"))
            )
        assert "frozen=True" in str(exc_info.value)

    def test_frozen_nested_model_is_accepted_and_cannot_drift(self) -> None:
        provenanced = ProvenancedValue[Owner](
            value=Owner(first_name="Dana", last_name="Reyes"),
            source_url=SOURCE,
            retrieved_at=_now(),
            retrieval_method=RetrievalMethod.llm_inference,
        )
        assert is_promotable(provenanced) is True

        # The regression itself: this used to succeed silently.
        with pytest.raises(ValidationError):
            provenanced.value.first_name = "SOMEONE ELSE"
        assert provenanced.value.first_name == "Dana"

    @pytest.mark.parametrize(
        "mutable",
        [["a", "b"], {"k": "v"}, {"a", "b"}],
        ids=["list", "dict", "set"],
    )
    def test_mutable_containers_are_rejected(self, mutable: object) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue[object].model_validate(_fields(value=mutable))
        assert "mutable" in str(exc_info.value)

    def test_immutable_containers_are_accepted(self) -> None:
        assert ProvenancedValue[tuple[str, ...]].model_validate(
            _fields(value=("214-555-0100", "214-555-0101"))
        )
        assert ProvenancedValue[frozenset[str]].model_validate(_fields(value=frozenset({"a"})))

    def test_a_mutable_field_inside_a_frozen_model_is_still_caught(self) -> None:
        """The walk is recursive — a frozen wrapper around a list is not immutable."""

        class Leaky(BaseModel):
            model_config = ConfigDict(frozen=True)
            aliases: list[str]

        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue[Leaky].model_validate(_fields(value=Leaky(aliases=["x"])))
        assert "value.aliases" in str(exc_info.value)

    def test_scalars_still_work(self) -> None:
        for scalar in ("Acme", 42, 3.14, True, None):
            assert ProvenancedValue[object].model_validate(_fields(value=scalar))
