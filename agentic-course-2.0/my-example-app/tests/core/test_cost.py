"""Cost accounting. The cap is a circuit breaker, so where exactly it fires matters."""

from decimal import Decimal

import pytest

from app.core.cost import BillableKind, RunCost
from app.core.exceptions import CostLimitExceededError


class TestRecording:
    def test_calls_accumulate(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup)
        cost.record(BillableKind.places_lookup, count=4)

        assert cost.total_calls(BillableKind.places_lookup) == 5

    def test_kinds_are_counted_separately(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=3)
        cost.record(BillableKind.anthropic_tokens, count=2)

        assert cost.total_calls(BillableKind.places_lookup) == 3
        assert cost.total_calls(BillableKind.anthropic_tokens) == 2
        assert cost.total_calls() == 5

    def test_unrecorded_kind_is_zero(self) -> None:
        assert RunCost().total_calls(BillableKind.anthropic_tokens) == 0

    def test_negative_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            RunCost().record(BillableKind.places_lookup, count=-1)


class TestMoney:
    def test_spend_accumulates_as_decimal(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, usd=Decimal("0.017"))
        cost.record(BillableKind.places_lookup, usd=Decimal("0.017"))
        cost.record(BillableKind.anthropic_tokens, usd=Decimal("0.004"))

        assert cost.total_usd(BillableKind.places_lookup) == Decimal("0.034")
        assert cost.total_usd() == Decimal("0.038")

    def test_arithmetic_stays_exact(self) -> None:
        """Three cents recorded as a float would not equal three cents. Decimal does."""
        cost = RunCost()
        for _ in range(3):
            cost.record(BillableKind.places_lookup, usd=Decimal("0.01"))

        assert cost.total_usd() == Decimal("0.03")
        assert isinstance(cost.total_usd(), Decimal)

    def test_calls_without_a_price_are_counted_but_not_charged(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=2)

        assert cost.total_calls(BillableKind.places_lookup) == 2
        assert cost.total_usd() == Decimal("0")


class TestCap:
    def test_passes_below_the_cap(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=499)
        cost.check_cap(BillableKind.places_lookup, cap=500)

    def test_raises_exactly_at_the_cap_not_one_call_early_or_late(self) -> None:
        """With a cap of 500, the 500th call is allowed and the 501st is refused."""
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=499)
        cost.check_cap(BillableKind.places_lookup, cap=500)  # 500th call: allowed

        cost.record(BillableKind.places_lookup)
        with pytest.raises(CostLimitExceededError):  # 501st call: refused
            cost.check_cap(BillableKind.places_lookup, cap=500)

    def test_zero_cap_refuses_immediately(self) -> None:
        """Degenerate but well-defined — no division, no accidental pass."""
        with pytest.raises(CostLimitExceededError):
            RunCost().check_cap(BillableKind.places_lookup, cap=0)

    def test_cap_is_per_kind(self) -> None:
        cost = RunCost()
        cost.record(BillableKind.anthropic_tokens, count=1000)
        cost.check_cap(BillableKind.places_lookup, cap=500)

    def test_error_carries_what_the_caller_needs_to_degrade(self) -> None:
        """T6 catches this to mark a run degraded; it needs the numbers, not just a message."""
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=500)

        with pytest.raises(CostLimitExceededError) as exc_info:
            cost.check_cap(BillableKind.places_lookup, cap=500)

        error = exc_info.value
        assert error.kind == "places_lookup"
        assert error.cap == 500
        assert error.recorded == 500
        assert error.code == "cost_limit_exceeded"
