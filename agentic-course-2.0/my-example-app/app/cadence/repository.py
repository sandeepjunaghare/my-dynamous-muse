"""Data access for ``cadence_state``. Queries only — no business rules, no commits.

The house rule from T2: **the repository flushes; the service commits.** A flush makes defaults and
constraints fire here, at the line that caused them, while the transaction boundary stays with the
service — the only layer that knows a prospect's update is finished.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.cadence.machine import CadencePosition, CadenceStatus
from app.cadence.models import CadenceState
from app.core.logging import get_logger

logger = get_logger(__name__)

CADENCE_SYNC_LOCK_ID = 7_340_011_101
"""The Postgres advisory-lock key a sync holds while it runs. Any constant; this one is ours."""


def _engine_of(session: AsyncSession) -> AsyncEngine:
    bind = session.bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    if isinstance(bind, AsyncEngine):
        return bind
    raise RuntimeError("the cadence session is not bound to an engine or a connection")


class CadenceRepository:
    """Reads and writes cadence rows through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, state_id: UUID) -> CadenceState | None:
        """One cadence by id, **re-read from the database** rather than the identity map.

        A sync rolls back a prospect that failed, and a rollback expires every loaded row; a
        fresh read is what lets it carry on with the next one.
        """
        result = await self._session.execute(
            select(CadenceState)
            .where(CadenceState.id == state_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    @asynccontextmanager
    async def sync_lock(self) -> AsyncGenerator[bool]:
        """Hold the cadence-sync advisory lock for the block. Yields whether it was acquired.

        **On a connection of its own**, session-scoped. A sync commits once per prospect, and the
        session hands its connection back to the pool at each commit — a transaction-scoped lock
        would be released after the first prospect, and a session-scoped one taken on the
        session's connection could be unlocked from a different one and leak. A dedicated
        connection holds it for exactly the block; if the process dies, Postgres drops it with the
        connection. Works on Supabase's session-mode pooler (5432), not its transaction mode.
        """
        async with _engine_of(self._session).connect() as connection:
            result = await connection.execute(
                text("select pg_try_advisory_lock(:key)"), {"key": CADENCE_SYNC_LOCK_ID}
            )
            acquired = bool(result.scalar_one())
            try:
                yield acquired
            finally:
                if acquired:
                    await connection.execute(
                        text("select pg_advisory_unlock(:key)"), {"key": CADENCE_SYNC_LOCK_ID}
                    )
                    await connection.commit()

    async def get_by_contact(self, contact_id: str) -> CadenceState | None:
        """The cadence for one HubSpot contact, live or parked, **as the database has it now**.

        Re-read rather than taken from the identity map: `park` checks the status under the sync
        lock, and a copy this session loaded earlier could still say ``live``.
        """
        result = await self._session.execute(
            select(CadenceState)
            .where(CadenceState.hubspot_contact_id == contact_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_live(self) -> Sequence[CadenceState]:
        """Every live cadence, soonest-due first — the order a sync works and a report reads."""
        result = await self._session.execute(
            select(CadenceState)
            .where(CadenceState.status == CadenceStatus.live.value)
            .order_by(CadenceState.due_at.asc(), CadenceState.hubspot_contact_id.asc())
        )
        return result.scalars().all()

    async def list_overdue(self, now: datetime) -> Sequence[CadenceState]:
        """Live cadences whose current touch was due before ``now``, most overdue first."""
        result = await self._session.execute(
            select(CadenceState)
            .where(
                CadenceState.status == CadenceStatus.live.value,
                CadenceState.due_at < now,
            )
            .order_by(CadenceState.due_at.asc(), CadenceState.hubspot_contact_id.asc())
        )
        return result.scalars().all()

    async def create(
        self,
        *,
        contact_id: str,
        company_id: str | None,
        owner_id: str | None,
        position: CadencePosition,
        task_id: str,
        due_at: datetime,
        anchor_at: datetime,
        enrolled_at: datetime,
        anchor_ref: str | None = None,
    ) -> CadenceState:
        """Insert a live cadence whose first task already exists in HubSpot.

        ``anchor_ref`` must come with ``anchor_at`` whenever the anchor is an activity — an
        adoption that ended on a logged note — or the next sync re-credits that activity.
        """
        state = CadenceState(
            hubspot_contact_id=contact_id,
            hubspot_company_id=company_id,
            hubspot_owner_id=owner_id,
            status=CadenceStatus.live.value,
            cycle=position.cycle,
            touch=position.touch.value,
            hubspot_task_id=task_id,
            due_at=due_at,
            anchor_at=anchor_at,
            anchor_ref=anchor_ref,
            enrolled_at=enrolled_at,
        )
        self._session.add(state)
        await self._session.flush()
        logger.info(
            "cadence.repository.state_created",
            contact_id=contact_id,
            cycle=position.cycle,
            touch=position.touch.value,
        )
        return state

    async def create_parked(
        self,
        *,
        contact_id: str,
        company_id: str | None,
        owner_id: str | None,
        position: CadencePosition,
        anchor_at: datetime,
        anchor_ref: str | None,
        enrolled_at: datetime,
        parked_at: datetime,
    ) -> CadenceState:
        """Insert a cadence that is finished from the start — an adopted refusal.

        No task, no due date and no pending key: the row exists only so that nothing can ever
        enrol the contact again (``uq_cadence_state_contact``).
        """
        state = CadenceState(
            hubspot_contact_id=contact_id,
            hubspot_company_id=company_id,
            hubspot_owner_id=owner_id,
            status=CadenceStatus.parked.value,
            cycle=position.cycle,
            touch=position.touch.value,
            anchor_at=anchor_at,
            anchor_ref=anchor_ref,
            enrolled_at=enrolled_at,
            parked_at=parked_at,
        )
        self._session.add(state)
        await self._session.flush()
        logger.info(
            "cadence.repository.state_created",
            contact_id=contact_id,
            status=CadenceStatus.parked.value,
            cycle=position.cycle,
            touch=position.touch.value,
        )
        return state

    async def advance(
        self,
        state: CadenceState,
        *,
        position: CadencePosition,
        pending_task_key: str,
        due_at: datetime,
        anchor_at: datetime,
        anchor_ref: str | None,
    ) -> None:
        """Move a live cadence to its next touch, whose task is **about to be** created.

        The row records the intent (``pending_task_key``) and no task id. The service commits this
        before it calls HubSpot, so an interrupted create leaves a row that says what to look for.
        """
        state.cycle = position.cycle
        state.touch = position.touch.value
        state.hubspot_task_id = None
        state.pending_task_key = pending_task_key
        state.due_at = due_at
        state.anchor_at = anchor_at
        state.anchor_ref = anchor_ref
        await self._session.flush()
        logger.info(
            "cadence.repository.state_advanced",
            contact_id=state.hubspot_contact_id,
            cycle=position.cycle,
            touch=position.touch.value,
        )

    async def attach_task(self, state: CadenceState, task_id: str) -> None:
        """Record the current touch's task — created or found — and clear the pending intent."""
        state.hubspot_task_id = task_id
        state.pending_task_key = None
        await self._session.flush()
        logger.info(
            "cadence.repository.task_attached",
            contact_id=state.hubspot_contact_id,
            task_id=task_id,
        )

    async def park(
        self,
        state: CadenceState,
        *,
        anchor_at: datetime,
        anchor_ref: str | None,
        parked_at: datetime,
    ) -> None:
        """Finish a cadence. Position stays at the last touch; there is no task and no due date."""
        state.status = CadenceStatus.parked.value
        state.hubspot_task_id = None
        state.pending_task_key = None
        state.due_at = None
        state.anchor_at = anchor_at
        state.anchor_ref = anchor_ref
        state.parked_at = parked_at
        await self._session.flush()
        logger.info("cadence.repository.state_parked", contact_id=state.hubspot_contact_id)
