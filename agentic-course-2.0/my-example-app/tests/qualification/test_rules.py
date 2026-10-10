"""The disqualifier evaluator, without a database: every operator, cast and way to be unsure."""

import pytest

from app.manifests.schemas import RuleOperator, SourceKind
from app.qualification.exceptions import RuleNotEvaluableError
from app.qualification.rules import apply_predicates, evaluate, predicate_rules
from app.qualification.schemas import OutcomeKind, RecordSet
from tests.qualification.builders import (
    DFW_COUNTIES,
    FREIGHT_V3_PREDICATES,
    a_body_with,
    census_row,
    judgment,
    predicate,
)

FIRED = OutcomeKind.fired
PASSED = OutcomeKind.passed
UNSURE = OutcomeKind.not_evaluable


type RuleValue = str | tuple[str, ...] | int | bool | None


def _kind(rule_operator: RuleOperator, value: RuleValue, text: str | None) -> OutcomeKind:
    rule = predicate("r", "f", rule_operator, value)
    return evaluate(rule, {"fmcsa": {"f": text}}, source="fmcsa").kind


class TestOperators:
    @pytest.mark.parametrize(
        ("operator", "value", "text", "expected"),
        [
            (RuleOperator.equals, "A", "A", FIRED),
            (RuleOperator.equals, "A", " A ", FIRED),
            (RuleOperator.equals, "A", "a", PASSED),
            (RuleOperator.equals, 7, "7", FIRED),
            (RuleOperator.not_equals, "A", "B", FIRED),
            (RuleOperator.not_equals, "A", "A", PASSED),
            (RuleOperator.contains, "B", "C;B", FIRED),
            (RuleOperator.contains, "B", "C", PASSED),
            (RuleOperator.not_contains, "B", "C;B", PASSED),
            (RuleOperator.not_contains, "B", "C", FIRED),
            (RuleOperator.not_contains, "B", "B", PASSED),
            (RuleOperator.in_set, ("085", "113"), "113", FIRED),
            (RuleOperator.in_set, ("085", "113"), "201", PASSED),
            (RuleOperator.not_in_set, DFW_COUNTIES, "201", FIRED),
            (RuleOperator.not_in_set, DFW_COUNTIES, " 113 ", PASSED),
            (RuleOperator.greater_than, 10, "11", FIRED),
            (RuleOperator.greater_than, 10, "10", PASSED),
            (RuleOperator.greater_than, 10, " 7 ", PASSED),
            (RuleOperator.greater_than, 10, "10.5", FIRED),
            (RuleOperator.less_than, 3, "2", FIRED),
            (RuleOperator.less_than, 3, "3", PASSED),
            (RuleOperator.is_true, None, "Y", FIRED),
            (RuleOperator.is_true, None, "true", FIRED),
            (RuleOperator.is_true, None, "N", PASSED),
            (RuleOperator.is_true, True, "0", PASSED),
        ],
    )
    def test_operator(
        self, operator: RuleOperator, value: RuleValue, text: str, expected: OutcomeKind
    ) -> None:
        assert _kind(operator, value, text) is expected

    def test_numeric_compare_is_numeric_not_lexical(self) -> None:
        """As strings, "9" > "10". The census sends text, so the cast is the whole point."""
        assert _kind(RuleOperator.greater_than, 10, "9") is PASSED


class TestUnjudgeableRecordsAreNeverDisqualified:
    @pytest.mark.parametrize("text", [None, "", "   "])
    def test_a_blank_or_absent_field(self, text: str | None) -> None:
        assert _kind(RuleOperator.not_contains, "B", text) is UNSURE

    @pytest.mark.parametrize("text", ["N/A", "ten", "NaN", "inf"])
    def test_text_where_a_number_belongs(self, text: str) -> None:
        rule = predicate("asset_based_carrier", "power_units", RuleOperator.greater_than, 10)
        outcome = evaluate(rule, {"fmcsa": {"power_units": text}}, source="fmcsa")
        assert outcome.kind is UNSURE
        assert outcome.reason == "not_numeric"
        assert outcome.observed == text

    def test_text_that_is_not_a_boolean(self) -> None:
        assert _kind(RuleOperator.is_true, None, "maybe") is UNSURE

    def test_a_missing_source(self) -> None:
        rule = predicate("not_allowed", "allowToOperate", RuleOperator.is_true, source="qcmobile")
        outcome = evaluate(rule, {"fmcsa": census_row()}, source="qcmobile")
        assert outcome.kind is UNSURE
        assert outcome.reason == "source_missing"


class TestMalformedRules:
    @pytest.mark.parametrize(
        ("operator", "value"),
        [
            (RuleOperator.in_set, "085"),
            (RuleOperator.not_in_set, ()),
            (RuleOperator.greater_than, "10"),
            (RuleOperator.greater_than, True),
            (RuleOperator.contains, ""),
            (RuleOperator.equals, ("a",)),
            (RuleOperator.equals, None),
            (RuleOperator.is_true, False),
        ],
    )
    def test_a_value_that_cannot_fit_the_operator_raises(
        self, operator: RuleOperator, value: RuleValue
    ) -> None:
        """``bool`` is an ``int`` in Python; a numeric compare against ``True`` is still wrong."""
        with pytest.raises(RuleNotEvaluableError):
            _kind(operator, value, "5")

    def test_a_judgment_rule_is_not_a_predicate(self) -> None:
        with pytest.raises(RuleNotEvaluableError):
            evaluate(judgment(), {"fmcsa": census_row()}, source="fmcsa")


class TestApplyPredicates:
    def test_freight_v3_keeps_a_small_dfw_broker(self) -> None:
        body = a_body_with(*FREIGHT_V3_PREDICATES)
        result = apply_predicates(body, {"fmcsa": census_row()})
        assert [o.kind for o in result.outcomes] == [PASSED, PASSED, PASSED]
        assert result.first_fired() is None

    @pytest.mark.parametrize(
        ("row", "first"),
        [
            (census_row(phy_cnty="201"), "outside_dfw_metro"),
            (census_row(carship="C"), "no_broker_entity_type"),
            (census_row(power_units="11"), "asset_based_carrier"),
            (census_row(phy_cnty="201", power_units="40"), "outside_dfw_metro"),
        ],
    )
    def test_the_first_rule_in_declaration_order_is_the_one_that_fired(
        self, row: dict[str, str | None], first: str
    ) -> None:
        result = apply_predicates(a_body_with(*FREIGHT_V3_PREDICATES), {"fmcsa": row})
        fired = result.first_fired()
        assert fired is not None
        assert fired.rule_id == first

    def test_every_rule_is_evaluated_even_after_one_fires(self) -> None:
        """The dry-run counts each rule standalone, which needs every outcome."""
        row = census_row(phy_cnty="201", carship="C", power_units="40")
        result = apply_predicates(a_body_with(*FREIGHT_V3_PREDICATES), {"fmcsa": row})
        assert [o.kind for o in result.outcomes] == [FIRED, FIRED, FIRED]

    def test_judgment_rules_are_skipped(self) -> None:
        body = a_body_with(judgment(), *FREIGHT_V3_PREDICATES)
        assert [rule.id for rule in predicate_rules(body)] == [
            rule.id for rule in FREIGHT_V3_PREDICATES
        ]
        assert len(apply_predicates(body, {"fmcsa": census_row()}).outcomes) == 3

    def test_a_rule_reads_its_declared_source(self) -> None:
        """``allowToOperate`` is a QCMobile field, not a census column."""
        rule = predicate("not_allowed", "allowToOperate", RuleOperator.is_true, source="qcmobile")
        body = a_body_with(
            rule,
            sources=(("fmcsa", SourceKind.bulk_file), ("qcmobile", SourceKind.registry_api)),
        )
        records: RecordSet = {"fmcsa": census_row(), "qcmobile": {"allowToOperate": "N"}}
        assert apply_predicates(body, records).outcomes[0].kind is PASSED
        records = {"fmcsa": census_row(), "qcmobile": {"allowToOperate": "Y"}}
        assert apply_predicates(body, records).outcomes[0].kind is FIRED

    def test_a_rule_without_a_source_reads_the_first_declared(self) -> None:
        body = a_body_with(
            predicate("no_broker_entity_type", "carship", RuleOperator.not_contains, "B"),
            sources=(("fmcsa", SourceKind.bulk_file), ("qcmobile", SourceKind.registry_api)),
        )
        outcome = apply_predicates(body, {"fmcsa": census_row(carship="C")}).outcomes[0]
        assert outcome.source == "fmcsa"
        assert outcome.kind is FIRED

    def test_unjudgeable_records_are_reported(self) -> None:
        body = a_body_with(*FREIGHT_V3_PREDICATES)
        result = apply_predicates(body, {"fmcsa": census_row(power_units="")})
        assert [o.rule_id for o in result.not_evaluable()] == ["asset_based_carrier"]
        assert result.first_fired() is None
