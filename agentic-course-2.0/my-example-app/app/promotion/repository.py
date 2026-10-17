"""Data access for the ``promotion`` ledger. Queries only — no business rules, no commits.

**The repository flushes; the service commits** — T2's house convention. The service commits after
every claim and after every HubSpot create, so the ledger is never behind HubSpot by more than the
one call in flight.

Every state change is a **compare-and-set** (``WHERE id = :id AND status = :from``): of two
invocations racing one row, the second changes nothing and gets ``None`` back. Theirs stands.
"""

from collections.abc import Collection, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.promotion.models import Promotion
from app.promotion.schemas import PromotionStatus
from app.sourcing.schemas import CandidateResponse

logger = get_logger(__name__)


class PromotionRepository:
    """Reads and writes the ledger through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        candidate: CandidateResponse,
        *,
        vertical: str,
        company_key: str,
        dedupe_phone: str | None,
        dedupe_domain: str | None,
        name_ambiguous: bool,
    ) -> Promotion | None:
        """Open a ``pending_create`` row for this business — the intent, recorded before the create.

        ``None`` means the business already has a row in this vertical, from any run and in any
        status: it has been promoted, or attempted, and must not be again. The run is the
        candidate's own (``fk_promotion_run_candidate``).
        """
        result = await self._session.scalars(
            insert(Promotion)
            .values(
                vertical=vertical,
                registry_id=candidate.registry_id,
                run_id=candidate.run_id,
                candidate_id=candidate.id,
                status=PromotionStatus.pending_create.value,
                company_key=company_key,
                dedupe_phone=dedupe_phone,
                dedupe_domain=dedupe_domain,
                name_ambiguous=name_ambiguous,
            )
            .on_conflict_do_nothing(constraint="uq_promotion_vertical_registry_id")
            .returning(Promotion),
            execution_options={"populate_existing": True},
        )
        row = result.one_or_none()
        if row is not None:
            logger.info(
                "promotion.ledger.row_claimed",
                promotion_id=str(row.id),
                vertical=vertical,
                registry_id=candidate.registry_id,
                run_id=str(candidate.run_id),
            )
        return row

    async def find_by_dedupe_key(
        self,
        vertical: str,
        *,
        phone: str | None,
        domain: str | None,
        excluding_id: UUID,
    ) -> Promotion | None:
        """Another row of this vertical sharing the normalized phone or domain — the same business.

        This is the dedupe HubSpot's lagging search cannot do: two candidates of one batch that
        resolve to one company. A ``duplicate`` row is never the match, because it created nothing.
        No key matches nothing: two rows are not the same business by both lacking a phone.
        """
        keys: list[ColumnElement[bool]] = []
        if phone is not None:
            keys.append(Promotion.dedupe_phone == phone)
        if domain is not None:
            keys.append(Promotion.dedupe_domain == domain)
        if not keys:
            return None
        result = await self._session.scalars(
            select(Promotion)
            .where(
                Promotion.vertical == vertical,
                Promotion.id != excluding_id,
                Promotion.status != PromotionStatus.duplicate.value,
                or_(*keys),
            )
            .order_by(Promotion.created_at.asc(), Promotion.registry_id.asc())
            .limit(1)
        )
        return result.one_or_none()

    async def get_by_registry_id(self, vertical: str, registry_id: str) -> Promotion | None:
        """This business's row in this vertical, if it was ever promoted or attempted."""
        result = await self._session.scalars(
            select(Promotion).where(
                Promotion.vertical == vertical, Promotion.registry_id == registry_id
            ),
            execution_options={"populate_existing": True},
        )
        return result.one_or_none()

    async def list_by_status(
        self, statuses: Collection[PromotionStatus], *, vertical: str | None = None
    ) -> Sequence[Promotion]:
        """Rows in any of ``statuses``, oldest first — the order a resume or a sync works them."""
        query = select(Promotion).where(Promotion.status.in_([s.value for s in statuses]))
        if vertical is not None:
            query = query.where(Promotion.vertical == vertical)
        result = await self._session.scalars(
            query.order_by(Promotion.created_at.asc(), Promotion.registry_id.asc()),
            execution_options={"populate_existing": True},
        )
        return result.all()

    async def list_for_run(self, run_id: UUID) -> Sequence[Promotion]:
        """Every row a run's candidates claimed, in registry-id order."""
        result = await self._session.scalars(
            select(Promotion)
            .where(Promotion.run_id == run_id)
            .order_by(Promotion.registry_id.asc()),
            execution_options={"populate_existing": True},
        )
        return result.all()

    async def status_counts_by_run(self, run_ids: Collection[UUID]) -> dict[UUID, dict[str, int]]:
        """``{run_id: {status: rows}}`` for the runs given. A run with no rows is absent."""
        if not run_ids:
            return {}
        result = await self._session.execute(
            select(Promotion.run_id, Promotion.status, func.count())
            .where(Promotion.run_id.in_(list(run_ids)))
            .group_by(Promotion.run_id, Promotion.status)
        )
        counts: dict[UUID, dict[str, int]] = {}
        for run_id, status, rows in result.tuples():
            counts.setdefault(run_id, {})[status] = rows
        return counts

    async def transition(
        self,
        row_id: UUID,
        *,
        from_status: PromotionStatus,
        to_status: PromotionStatus,
        hubspot_company_id: str | None = None,
        hubspot_contact_id: str | None = None,
        matched_company_id: str | None = None,
        duplicate_of_id: UUID | None = None,
        decided_at: datetime | None = None,
        enrolled_at: datetime | None = None,
        touched_at: datetime | None = None,
    ) -> Promotion | None:
        """Move a row from ``from_status`` to ``to_status``; ``None`` when it was not in ``from``.

        A compare-and-set, so a lost race changes nothing. Only the values given are set — no
        transition ever clears a column. ``from == to`` is allowed: recording the company id the
        moment its create returns, still ``pending_create``, or stamping a create attempt.
        ``touched_at`` is the ``updated_at`` to record, from the caller's clock; the database's
        ``now()`` when omitted.
        """
        given: dict[str, str | UUID | datetime | None] = {
            "hubspot_company_id": hubspot_company_id,
            "hubspot_contact_id": hubspot_contact_id,
            "matched_company_id": matched_company_id,
            "duplicate_of_id": duplicate_of_id,
            "decided_at": decided_at,
            "enrolled_at": enrolled_at,
        }
        values = {name: value for name, value in given.items() if value is not None}
        result = await self._session.scalars(
            update(Promotion)
            .where(Promotion.id == row_id, Promotion.status == from_status.value)
            .values(
                status=to_status.value,
                updated_at=func.now() if touched_at is None else touched_at,
                **values,
            )
            .returning(Promotion),
            execution_options={"populate_existing": True},
        )
        row = result.one_or_none()
        if row is None:
            logger.info(
                "promotion.ledger.transition_lost",
                promotion_id=str(row_id),
                from_status=from_status.value,
                to_status=to_status.value,
            )
            return None
        logger.info(
            "promotion.ledger.status_changed",
            promotion_id=str(row_id),
            from_status=from_status.value,
            to_status=to_status.value,
            fields=sorted(values),
        )
        return row
