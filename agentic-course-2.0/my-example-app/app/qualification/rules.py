"""The disqualifier evaluator: a manifest's free predicate rules, applied to raw source records.

Pure — no I/O, no logging in the hot path (the dry-run calls this ~378k times; callers log the
aggregate). Free, so it runs before anything billable (D8). There are no rule literals and no
vertical names here: every field, operator and value comes from the manifest row.

The semantics, decided once so the dry-run and the pipeline cannot disagree:

* **Text is stripped, then compared exactly and case-sensitively.** Registry fields are codes
  (``C;B``, FIPS county codes), and a case-folded match would quietly widen a rule.
* ``contains`` / ``not_contains`` is a substring test on that text (``'B' in 'C;B'``).
* ``greater_than`` / ``less_than`` cast the field to a number here — census numerics such as
  ``power_units`` arrive as text, and a compare that silently ran on strings would sort ``"9"``
  above ``"10"``.
* **A field absent or blank from a record that exists fires the three negated operators**
  (``not_equals``, ``not_in_set``, ``not_contains``) and no other. That is how T5's SoQL pushdown
  reads the same rule — a null ``phy_state`` is not ``'TX'`` — so the dry-run funnel and the live
  pool agree (owner decision, T7 Phase 6). An entity with no broker authority on file is not ``A``.
* A record the rule cannot judge — its **source** is absent (never looked, the lookup failed, the
  source found nothing), its field is absent for a positive operator, or the text is not the
  number or boolean the operator needs — is :attr:`~OutcomeKind.not_evaluable`, and **that never
  disqualifies**. An unjudgeable record stays in the pool and is counted, so a person sees it.
* A rule whose operator and value do not fit raises :class:`RuleNotEvaluableError`: that is the
  manifest being wrong, not the record.
* A rule that fires on (nearly) every candidate in a batch is presumed wrong too
  (:func:`check_batch`): a field the source never stores is absent everywhere, and the negated
  operators would otherwise disqualify, and permanently suppress, the whole batch.
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import assert_never

from app.manifests.schemas import DisqualifierRule, ManifestBody, RuleKind, RuleOperator
from app.qualification.exceptions import BatchWideDisqualificationError, RuleNotEvaluableError
from app.qualification.schemas import OutcomeKind, PredicateResult, RecordSet, RuleOutcome

NEGATED_OPERATORS = frozenset(
    {RuleOperator.not_equals, RuleOperator.not_in_set, RuleOperator.not_contains}
)
"""The operators an absent field satisfies: "not X" holds of a value that is not there."""

MIN_SAMPLE = 5
"""Fewest judged candidates in a batch before :func:`check_batch` may trip."""

TRIP_SHARE = 0.9
"""Share of a batch's judged candidates one rule must fire on to trip :func:`check_batch`. A rule
that is wrong fires on all of them; a real one that rejects 90% of a pushed-down pool is worth a
person's look before it suppresses that many businesses for good."""

TRUE_TEXT = frozenset({"Y", "YES", "TRUE", "T", "1"})
FALSE_TEXT = frozenset({"N", "NO", "FALSE", "F", "0"})


def predicate_rules(body: ManifestBody) -> tuple[DisqualifierRule, ...]:
    """The manifest's predicate rules, in declaration order. Judgment rules are the node's."""
    return tuple(
        cited.value for cited in body.disqualifier_rules if cited.value.kind is RuleKind.predicate
    )


def check_shape(rule: DisqualifierRule) -> tuple[str, RuleOperator]:
    """Return the rule's field and operator, or raise if its value cannot fit the operator.

    Checked once per rule, so a malformed rule fails before any record is read rather than on
    every record.
    """
    if rule.field is None or rule.operator is None:
        raise RuleNotEvaluableError(
            f"rule {rule.id!r} is not a predicate: it has no field or operator", rule_id=rule.id
        )
    value = rule.value
    operator = rule.operator
    match operator:
        case RuleOperator.equals | RuleOperator.not_equals:
            fits = isinstance(value, str | int) and not isinstance(value, bool)
        case RuleOperator.contains | RuleOperator.not_contains:
            fits = isinstance(value, str) and value.strip() != ""
        case RuleOperator.in_set | RuleOperator.not_in_set:
            fits = isinstance(value, tuple) and len(value) > 0
        case RuleOperator.greater_than | RuleOperator.less_than:
            fits = isinstance(value, int) and not isinstance(value, bool)
        case RuleOperator.is_true:
            fits = value is None or value is True
        case _:
            assert_never(operator)
    if not fits:
        raise RuleNotEvaluableError(
            f"rule {rule.id!r}: {operator.value} cannot compare against {value!r}",
            rule_id=rule.id,
        )
    return rule.field, operator


def _as_number(text: str) -> Decimal | None:
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _as_bool(text: str) -> bool | None:
    upper = text.upper()
    if upper in TRUE_TEXT:
        return True
    if upper in FALSE_TEXT:
        return False
    return None


def _matches(
    operator: RuleOperator, value: str | tuple[str, ...] | int | bool | None, text: str
) -> bool | str:
    """Whether ``text`` matches; or, as a string, why it cannot be judged."""
    match operator:
        case RuleOperator.equals:
            return text == str(value).strip()
        case RuleOperator.not_equals:
            return text != str(value).strip()
        case RuleOperator.contains:
            return str(value) in text
        case RuleOperator.not_contains:
            return str(value) not in text
        case RuleOperator.in_set:
            return isinstance(value, tuple) and text in {item.strip() for item in value}
        case RuleOperator.not_in_set:
            return isinstance(value, tuple) and text not in {item.strip() for item in value}
        case RuleOperator.greater_than | RuleOperator.less_than:
            number = _as_number(text)
            if number is None or not isinstance(value, int):
                return "not_numeric"
            if operator is RuleOperator.greater_than:
                return number > value
            return number < value
        case RuleOperator.is_true:
            flag = _as_bool(text)
            return "not_boolean" if flag is None else flag
        case _:
            assert_never(operator)


def evaluate(rule: DisqualifierRule, records: RecordSet, *, source: str) -> RuleOutcome:
    """One predicate rule's verdict on one business's records."""
    field, operator = check_shape(rule)

    def unjudged(reason: str, observed: str | None = None) -> RuleOutcome:
        return RuleOutcome(
            rule_id=rule.id,
            source=source,
            field=field,
            kind=OutcomeKind.not_evaluable,
            observed=observed,
            reason=reason,
        )

    record = records.get(source)
    if record is None:
        return unjudged("source_missing")
    raw = record.get(field)
    if raw is None or not raw.strip():
        if operator in NEGATED_OPERATORS:
            # `observed=None` is the evidence: the field was looked for and was not there.
            return RuleOutcome(rule_id=rule.id, source=source, field=field, kind=OutcomeKind.fired)
        return unjudged("field_missing")
    text = raw.strip()

    matched = _matches(operator, rule.value, text)
    if isinstance(matched, str):
        return unjudged(matched, text)
    return RuleOutcome(
        rule_id=rule.id,
        source=source,
        field=field,
        kind=OutcomeKind.fired if matched else OutcomeKind.passed,
        observed=text,
    )


def check_batch(
    results: Sequence[PredicateResult],
    *,
    min_sample: int = MIN_SAMPLE,
    trip_share: float = TRIP_SHARE,
) -> None:
    """Raise if one rule fired on (nearly) every candidate in a batch that it could judge.

    "Could judge" means the candidate has a record for the rule's source: a candidate QCMobile did
    not know says nothing about a QCMobile rule. Each rule is counted standalone, not only where it
    was the first to fire. Below ``min_sample`` judged candidates nothing trips, so one unlucky
    small batch cannot jam the backlog.
    """
    judged: dict[str, int] = {}
    fired: dict[str, int] = {}
    seen: dict[str, list[str]] = {}
    for result in results:
        for outcome in result.outcomes:
            if outcome.reason == "source_missing":
                continue
            judged[outcome.rule_id] = judged.get(outcome.rule_id, 0) + 1
            if outcome.kind is OutcomeKind.fired:
                fired[outcome.rule_id] = fired.get(outcome.rule_id, 0) + 1
                seen.setdefault(outcome.rule_id, []).append(
                    "absent" if outcome.observed is None else repr(outcome.observed)
                )
    for rule_id, count in judged.items():
        hits = fired.get(rule_id, 0)
        if count >= min_sample and hits >= trip_share * count:
            examples = ", ".join(sorted(set(seen[rule_id]))[:3])
            raise BatchWideDisqualificationError(
                f"rule {rule_id!r} fired on {hits} of the {count} batch candidates it could judge "
                f"(saw: {examples}) — check its field, value and `source` against a live record. "
                "Nothing was recorded; the run's batch goes back to the backlog",
                rule_id=rule_id,
                fired=hits,
                judged=count,
            )


def apply_predicates(body: ManifestBody, records: RecordSet) -> PredicateResult:
    """Every predicate rule's outcome for one business, in declaration order.

    Evaluates every rule rather than stopping at the first that fires: the dry-run needs each
    rule's standalone matches, and the pipeline needs only :meth:`PredicateResult.first_fired`.
    """
    return PredicateResult(
        outcomes=tuple(
            evaluate(rule, records, source=body.source_for(rule)) for rule in predicate_rules(body)
        )
    )
