"""The two places a predicate's meaning lives must agree on an absent field (T7 Phase 6).

T5 applies census rules in SoQL (``app/sourcing/sources/socrata.py``); T7 applies every rule in
Python (``app/qualification/rules.py``), including in the dry-run. If they read a missing value
differently, the dry-run's funnel and the live pool disagree, and nothing else would notice. The
SQL keeps a row with a null field exactly when its keep clause says ``IS NULL OR``; the evaluator
must fire on an absent field exactly when the SQL would drop that row.
"""

import pytest

from app.manifests.schemas import RuleOperator
from app.qualification.rules import evaluate
from app.qualification.schemas import OutcomeKind
from app.sourcing.sources.socrata import pushdown_where
from tests.qualification.builders import predicate

PUSHABLE = [
    (RuleOperator.equals, "I"),
    (RuleOperator.not_equals, "TX"),
    (RuleOperator.in_set, ("085", "113")),
    (RuleOperator.not_in_set, ("085", "113")),
    (RuleOperator.contains, "B"),
    (RuleOperator.not_contains, "B"),
    (RuleOperator.greater_than, 10),
    (RuleOperator.less_than, 10),
]


@pytest.mark.parametrize(("operator", "value"), PUSHABLE)
def test_the_evaluator_fires_on_an_absent_field_exactly_when_soql_drops_the_row(
    operator: RuleOperator, value: str | tuple[str, ...] | int
) -> None:
    rule = predicate("r", "f", operator, value)
    pushdown = pushdown_where([rule], columns=frozenset({"f"}), integer_columns=frozenset({"f"}))
    assert pushdown.pushed_down == ("r",), "every operator listed here must push down"
    assert pushdown.where is not None
    soql_drops_a_null = "IS NULL OR" not in pushdown.where

    outcome = evaluate(rule, {"census": {"f": None}}, source="census")

    assert (outcome.kind is OutcomeKind.fired) is soql_drops_a_null
