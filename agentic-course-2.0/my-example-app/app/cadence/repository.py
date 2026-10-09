"""Data access for ``cadence_state``. Queries only — no business rules, no commits.

The house rule from T2: **the repository flushes; the service commits.** A flush makes defaults and
constraints fire here, at the line that caused them, while the transaction boundary stays with the
service — the only layer that knows a prospect's update is finished.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CadencePosition, CadenceStatus
from app.cadence.models import CadenceState
from app.core.logging import get_logger

logger = get_logger(__name__)


class CadenceRepository:
    """Reads and writes cadence rows through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_contact(self, contact_id: str) -> CadenceState | None:
        """The cadence for one HubSpot contact, live or parked."""
        result = await self._session.execute(
            select(CadenceState).where(CadenceState.hubspot_contact_id == contact_id)
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
    ) -> CadenceState:
        """Insert a live cadence whose first task already exists in HubSpot."""
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
            anchor_ref=None,
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

    async def advance(
        self,
        state: CadenceState,
        *,
        position: CadencePosition,
        task_id: str,
        due_at: datetime,
        anchor_at: datetime,
        anchor_ref: str | None,
    ) -> None:
        """Move a live cadence to its next touch, whose task now exists."""
        state.cycle = position.cycle
        state.touch = position.touch.value
        state.hubspot_task_id = task_id
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
        state.due_at = None
        state.anchor_at = anchor_at
        state.anchor_ref = anchor_ref
        state.parked_at = parked_at
        await self._session.flush()
        logger.info("cadence.repository.state_parked", contact_id=state.hubspot_contact_id)
