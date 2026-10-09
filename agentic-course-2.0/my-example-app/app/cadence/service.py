"""The cadence's business operations: enrol, sync, overdue. The service owns every commit.

**Nothing here sends, calls or knocks.** Each touch becomes a HubSpot task — a request to a human —
and the email touch is a task to draft and send *by hand* (Spike 4). Task creation is ungated by
the provenance write-gate on purpose: a task is not a claim about a prospect.

Sync is idempotent by construction rather than by bookkeeping. A task is created only when the
position advances, and a live row always points at exactly one open task, so a sync with nothing
new in HubSpot finds nothing done and creates nothing — however many times it runs in a day.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.exceptions import AlreadyEnrolledError
from app.cadence.machine import (
    CadencePosition,
    Touch,
    due_at_enrolment,
    local_date,
    task_for,
)
from app.cadence.models import CadenceState
from app.cadence.repository import CadenceRepository
from app.cadence.schemas import (
    CadenceStateResponse,
    OverdueTouch,
    SyncFailure,
    SyncReport,
    TaskSnapshot,
)
from app.cadence.sync import OutcomeReader, plan_advance
from app.core.logging import get_logger
from app.promotion.client import HubSpotClient, get_hubspot_client
from app.promotion.exceptions import HubSpotError

logger = get_logger(__name__)

type Clock = Callable[[], datetime]
type HubSpotFactory = Callable[[], HubSpotClient]


def utc_now() -> datetime:
    """The real clock. Injected so a test can walk three cycles without waiting twelve days."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _ProspectResult:
    """What syncing one prospect did, for the report."""

    touches_closed: int = 0
    task_created: bool = False
    parked: bool = False
    task_missing: bool = False
    superseded_task_id: str | None = None


class CadenceService:
    """Enrolment, the outcome sync, and the overdue view.

    The HubSpot client is reached through a factory, called only by operations that talk to the
    portal — so the overdue view, which reads only our own schedule, works with no token set.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        hubspot: HubSpotFactory = get_hubspot_client,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._repository = CadenceRepository(session)
        self._hubspot = hubspot
        self._clock = clock

    async def enrol(
        self,
        contact_id: str,
        *,
        company_id: str | None = None,
        owner_id: str | None = None,
        start: CadencePosition | None = None,
        anchor_at: datetime | None = None,
        due_at: datetime | None = None,
    ) -> CadenceStateResponse:
        """Put a contact into the cadence and create the task for its first touch.

        **The seam T9 and T13 call.** T9 hands a freshly promoted prospect over with the defaults:
        cycle one, the call, due today, evidence counted from now. T13 adopts a hand-worked prospect
        by passing the position it **reconstructed** from logged activity (``start``), the instant
        evidence should count from (``anchor_at``) and the due date history implies (``due_at``) —
        cycle position is never reset, so adoption must not fall back to the defaults.

        The task is created before the row is written: a row pointing at a task that was never
        made would be a prospect nobody is asked to touch.
        """
        if await self._repository.get_by_contact(contact_id) is not None:
            raise AlreadyEnrolledError(
                f"contact {contact_id} already has a cadence — cycle position is never reset",
                contact_id=contact_id,
            )

        now = self._clock()
        position = start or CadencePosition.first()
        due = due_at or due_at_enrolment(now)
        created = await self._hubspot().create_task(
            task_for(position, due, owner_id), contact_id=contact_id, company_id=company_id
        )
        state = await self._repository.create(
            contact_id=contact_id,
            company_id=company_id,
            owner_id=owner_id,
            position=position,
            task_id=created.id,
            due_at=due,
            anchor_at=anchor_at or now,
            enrolled_at=now,
        )
        await self._session.commit()
        logger.info(
            "cadence.service.prospect_enrolled",
            contact_id=contact_id,
            cycle=position.cycle,
            touch=position.touch.value,
            task_id=created.id,
        )
        return CadenceStateResponse.model_validate(state)

    async def sync(self) -> SyncReport:
        """Read outcomes for every live prospect and advance whatever is done.

        One prospect's HubSpot failure is recorded and the rest carry on; it is retried, unchanged,
        next time. Each prospect is committed on its own, and its plan is computed in full before
        the one remote write (the next task), so a failed create leaves its row exactly as it was.
        Failing to read the tasks at all fails the whole sync — there is nothing to decide without
        them.
        """
        now = self._clock()
        report = SyncReport(ran_at=now)
        states = list(await self._repository.list_live())
        logger.info("cadence.sync.run_started", live=len(states))

        if states:
            client = self._hubspot()
            reader = OutcomeReader(client)
            tasks = await reader.tasks(
                [state.hubspot_task_id for state in states if state.hubspot_task_id is not None]
            )
            for state in states:
                report.checked += 1
                contact_id = state.hubspot_contact_id
                try:
                    result = await self._sync_one(state, tasks, reader, client, now)
                except HubSpotError as exc:
                    logger.error(
                        "cadence.sync.prospect_failed",
                        contact_id=contact_id,
                        code=exc.code,
                        error=exc.message,
                    )
                    report.failures.append(
                        SyncFailure(hubspot_contact_id=contact_id, error=exc.message, code=exc.code)
                    )
                    continue
                report.touches_closed += result.touches_closed
                report.tasks_created += int(result.task_created)
                report.parked += int(result.parked)
                report.unchanged += int(result.touches_closed == 0)
                if result.task_missing:
                    report.missing_tasks.append(contact_id)
                if result.superseded_task_id is not None:
                    report.open_tasks_superseded.append(result.superseded_task_id)

        report.overdue = await self.overdue(now)
        for touch in report.overdue:
            logger.warning(
                "cadence.sync.touch_overdue",
                contact_id=touch.hubspot_contact_id,
                cycle=touch.cycle,
                touch=touch.touch.value,
                days_overdue=touch.days_overdue,
            )
        logger.info(
            "cadence.sync.run_completed",
            checked=report.checked,
            touches_closed=report.touches_closed,
            tasks_created=report.tasks_created,
            parked=report.parked,
            overdue=len(report.overdue),
            failures=len(report.failures),
        )
        return report

    async def _sync_one(
        self,
        state: CadenceState,
        tasks: Mapping[str, TaskSnapshot],
        reader: OutcomeReader,
        client: HubSpotClient,
        now: datetime,
    ) -> _ProspectResult:
        """Decide, then apply. Nothing is mutated until the plan and the next task both exist."""
        contact_id = state.hubspot_contact_id
        task = None if state.hubspot_task_id is None else tasks.get(state.hubspot_task_id)
        activities = await reader.activities(contact_id, since=state.anchor_at)
        position = CadencePosition(cycle=state.cycle, touch=Touch(state.touch))
        plan = plan_advance(position, state.anchor_at, state.anchor_ref, task, activities, now)

        if not plan.changed:
            if task is None:
                logger.warning(
                    "cadence.sync.task_missing",
                    contact_id=contact_id,
                    task_id=state.hubspot_task_id,
                )
            return _ProspectResult(task_missing=task is None)

        for step in plan.steps:
            logger.info(
                "cadence.sync.touch_done",
                contact_id=contact_id,
                cycle=step.position.cycle,
                touch=step.position.touch.value,
                source=step.signal.source.value,
            )
        superseded = state.hubspot_task_id if task is not None and not task.completed else None
        anchor_at = plan.anchor_at or state.anchor_at

        if plan.next_position is None or plan.next_due_at is None:
            await self._repository.park(
                state, anchor_at=anchor_at, anchor_ref=plan.anchor_ref, parked_at=now
            )
            await self._session.commit()
            logger.info("cadence.service.prospect_parked", contact_id=contact_id)
            return _ProspectResult(
                touches_closed=len(plan.steps), parked=True, superseded_task_id=superseded
            )

        created = await client.create_task(
            task_for(plan.next_position, plan.next_due_at, state.hubspot_owner_id),
            contact_id=contact_id,
            company_id=state.hubspot_company_id,
        )
        await self._repository.advance(
            state,
            position=plan.next_position,
            task_id=created.id,
            due_at=plan.next_due_at,
            anchor_at=anchor_at,
            anchor_ref=plan.anchor_ref,
        )
        await self._session.commit()
        return _ProspectResult(
            touches_closed=len(plan.steps), task_created=True, superseded_task_id=superseded
        )

    async def overdue(self, now: datetime | None = None) -> list[OverdueTouch]:
        """Live touches past their due date, most overdue first. Reads only our own schedule.

        Between syncs this can list a touch a person has since done; the next sync is what
        notices. That staleness is the price of not mirroring outcomes.
        """
        moment = now or self._clock()
        today = local_date(moment)
        return [
            OverdueTouch(
                hubspot_contact_id=state.hubspot_contact_id,
                hubspot_company_id=state.hubspot_company_id,
                hubspot_task_id=state.hubspot_task_id,
                cycle=state.cycle,
                touch=Touch(state.touch),
                due_at=state.due_at,
                days_overdue=(today - local_date(state.due_at)).days,
            )
            for state in await self._repository.list_overdue(moment)
            if state.due_at is not None
        ]
