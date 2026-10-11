"""The disqualifier evaluator, without a database: every operator, cast and way to be unsure."""

import pytest

from app.manifests.schemas import RuleOperator, SourceKind
from app.qualification.exceptions import BatchWideDisqualificationError, RuleNotEvaluableError
from app.qualification.rules import (
    MIN_SAMPLE,
    apply_predicates,
    check_batch,
    evaluate,
    predicate_rules,
)
from app.qualification.schemas import OutcomeKind, PredicateResult, RecordSet
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


class TestAnAbsentFieldOnAFoundRecord:
    """Owner decision (T7 Phase 6): "not X" holds of a missing value, exactly as T5's SoQL pushdown
    reads the same rule, so the dry-run and the live pool cannot disagree."""

    @pytest.mark.parametrize("text", [None, "", "   "])
    @pytest.mark.parametrize(
        ("operator", "value"),
        [
            (RuleOperator.not_equals, "A"),
            (RuleOperator.not_in_set, ("A", "P")),
            (RuleOperator.not_contains, "B"),
        ],
    )
    def test_fires_a_negated_operator(
        self, operator: RuleOperator, value: RuleValue, text: str | None
    ) -> None:
        rule = predicate("r", "f", operator, value)
        outcome = evaluate(rule, {"fmcsa": {"f": text}}, source="fmcsa")
        assert outcome.kind is FIRED
        assert outcome.observed is None
        assert outcome.reason is None

    def test_fires_when_the_key_is_not_in_the_record_at_all(self) -> None:
        rule = predicate("r", "brokerAuthorityStatus", RuleOperator.not_equals, "A")
        assert evaluate(rule, {"qcmobile": {"legalName": "X"}}, source="qcmobile").kind is FIRED


class TestUnjudgeableRecordsAreNeverDisqualified:
    @pytest.mark.parametrize("text", [None, "", "   "])
    @pytest.mark.parametrize(
        ("operator", "value"),
        [
            (RuleOperator.equals, "N"),
            (RuleOperator.in_set, ("I", "N")),
            (RuleOperator.contains, "B"),
            (RuleOperator.greater_than, 10),
            (RuleOperator.less_than, 10),
            (RuleOperator.is_true, None),
        ],
    )
    def test_a_blank_or_absent_field_for_a_positive_operator(
        self, operator: RuleOperator, value: RuleValue, text: str | None
    ) -> None:
        assert _kind(operator, value, text) is UNSURE

    @pytest.mark.parametrize(
        "operator", [RuleOperator.not_equals, RuleOperator.equals, RuleOperator.not_contains]
    )
    def test_a_missing_source_even_for_a_negated_operator(self, operator: RuleOperator) -> None:
        """No record at all (never looked, lookup failed, found nothing) is never a drop."""
        rule = predicate("r", "brokerAuthorityStatus", operator, "A", source="qcmobile")
        outcome = evaluate(rule, {"fmcsa": census_row()}, source="qcmobile")
        assert outcome.kind is UNSURE
        assert outcome.reason == "source_missing"

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
        rule = predicate(
            "not_allowed_to_operate",
            "allowedToOperate",
            RuleOperator.equals,
            "N",
            source="qcmobile",
        )
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
        """``allowedToOperate`` is a QCMobile field, not a census column."""
        rule = predicate(
            "not_allowed_to_operate",
            "allowedToOperate",
            RuleOperator.equals,
            "N",
            source="qcmobile",
        )
        body = a_body_with(
            rule,
            sources=(("fmcsa", SourceKind.bulk_file), ("qcmobile", SourceKind.registry_api)),
        )
        records: RecordSet = {"fmcsa": census_row(), "qcmobile": {"allowedToOperate": "Y"}}
        assert apply_predicates(body, records).outcomes[0].kind is PASSED
        records = {"fmcsa": census_row(), "qcmobile": {"allowedToOperate": "N"}}
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


class TestCheckBatch:
    """The circuit breaker (PR #18 review, M1): a rule that fires on nearly the whole batch it can
    judge is presumed wrong, and refused before anything is recorded."""

    BROKER = predicate("broker", "brokerAuthorityStatus", RuleOperator.not_equals, "A", source="qc")
    TYPO = predicate("typo", "brokerAuthorityStatsu", RuleOperator.not_equals, "A", source="qc")

    def _results(self, *records: RecordSet) -> list[PredicateResult]:
        body = a_body_with(self.BROKER, self.TYPO, sources=(("qc", SourceKind.registry_api),))
        return [apply_predicates(body, record) for record in records]

    def test_a_field_absent_on_every_found_record_trips(self) -> None:
        records = [{"qc": {"brokerAuthorityStatus": "A"}}] * MIN_SAMPLE
        with pytest.raises(
            BatchWideDisqualificationError, match="'typo' fired on 5 of the 5"
        ) as exc:
            check_batch(self._results(*records))
        assert (exc.value.fired, exc.value.judged) == (MIN_SAMPLE, MIN_SAMPLE)

    def test_a_real_value_firing_on_nearly_everything_trips_too(self) -> None:
        """A value spelled in the wrong case (``'a'``) fires on every ``'A'`` — the same mistake."""
        records = [{"qc": {"brokerAuthorityStatus": "I", "brokerAuthorityStatsu": "A"}}] * 9 + [
            {"qc": {"brokerAuthorityStatus": "A", "brokerAuthorityStatsu": "A"}}
        ]
        with pytest.raises(BatchWideDisqualificationError, match="'broker' fired on 9 of the 10"):
            check_batch(self._results(*records))

    def test_under_the_share_passes(self) -> None:
        records = [{"qc": {"brokerAuthorityStatus": "I", "brokerAuthorityStatsu": "A"}}] * 8 + [
            {"qc": {"brokerAuthorityStatus": "A", "brokerAuthorityStatsu": "A"}}
        ] * 2
        check_batch(self._results(*records))

    def test_below_the_sample_nothing_trips(self) -> None:
        """One small batch must not jam the backlog by refusing forever."""
        records: list[RecordSet] = [{"qc": {}}] * (MIN_SAMPLE - 1)
        check_batch(self._results(*records))

    def test_a_candidate_without_the_source_is_not_counted(self) -> None:
        """QCMobile not knowing a carrier says nothing about a QCMobile rule — so twenty unknown
        carriers neither dilute a trip nor make up the sample."""
        unknown: RecordSet = {}
        found: RecordSet = {"qc": {"brokerAuthorityStatus": "A"}}
        with pytest.raises(BatchWideDisqualificationError, match="'typo' fired on 5 of the 5"):
            check_batch(self._results(*([unknown] * 20), *([found] * MIN_SAMPLE)))
        check_batch(self._results(*([unknown] * 20), *([found] * (MIN_SAMPLE - 1))))
