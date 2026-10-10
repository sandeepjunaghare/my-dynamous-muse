"""The manifest schema is where "vertical is data" either holds or silently doesn't.

Two properties carry the load and both are checked here: **every researched field is cited**, and
**nothing inside a citation can be edited afterwards**. Everything else in the slice trusts them.

Deliberately-invalid input is built with ``model_validate``, not the typed constructor: the
constructor would be a static type error, and this project allows no suppression to silence one.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

from app.manifests.schemas import (
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestSource,
    QualifyingSignal,
    RuleKind,
    RuleOperator,
    ScoreAxis,
    SourceKind,
    SourceTerms,
    TermsDecision,
    Vocabulary,
)
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from tests.manifests.builders import a_body, cited

RETRIEVED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _cited[T](value: T) -> ProvenancedValue[T]:
    return ProvenancedValue(
        value=value,
        source_url="https://safer.fmcsa.dot.gov/",
        retrieved_at=RETRIEVED_AT,
        retrieval_method=RetrievalMethod.manual_research,
    )


def _source(name: str = "fmcsa") -> ManifestSource:
    return ManifestSource(
        name=name,
        kind=SourceKind.bulk_file,
        description="the census file",
        base_url="https://safer.fmcsa.dot.gov/",
    )


def _body(**overrides: object) -> ManifestBody:
    """A minimal valid body, with individual fields overridable per test."""
    fields: dict[str, object] = {
        "sources": (_cited(_source()),),
        "icp_band": _cited(
            IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True)
        ),
        "vocabulary": _cited(Vocabulary(terms=("loads", "lanes"))),
    }
    fields.update(overrides)
    return ManifestBody.model_validate(fields)


def _terms(source_name: str, decision: TermsDecision = TermsDecision.accepted) -> SourceTerms:
    return SourceTerms(
        source_name=source_name,
        decision=decision,
        decided_by="sandeep",
        decided_at=datetime(2026, 9, 27, 9, 0, tzinfo=UTC),
    )


class TestCitationIsMandatory:
    def test_a_source_without_provenance_cannot_be_wrapped(self) -> None:
        """The whole primitive: no source_url, no citation, no value."""
        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue.model_validate({"value": _source()})
        message = str(exc_info.value)
        assert "source_url" in message
        assert "retrieved_at" in message

    def test_a_body_field_cannot_hold_a_bare_value(self) -> None:
        """`sources` takes citations, not sources — an uncited source is not representable."""
        with pytest.raises(ValidationError):
            _body(sources=(_source(),))


class TestImmutabilityInsideACitation:
    def test_a_list_inside_a_cited_value_is_rejected(self) -> None:
        """`_assert_immutable` refuses a list: a citation for an editable value is not one."""
        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue.model_validate(
                {
                    "value": ["loads", "lanes"],
                    "source_url": "https://safer.fmcsa.dot.gov/",
                    "retrieved_at": RETRIEVED_AT,
                    "retrieval_method": RetrievalMethod.manual_research,
                }
            )
        assert "mutable" in str(exc_info.value)

    def test_a_non_frozen_nested_model_is_rejected(self) -> None:
        class Mutable(BaseModel):
            name: str

        with pytest.raises(ValidationError) as exc_info:
            ProvenancedValue.model_validate(
                {
                    "value": Mutable(name="fmcsa"),
                    "source_url": "https://safer.fmcsa.dot.gov/",
                    "retrieved_at": RETRIEVED_AT,
                    "retrieval_method": RetrievalMethod.manual_research,
                }
            )
        assert "frozen=True" in str(exc_info.value)

    def test_every_leaf_model_is_frozen(self) -> None:
        """Each of these is cited somewhere, so each must survive `_assert_immutable`."""
        for model in (ManifestSource, DisqualifierRule, QualifyingSignal, IcpBand, Vocabulary):
            assert model.model_config.get("frozen") is True, model.__name__


class TestLeafValidation:
    def test_icp_band_rejects_an_inverted_range(self) -> None:
        """An inverted band matches nothing, which reads as "the registry was empty"."""
        with pytest.raises(ValidationError) as exc_info:
            IcpBand.model_validate(
                {"headcount_min": 200, "headcount_max": 20, "requires_office_function": True}
            )
        assert "headcount_min" in str(exc_info.value)

    def test_vocabulary_rejects_an_empty_tuple(self) -> None:
        with pytest.raises(ValidationError):
            Vocabulary.model_validate({"terms": ()})

    def test_source_name_must_be_a_slug(self) -> None:
        """`--accept-terms` is typed by hand, so the names have to be typeable."""
        with pytest.raises(ValidationError):
            ManifestSource.model_validate(
                {
                    "name": "FMCSA (SAFER)",
                    "kind": SourceKind.bulk_file,
                    "description": "d",
                    "base_url": "https://safer.fmcsa.dot.gov/",
                }
            )

    def test_a_rule_can_exclude_by_absence(self) -> None:
        """FMCSA's ``carship`` combines codes (``C;B``, ``F;S;B``…). Without a negated contains, the
        only way to say "no broker code" was to list every non-broker combination, and the live
        freight proposal missed about 1,400 Texas records doing exactly that."""
        rule = DisqualifierRule.model_validate(
            {
                "id": "no_broker_entity_type",
                "kind": RuleKind.predicate,
                "description": "d",
                "field": "carship",
                "operator": "not_contains",
                "value": "B",
            }
        )

        assert rule.operator is RuleOperator.not_contains
        assert DisqualifierRule.model_validate_json(rule.model_dump_json()) == rule

    def test_a_rule_can_exclude_outside_a_set(self) -> None:
        """ "Not in the DFW counties" is a geography rule. With only ``in_set``, the rule could name
        the metro but would remove exactly the candidates it was meant to keep."""
        rule = DisqualifierRule.model_validate(
            {
                "id": "outside_dfw_metro",
                "kind": RuleKind.predicate,
                "description": "d",
                "field": "phy_cnty",
                "operator": "not_in_set",
                "value": ("113", "439"),
            }
        )

        assert rule.operator is RuleOperator.not_in_set
        assert DisqualifierRule.model_validate_json(rule.model_dump_json()) == rule

    def test_a_predicate_rule_needs_a_field_and_an_operator(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DisqualifierRule.model_validate(
                {"id": "asset_based", "kind": RuleKind.predicate, "description": "d"}
            )
        assert "predicate" in str(exc_info.value)

    def test_a_judgment_rule_must_not_carry_one(self) -> None:
        """A judgment rule that looks mechanical would be evaluated by T7 instead of the node."""
        with pytest.raises(ValidationError) as exc_info:
            DisqualifierRule.model_validate(
                {
                    "id": "rollup",
                    "kind": RuleKind.judgment,
                    "description": "d",
                    "field": "name",
                    "operator": RuleOperator.contains,
                }
            )
        assert "judgment" in str(exc_info.value)

    def test_terms_decided_at_must_be_timezone_aware(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            SourceTerms.model_validate(
                {
                    "source_name": "fmcsa",
                    "decision": TermsDecision.accepted,
                    "decided_by": "sandeep",
                    "decided_at": datetime(2026, 9, 27, 9, 0),
                }
            )
        assert "timezone-aware" in str(exc_info.value)

    def test_terms_decided_at_is_normalised_to_utc(self) -> None:
        """Two decisions recorded in different zones must still order against each other."""
        central = SourceTerms(
            source_name="fmcsa",
            decision=TermsDecision.accepted,
            decided_by="sandeep",
            decided_at=datetime(2026, 9, 27, 9, 0, tzinfo=timezone(timedelta(hours=-5))),
        )
        assert central.decided_at.tzinfo is UTC
        assert central.decided_at == datetime(2026, 9, 27, 14, 0, tzinfo=UTC)


class TestBodyValidation:
    def test_a_manifest_must_declare_a_source(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _body(sources=())
        assert "at least one source" in str(exc_info.value)

    def test_duplicate_source_names_are_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _body(sources=(_cited(_source()), _cited(_source())))
        assert "duplicate source names" in str(exc_info.value)

    def test_duplicate_rule_ids_are_rejected(self) -> None:
        rule = DisqualifierRule(id="asset_based", kind=RuleKind.judgment, description="d")
        with pytest.raises(ValidationError) as exc_info:
            _body(disqualifier_rules=(_cited(rule), _cited(rule)))
        assert "duplicate disqualifier rule ids" in str(exc_info.value)

    def test_duplicate_signal_ids_are_rejected(self) -> None:
        signal = QualifyingSignal(id="tms", question="which TMS", feeds=ScoreAxis.automatable)
        with pytest.raises(ValidationError) as exc_info:
            _body(qualifying_signals=(_cited(signal), _cited(signal)))
        assert "duplicate qualifying signal ids" in str(exc_info.value)

    def test_terms_for_an_undeclared_source_are_rejected(self) -> None:
        """An orphan decision is how a manifest looks fully decided when it is not."""
        with pytest.raises(ValidationError) as exc_info:
            _body(terms=(_terms("places"),))
        assert "undeclared source" in str(exc_info.value)

    def test_a_source_cannot_be_decided_twice(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _body(terms=(_terms("fmcsa"), _terms("fmcsa", TermsDecision.rejected)))
        assert "more than one terms decision" in str(exc_info.value)


class TestUndecidedSources:
    def test_a_fresh_draft_has_every_source_undecided(self) -> None:
        body = _body(sources=(_cited(_source()), _cited(_source("places"))))
        assert body.undecided_sources() == ("fmcsa", "places")

    def test_an_accepted_source_drops_out(self) -> None:
        body = _body(
            sources=(_cited(_source()), _cited(_source("places"))),
            terms=(_terms("fmcsa"),),
        )
        assert body.undecided_sources() == ("places",)

    def test_a_rejected_source_still_blocks(self) -> None:
        """The question was asked and answered no — that is not permission to use it."""
        body = _body(terms=(_terms("fmcsa", TermsDecision.rejected),))
        assert body.undecided_sources() == ("fmcsa",)


class TestJsonRoundTrip:
    def test_dump_and_revalidate_are_lossless(self) -> None:
        """What the repository does on every read and write.

        `mode="json"` matters: a plain dump leaves `datetime` objects that asyncpg refuses to write
        into JSONB.
        """
        body = _body(
            disqualifier_rules=(
                _cited(
                    DisqualifierRule(
                        id="asset_based",
                        kind=RuleKind.predicate,
                        description="d",
                        field="carrier_operation",
                        operator=RuleOperator.equals,
                        value="asset_based",
                    )
                ),
            ),
            qualifying_signals=(
                _cited(QualifyingSignal(id="tms", question="q", feeds=ScoreAxis.automatable)),
            ),
            terms=(_terms("fmcsa"),),
        )

        restored = ManifestBody.model_validate(body.model_dump(mode="json"))

        assert restored == body
        assert restored.icp_band.retrieved_at == RETRIEVED_AT
        assert restored.icp_band.retrieved_at.tzinfo is UTC
        assert restored.disqualifier_rules[0].value.operator is RuleOperator.equals


class TestRuleSource:
    """Which declared source a predicate reads — ``allowToOperate`` is QCMobile, not the census."""

    def _rule(self, source: str | None) -> DisqualifierRule:
        return DisqualifierRule(
            id="not_allowed",
            kind=RuleKind.predicate,
            description="no operating authority",
            field="allowToOperate",
            operator=RuleOperator.is_true,
            source=source,
        )

    def _body(self, rule: DisqualifierRule) -> ManifestBody:
        return a_body("fmcsa", "qcmobile").model_copy(update={"disqualifier_rules": (cited(rule),)})

    def test_a_rule_may_name_a_declared_source(self) -> None:
        body = ManifestBody.model_validate(self._body(self._rule("qcmobile")).model_dump())
        assert body.source_for(body.disqualifier_rules[0].value) == "qcmobile"

    def test_no_source_means_the_first_declared(self) -> None:
        """Every row written before ``source`` existed meant exactly this."""
        body = ManifestBody.model_validate(self._body(self._rule(None)).model_dump())
        assert body.source_for(body.disqualifier_rules[0].value) == "fmcsa"

    def test_an_undeclared_source_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="undeclared source"):
            ManifestBody.model_validate(self._body(self._rule("socrata")).model_dump())

    def test_a_judgment_rule_must_not_name_a_source(self) -> None:
        with pytest.raises(ValidationError, match="judgment rule"):
            DisqualifierRule(
                id="rollup", kind=RuleKind.judgment, description="rollup", source="fmcsa"
            )

    def test_a_row_without_the_key_still_loads(self) -> None:
        dumped = a_body().model_dump(mode="json")
        for rule in dumped["disqualifier_rules"]:
            rule["value"].pop("source", None)
        assert ManifestBody.model_validate(dumped).disqualifier_rules[0].value.source is None
