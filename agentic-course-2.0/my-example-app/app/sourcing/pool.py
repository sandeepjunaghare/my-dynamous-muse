"""Data access for ``sourcing_pool`` — the backlog (D12). Queries only; flushes, never commits.

A separate module from ``repository.py`` on purpose: T7 (in parallel) may edit the candidate
repository for re-source suppression, and two branches editing one file is the merge conflict the
Wave 5 seam exists to avoid.
"""

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.sourcing.models import SourcingPool, SourcingRun
from app.sourcing.schemas import RunStatus
from app.sourcing.sources.base import PoolRecord

logger = get_logger(__name__)

_UPSERT_CHUNK = 500
"""Rows per bulk upsert: freight v3's ~4,450 is nine statements, not 4,450 round trips."""


class PoolRepository:
    """Reads and writes one vertical's backlog through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def refresh(self, vertical: str, run_id: UUID, records: Sequence[PoolRecord]) -> int:
        """Upsert what the registry returned this run; return how many entries are new.

        On conflict the registry's fields, ordering keys and ``last_seen_run_id`` are replaced —
        the pool is the registry's, overwritten whole. ``batched_run_id`` and ``first_seen_run_id``
        are never touched: a refresh must not forget what was already worked.
        """
        registry_ids = [record.registry_id for record in records]
        if len(set(registry_ids)) != len(registry_ids):
            # A multi-row ON CONFLICT naming one key twice raises "cannot affect row a second
            # time" — and takes the whole chunk with it. Discovery sources dedupe; this says so.
            raise ValueError("pool refresh records must be unique by registry_id")

        for start in range(0, len(records), _UPSERT_CHUNK):
            chunk = records[start : start + _UPSERT_CHUNK]
            inserting = insert(SourcingPool).values(
                [
                    {
                        "vertical": vertical,
                        "registry_id": record.registry_id,
                        "fields": record.fields.to_stored(),
                        "currency_date": record.currency_date,
                        "has_principal": record.has_principal,
                        "first_seen_run_id": run_id,
                        "last_seen_run_id": run_id,
                    }
                    for record in chunk
                ]
            )
            await self._session.execute(
                inserting.on_conflict_do_update(
                    constraint="uq_sourcing_pool_vertical_registry_id",
                    set_={
                        "fields": inserting.excluded.fields,
                        "currency_date": inserting.excluded.currency_date,
                        "has_principal": inserting.excluded.has_principal,
                        "last_seen_run_id": inserting.excluded.last_seen_run_id,
                        "updated_at": func.now(),
                    },
                )
            )
        await self._session.flush()

        new = await self._session.scalar(
            select(func.count())
            .select_from(SourcingPool)
            .where(SourcingPool.vertical == vertical, SourcingPool.first_seen_run_id == run_id)
        )
        logger.info(
            "sourcing.pool.refresh_completed",
            vertical=vertical,
            run_id=str(run_id),
            seen=len(records),
            new=new or 0,
        )
        return new or 0

    def _available(self, vertical: str, run_id: UUID) -> ColumnElement[bool]:
        """Seen by this run's refresh, and never batched — or batched by a run that failed."""
        failed_runs = select(SourcingRun.id).where(SourcingRun.status == RunStatus.failed.value)
        return and_(
            SourcingPool.vertical == vertical,
            SourcingPool.last_seen_run_id == run_id,
            SourcingPool.batched_run_id.is_(None) | SourcingPool.batched_run_id.in_(failed_runs),
        )

    async def count_available(self, vertical: str, run_id: UUID) -> int:
        """How many entries this run could take, before it takes any."""
        count = await self._session.scalar(
            select(func.count()).select_from(SourcingPool).where(self._available(vertical, run_id))
        )
        return count or 0

    async def select_batch(
        self, vertical: str, run_id: UUID, *, size: int, currency_cutoff: date
    ) -> list[SourcingPool]:
        """Take up to ``size`` available entries, best first, and mark them batched by ``run_id``.

        Best first (D12): a current record (refreshed on or after ``currency_cutoff``), then one
        naming a principal, then ``registry_id``. The last is lexical (``usdot:10`` before
        ``usdot:9``) — any total order would do; this one is stable, which is all D12 asks for.
        A missing date sorts with the stale ones: the expression is spelled as a ``CASE`` so no
        NULL ordering rule is involved.

        No row lock: one user, one run (CLAUDE.md — no run-lock until one is needed). Two
        concurrent runs of one vertical could take overlapping batches.
        """
        current = case((SourcingPool.currency_date >= currency_cutoff, 1), else_=0)
        principal = case((SourcingPool.has_principal, 1), else_=0)
        result = await self._session.scalars(
            select(SourcingPool)
            .where(self._available(vertical, run_id))
            .order_by(
                current.desc(),
                principal.desc(),
                SourcingPool.registry_id.asc(),
                SourcingPool.id.asc(),
            )
            .limit(size)
        )
        selected = list(result.all())
        if selected:
            await self._session.execute(
                update(SourcingPool)
                .where(SourcingPool.id.in_([entry.id for entry in selected]))
                .values(batched_run_id=run_id, updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await self._session.flush()
            for entry in selected:
                entry.batched_run_id = run_id

        logger.info(
            "sourcing.pool.batch_selected",
            vertical=vertical,
            run_id=str(run_id),
            requested=size,
            selected=len(selected),
            currency_cutoff=currency_cutoff.isoformat(),
        )
        return selected
