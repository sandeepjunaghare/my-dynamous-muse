"""Per-run accounting for billable calls.

This lives in ``core/`` rather than in the enrichment ticket that first spends money, because D9
chose to *measure* cost rather than cap it — and measurement that arrives with the first spender
arrives too late to set a ceiling from. T6 (Google Places) and T12 (Anthropic) are both known
consumers.

Deliberately storage-agnostic: a :class:`RunCost` accumulates in memory for the length of one run,
and T4 persists the totals onto ``sourcing_run``.
"""

from decimal import Decimal
from enum import StrEnum

from app.core.exceptions import CostLimitExceededError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BillableKind(StrEnum):
    """A category of call that costs money."""

    places_lookup = "places_lookup"
    """A Google Places request — address/phone verification and the review-name signal (T6)."""

    anthropic_tokens = "anthropic_tokens"
    """An Agent SDK call at a judgment node or in the manifest-authoring agent (T6, T12)."""


class RunCost:
    """Accumulates the billable calls and spend of a single run.

    One instance per run. Not thread-safe and does not need to be: the weekly run is one
    sequential pipeline with one user behind it.
    """

    def __init__(self) -> None:
        self._calls: dict[BillableKind, int] = {}
        self._usd: dict[BillableKind, Decimal] = {}

    def record(
        self,
        kind: BillableKind,
        count: int = 1,
        usd: Decimal | None = None,
    ) -> None:
        """Record ``count`` calls of ``kind``, optionally with the dollars they cost.

        Money is ``Decimal`` throughout — never ``float``, whose rounding error accumulates over a
        run and makes the reported total quietly wrong.
        """
        if count < 0:
            raise ValueError("count must not be negative")

        self._calls[kind] = self._calls.get(kind, 0) + count
        if usd is not None:
            self._usd[kind] = self._usd.get(kind, Decimal("0")) + usd

        logger.info(
            "core.cost.call_recorded",
            kind=kind.value,
            count=count,
            usd=str(usd) if usd is not None else None,
            running_calls=self._calls[kind],
        )

    def total_calls(self, kind: BillableKind | None = None) -> int:
        """Calls recorded for ``kind``, or across every kind when ``kind`` is omitted."""
        if kind is None:
            return sum(self._calls.values())
        return self._calls.get(kind, 0)

    def total_usd(self, kind: BillableKind | None = None) -> Decimal:
        """Dollars recorded for ``kind``, or across every kind when ``kind`` is omitted."""
        if kind is None:
            return sum(self._usd.values(), Decimal("0"))
        return self._usd.get(kind, Decimal("0"))

    def check_cap(self, kind: BillableKind, cap: int) -> None:
        """Raise :class:`CostLimitExceededError` if another call of ``kind`` would exceed ``cap``.

        A **pre-flight** guard: call it immediately before spending, not after. With a cap of 500,
        the check passes while 499 calls are recorded (the 500th is allowed) and raises once 500
        are recorded (the 501st is refused). A cap of ``0`` therefore refuses every call, which is
        degenerate but well-defined.
        """
        recorded = self.total_calls(kind)
        if recorded >= cap:
            logger.warning(
                "core.cost.cap_exceeded",
                kind=kind.value,
                cap=cap,
                recorded=recorded,
            )
            raise CostLimitExceededError(
                f"per-run cap of {cap} {kind.value} calls reached",
                kind=kind.value,
                cap=cap,
                recorded=recorded,
            )
