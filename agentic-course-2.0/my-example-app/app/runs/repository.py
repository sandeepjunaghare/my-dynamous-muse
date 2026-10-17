"""Data access for ``weekly_run``. Queries only — no business rules, no commits.

**The repository flushes; the service commits** (T2's house convention). Every timestamp is the
**database's** ``now()``, as on ``sourcing_run``, so M7 is never measured across two clocks.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.runs.models import WeeklyRun
from app.runs.schemas import WeeklyRunRequest, WeeklyRunStatus

logger = get_logger(__name__)


class WeeklyRunRepository:
    """Reads and writes weekly runs through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, request: WeeklyRunRequest) -> WeeklyRun:
        """Insert a ``running`` row and flush.

        A second running row violates ``uq_weekly_run_one_running`` here, at the flush; the service
        rolls back and turns that one constraint into a 409.
        """
        run = WeeklyRun(
            vertical=request.vertical,
            status=WeeklyRunStatus.running.value,
            dry_run=request.dry_run,
            batch_size=request.batch_size,
            allow_degraded=request.allow_degraded,
        )
        self._session.add(run)
        await self._session.flush()
        # `requested_at` and `promotion_counts` are server defaults; load them inside the async
        # context rather than lazily on first access (an error under asyncio).
        await self._session.refresh(run)
        return run

    async def get(self, run_id: UUID) -> WeeklyRun | None:
        """One weekly run by id, whatever its status."""
        result = await self._session.scalars(
            select(WeeklyRun).where(WeeklyRun.id == run_id),
            execution_options={"populate_existing": True},
        )
        return result.one_or_none()

    async def running(self) -> WeeklyRun | None:
        """The weekly run in progress, if any. The partial unique index allows at most one."""
        result = await self._session.scalars(
            select(WeeklyRun).where(WeeklyRun.status == WeeklyRunStatus.running.value),
            execution_options={"populate_existing": True},
        )
        return result.one_or_none()

    async def record_sourcing_run(self, run_id: UUID, sourcing_run_id: UUID) -> None:
        """Link the sourcing run the pipeline just opened."""
        await self._session.execute(
            update(WeeklyRun).where(WeeklyRun.id == run_id).values(sourcing_run_id=sourcing_run_id)
        )

    async def mark_sourced(self, run_id: UUID) -> None:
        """Sourcing finished: stamp ``sourced_at`` from the database clock."""
        await self._session.execute(
            update(WeeklyRun).where(WeeklyRun.id == run_id).values(sourced_at=func.now())
        )

    async def finish(
        self,
        run_id: UUID,
        status: WeeklyRunStatus,
        *,
        detail: str | None,
        promotion_counts: Mapping[str, int] | None = None,
        promotion_stopped: str | None = None,
        promoted: bool = False,
    ) -> WeeklyRun | None:
        """Close a running run in one conditional write; ``None`` when it was no longer running.

        A compare-and-set on ``status = 'running'``: a run the reaper already failed keeps that
        outcome rather than being overwritten by a late finish.
        """
        values: dict[str, object] = {
            "status": status.value,
            "status_detail": detail,
            "finished_at": func.now(),
            "promotion_stopped": promotion_stopped,
        }
        if promotion_counts is not None:
            values["promotion_counts"] = dict(promotion_counts)
        if promoted:
            values["promoted_at"] = func.now()
        result = await self._session.scalars(
            update(WeeklyRun)
            .where(WeeklyRun.id == run_id, WeeklyRun.status == WeeklyRunStatus.running.value)
            .values(values)
            .returning(WeeklyRun),
            execution_options={"populate_existing": True},
        )
        return result.one_or_none()

    async def reap_stale(self, older_than: timedelta) -> list[UUID]:
        """Fail weekly runs still ``running`` past ``older_than``; this lifts the guard they hold.

        A run killed outright (the Mac asleep, a ``kill -9``, a server restart mid-task) never
        reaches its own ``except``. Its sourcing run is reaped by sourcing, at its own next start.
        """
        hours = older_than.total_seconds() / 3600
        result = await self._session.scalars(
            update(WeeklyRun)
            .where(
                WeeklyRun.status == WeeklyRunStatus.running.value,
                WeeklyRun.requested_at < func.now() - older_than,
            )
            .values(
                status=WeeklyRunStatus.failed.value,
                status_detail=f"abandoned: still running after {hours:g}h",
                finished_at=func.now(),
            )
            .returning(WeeklyRun.id)
        )
        reaped = list(result.all())
        if reaped:
            logger.warning("runs.weekly.runs_reaped", run_ids=[str(run_id) for run_id in reaped])
        return reaped

    async def requested_between(self, start: datetime, end: datetime) -> Sequence[WeeklyRun]:
        """Weekly runs requested in ``[start, end)``, oldest first."""
        result = await self._session.scalars(
            select(WeeklyRun)
            .where(WeeklyRun.requested_at >= start, WeeklyRun.requested_at < end)
            .order_by(WeeklyRun.requested_at.asc()),
            execution_options={"populate_existing": True},
        )
        return result.all()
