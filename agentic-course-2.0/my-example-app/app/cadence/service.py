"""The cadence's business operations: enrol, sync, overdue. The service owns every commit.

**Nothing here sends, calls or knocks.** Each touch becomes a HubSpot task — a request to a human —
and the email touch is a task to draft and send *by hand* (Spike 4). Task creation is ungated by
the provenance write-gate on purpose: a task is not a claim about a prospect.

How a sync avoids creating a task twice
---------------------------------------
* **Sequentially**, a task is created only when the position advances, so a sync with nothing new
  in HubSpot finds nothing done and creates nothing, however many times it runs in a day.
* **After an interrupted create.** A create HubSpot executed but answered with a 5xx or a timeout,
  or one whose commit never happened, is ambiguous, and the gateway never retries it. So the
  advance is committed *first*, as a pending intent carrying a deterministic key
  (:func:`~app.cadence.machine.task_key`) that also goes into the task body. A row still pending at
  the next sync is resolved by looking for a task carrying that key among the contact's
  associated tasks, and adopting it. Only if none exists is the task created.
* **Concurrently**, a Postgres advisory lock lets one sync run at a time. A second one skips and
  says so (:attr:`~app.cadence.schemas.SyncReport.skipped`).

Each prospect is **decided** first (HubSpot reads, then the pure :func:`plan_advance`) and only then
**applied**. A dry run stops after the decision, so it predicts exactly what the real sync does.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.exceptions import (
    AlreadyEnrolledError,
    AlreadyParkedError,
    NotEnrolledError,
    SyncRunningError,
)
from app.cadence.machine import (
    CadencePosition,
    CadenceStatus,
    Touch,
    due_at_enrolment,
    local_date,
    task_for,
    task_key,
)
from app.cadence.models import CadenceState
from app.cadence.repository import CadenceRepository
from app.cadence.schemas import (
    AdvancePlan,
    CadenceStateResponse,
    OverdueTouch,
    ParkResult,
    PendingTask,
    ProspectPlan,
    SyncFailure,
    SyncReport,
    TaskSnapshot,
)
from app.cadence.sync import OutcomeReader, plan_advance
from app.core.config import get_settings
from app.core.logging import get_logger
from app.promotion.client import HubSpotClient, get_hubspot_client
from app.promotion.exceptions import HubSpotError

logger = get_logger(__name__)

type Clock = Callable[[], datetime]
type HubSpotFactory = Callable[[], HubSpotClient]

UNIQUE_CONTACT = "uq_cadence_state_contact"
"""The one-cadence-per-contact constraint. Only a violation of this one means "already
enrolled"; any other integrity error is a bug and is raised as it is."""

OWNER_UNSET_WARNING = (
    "HUBSPOT_DEFAULT_OWNER_ID is not set: cadence tasks without a named owner are created "
    "unassigned, and appear in nobody's 'My tasks'"
)


def utc_now() -> datetime:
    """The real clock. Injected so a test can walk three cycles without waiting twelve days."""
    return datetime.now(UTC)


def default_owner_id() -> str | None:
    """``HUBSPOT_DEFAULT_OWNER_ID``, or ``None`` when it is unset or blank.

    Read on each call rather than captured at construction, so a changed setting takes effect
    without rebuilding the service.
    """
    owner = get_settings().hubspot_default_owner_id
    if owner is None or not owner.strip():
        return None
    return owner.strip()


@dataclass(frozen=True)
class _RowView:
    """The fields a decision reads from a cadence row, captured as plain values at one moment.

    A dry run plans every prospect from the rows as ``list_live`` read them, the same moment as the
    task batch. Re-reading a row that a concurrent sync has since advanced would pair it with a
    batch that never saw its new task, and report a task deleted that was not. Plain values also
    survive the rollback a failed prospect triggers, which expires every loaded row.
    """

    state_id: UUID
    contact_id: str
    cycle: int
    touch: Touch
    task_id: str | None
    pending_key: str | None
    anchor_at: datetime
    anchor_ref: str | None

    @classmethod
    def of(cls, state: CadenceState) -> "_RowView":
        return cls(
            state_id=state.id,
            contact_id=state.hubspot_contact_id,
            cycle=state.cycle,
            touch=Touch(state.touch),
            task_id=state.hubspot_task_id,
            pending_key=state.pending_task_key,
            anchor_at=state.anchor_at,
            anchor_ref=state.anchor_ref,
        )


@dataclass(frozen=True)
class _Decision:
    """What a sync concluded for one prospect from HubSpot reads alone. Nothing is applied yet."""

    task: TaskSnapshot | None
    """The current touch's task: from the batch read, or — for a pending create — the task found
    carrying its key, ``None`` when there is none."""
    pending_key: str | None
    plan: AdvancePlan


@dataclass
class _ProspectResult:
    """What syncing one prospect did, for the report."""

    touches_closed: int = 0
    tasks_created: int = 0
    parked: bool = False
    task_missing: bool = False
    superseded_task_ids: list[str] = field(default_factory=list[str])
    adopted_task_id: str | None = None


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
        anchor_ref: str | None = None,
        due_at: datetime | None = None,
    ) -> CadenceStateResponse:
        """Put a contact into the cadence and create the task for its first touch.

        **The seam T9 and T13 call.** T9 hands a freshly promoted prospect over with the defaults:
        cycle one, the call, due today, evidence counted from now. T13 adopts a hand-worked prospect
        by passing the position it **reconstructed** from logged activity (``start``), the anchor
        evidence should count from (``anchor_at``, ``anchor_ref``) and the due date history implies
        (``due_at``) — cycle position is never reset, so adoption must not fall back to the
        defaults. **When ``anchor_at`` came from an activity, its ref must come too**: an anchor
        ``(t, None)`` sorts before the activity at ``t``, and the next sync would credit it again.

        With no ``owner_id``, the task goes to ``HUBSPOT_DEFAULT_OWNER_ID``; with neither, it is
        unassigned and a warning is logged.

        The task is created before the row is written: a row pointing at a task that was never
        made would be a prospect nobody is asked to touch. **Safe to retry:** a task already
        carrying this touch's key — HubSpot made it and the answer was lost, or the commit never
        happened — is found and reused rather than made twice. Two enrolments racing past the
        check below meet the unique constraint, and the loser raises :class:`AlreadyEnrolledError`.
        """
        if await self._repository.get_by_contact(contact_id) is not None:
            raise AlreadyEnrolledError(
                f"contact {contact_id} already has a cadence — cycle position is never reset",
                contact_id=contact_id,
            )

        now = self._clock()
        position = start or CadencePosition.first()
        due = due_at or due_at_enrolment(now)
        owner = owner_id or default_owner_id()
        if owner is None:
            logger.warning("cadence.service.owner_unset", contact_id=contact_id)
        client = self._hubspot()
        key = task_key(contact_id, position)
        existing = await OutcomeReader(client).find_task_by_key(contact_id, key)
        created_here = existing is None
        if existing is not None:
            task_id = existing.task_id
            logger.warning("cadence.service.task_reused", contact_id=contact_id, task_id=task_id)
        else:
            created = await client.create_task(
                task_for(position, due, owner, key=key),
                contact_id=contact_id,
                company_id=company_id,
            )
            task_id = created.id
        try:
            state = await self._repository.create(
                contact_id=contact_id,
                company_id=company_id,
                owner_id=owner,
                position=position,
                task_id=task_id,
                due_at=due,
                anchor_at=anchor_at or now,
                anchor_ref=anchor_ref,
                enrolled_at=now,
            )
        except IntegrityError as exc:
            await self._session.rollback()
            if UNIQUE_CONTACT not in str(exc.orig):
                raise
            orphan = ""
            if created_here:
                logger.warning(
                    "cadence.service.task_orphaned", contact_id=contact_id, task_id=task_id
                )
                orphan = f"; task {task_id} this run created is left open — close it by hand"
            raise AlreadyEnrolledError(
                f"contact {contact_id} was enrolled by another run while this one was working — "
                f"cycle position is never reset{orphan}",
                contact_id=contact_id,
            ) from exc
        await self._session.commit()
        logger.info(
            "cadence.service.prospect_enrolled",
            contact_id=contact_id,
            cycle=position.cycle,
            touch=position.touch.value,
            task_id=task_id,
        )
        return CadenceStateResponse.model_validate(state)

    async def adopt_parked(
        self,
        contact_id: str,
        *,
        company_id: str | None,
        position: CadencePosition,
        anchor_at: datetime,
        anchor_ref: str | None,
    ) -> CadenceStateResponse:
        """Record a hand-worked prospect as already finished — a refusal, or all nine touches done.

        T13's other seam. The parked row is what keeps the contact out for good: nothing can enrol
        it afterwards. Never reaches HubSpot, and takes no lock of its own — its caller
        (:meth:`~app.cadence.adoption.Adopter.apply`) already holds the sync lock.
        """
        if await self._repository.get_by_contact(contact_id) is not None:
            raise AlreadyEnrolledError(
                f"contact {contact_id} already has a cadence — cycle position is never reset",
                contact_id=contact_id,
            )
        now = self._clock()
        try:
            state = await self._repository.create_parked(
                contact_id=contact_id,
                company_id=company_id,
                owner_id=None,
                position=position,
                anchor_at=anchor_at,
                anchor_ref=anchor_ref,
                enrolled_at=now,
                parked_at=now,
            )
        except IntegrityError as exc:
            await self._session.rollback()
            if UNIQUE_CONTACT not in str(exc.orig):
                raise
            raise AlreadyEnrolledError(
                f"contact {contact_id} was enrolled by another run while this one was working",
                contact_id=contact_id,
            ) from exc
        await self._session.commit()
        logger.info(
            "cadence.service.prospect_parked",
            contact_id=contact_id,
            by="adoption",
            cycle=position.cycle,
            touch=position.touch.value,
        )
        return CadenceStateResponse.model_validate(state)

    async def sync(self, *, dry_run: bool = False) -> SyncReport:
        """Read outcomes for every live prospect and advance whatever is done.

        One sync at a time: if another holds the run lock, this one returns at once with
        ``skipped`` set and touches nothing.

        ``dry_run`` makes the same reads and the same decisions and applies none of them: no task
        is created, no row written, and no ``touch_done`` is logged, so metric M5 never counts a
        rehearsal. ``plans`` says what would have happened. It takes no lock, since it writes
        nothing, so it can run alongside a real sync.

        Each prospect is its own unit of work. A HubSpot, validation or database failure on one
        rolls back whatever that prospect had not committed, is reported under ``failures``, and
        the sync carries on with the next. Failing to read the tasks at all fails the whole sync —
        there is nothing to decide without them.
        """
        now = self._clock()
        if dry_run:
            return await self._run(now, dry_run=True)
        async with self._repository.sync_lock() as acquired:
            if not acquired:
                logger.warning("cadence.sync.run_skipped", reason="another sync holds the lock")
                return SyncReport(ran_at=now, skipped=True)
            return await self._run(now, dry_run=False)

    async def _run(self, now: datetime, *, dry_run: bool) -> SyncReport:
        report = SyncReport(ran_at=now, dry_run=dry_run)
        default_owner = default_owner_id()
        if default_owner is None:
            logger.warning("cadence.sync.owner_unset")
            report.warnings.append(OWNER_UNSET_WARNING)

        # Plain values, captured now, in one read with the task batch below. See `_RowView`.
        work = [_RowView.of(state) for state in await self._repository.list_live()]
        task_ids = [row.task_id for row in work if row.task_id is not None]
        logger.info("cadence.sync.run_started", live=len(work), dry_run=dry_run)

        if work:
            client = self._hubspot()
            reader = OutcomeReader(client)
            tasks = await reader.tasks(task_ids)
            for row in work:
                contact_id = row.contact_id
                try:
                    if dry_run:
                        report.checked += 1
                        report.plans.append(await self._plan_one(row, tasks, reader, now))
                        continue
                    # A real sync writes, so it works from the row as it is now. Under the run lock
                    # only `park` could have changed it, and `park` takes the same lock.
                    state = await self._repository.get(row.state_id)
                    if state is None or state.status != CadenceStatus.live.value:
                        continue
                    report.checked += 1
                    result = await self._sync_one(state, tasks, reader, client, now, default_owner)
                except HubSpotError as exc:
                    await self._record_failure(report, contact_id, exc.code, exc.message)
                    continue
                except ValidationError as exc:
                    await self._record_failure(
                        report,
                        contact_id,
                        "invalid_hubspot_response",
                        f"HubSpot answered a body that failed validation ({exc.title}, "
                        f"{exc.error_count()} error(s))",
                    )
                    continue
                except SQLAlchemyError as exc:
                    await self._record_failure(
                        report, contact_id, "database_error", str(exc).splitlines()[0]
                    )
                    continue
                report.touches_closed += result.touches_closed
                report.tasks_created += result.tasks_created
                report.parked += int(result.parked)
                report.unchanged += int(result.touches_closed == 0)
                if result.task_missing:
                    report.missing_tasks.append(contact_id)
                report.open_tasks_superseded.extend(result.superseded_task_ids)
                if result.adopted_task_id is not None:
                    report.pending_tasks_adopted.append(result.adopted_task_id)

        report.overdue = await self.overdue(now)
        # A rehearsal reports overdue touches but does not log them: whatever alerts on the event
        # should see each real sync once, not every dry run as well.
        for touch in [] if dry_run else report.overdue:
            logger.warning(
                "cadence.sync.touch_overdue",
                contact_id=touch.hubspot_contact_id,
                cycle=touch.cycle,
                touch=touch.touch.value,
                days_overdue=touch.days_overdue,
            )
        logger.info(
            "cadence.sync.run_completed",
            dry_run=dry_run,
            checked=report.checked,
            touches_closed=report.touches_closed,
            tasks_created=report.tasks_created,
            parked=report.parked,
            adopted=len(report.pending_tasks_adopted),
            overdue=len(report.overdue),
            failures=len(report.failures),
        )
        return report

    async def _record_failure(
        self, report: SyncReport, contact_id: str, code: str, message: str
    ) -> None:
        """Roll back what this prospect had not committed, and report it. The sync carries on."""
        await self._session.rollback()
        logger.error(
            "cadence.sync.prospect_failed", contact_id=contact_id, code=code, error=message
        )
        report.failures.append(SyncFailure(hubspot_contact_id=contact_id, error=message, code=code))

    async def _sync_one(
        self,
        state: CadenceState,
        tasks: Mapping[str, TaskSnapshot],
        reader: OutcomeReader,
        client: HubSpotClient,
        now: datetime,
        default_owner: str | None,
    ) -> _ProspectResult:
        """Decide, resolve any interrupted create, then apply."""
        contact_id = state.hubspot_contact_id
        owner = state.hubspot_owner_id or default_owner
        result = _ProspectResult()
        decision = await self._decide(_RowView.of(state), tasks, reader, now)
        task = decision.task
        if decision.pending_key is not None:
            task = await self._resolve_pending(
                state, decision.pending_key, decision.task, client, owner, now, result
            )
        plan = decision.plan

        if not plan.changed:
            if task is None:
                logger.warning(
                    "cadence.sync.task_missing",
                    contact_id=contact_id,
                    task_id=state.hubspot_task_id,
                )
                result.task_missing = True
            return result

        for step in plan.steps:
            logger.info(
                "cadence.sync.touch_done",
                contact_id=contact_id,
                cycle=step.position.cycle,
                touch=step.position.touch.value,
                source=step.signal.source.value,
            )
        result.touches_closed = len(plan.steps)
        if task is not None and not task.completed:
            result.superseded_task_ids.append(task.task_id)
        anchor_at = plan.anchor_at or state.anchor_at

        if plan.next_position is None or plan.next_due_at is None:
            await self._repository.park(
                state, anchor_at=anchor_at, anchor_ref=plan.anchor_ref, parked_at=now
            )
            await self._session.commit()
            logger.info("cadence.service.prospect_parked", contact_id=contact_id)
            result.parked = True
            return result

        key = task_key(contact_id, plan.next_position)
        await self._repository.advance(
            state,
            position=plan.next_position,
            pending_task_key=key,
            due_at=plan.next_due_at,
            anchor_at=anchor_at,
            anchor_ref=plan.anchor_ref,
        )
        # Durable before HubSpot is asked: from here, an interrupted create is found, not repeated.
        await self._session.commit()
        created = await client.create_task(
            task_for(plan.next_position, plan.next_due_at, owner, key=key),
            contact_id=contact_id,
            company_id=state.hubspot_company_id,
        )
        await self._repository.attach_task(state, created.id)
        await self._session.commit()
        result.tasks_created += 1
        return result

    async def _decide(
        self,
        row: _RowView,
        tasks: Mapping[str, TaskSnapshot],
        reader: OutcomeReader,
        now: datetime,
    ) -> _Decision:
        """Read what HubSpot says about one prospect and plan its advance. Reads only.

        A row with an interrupted create is looked up by its key among the contact's tasks (the
        associations read, not search, which lags). Planning on "no task" when none is found is
        the same plan the fresh, open task the real sync then creates would give: an open task
        closes nothing.
        """
        key = row.pending_key
        if key is not None:
            task = await reader.find_task_by_key(row.contact_id, key)
        else:
            task = None if row.task_id is None else tasks.get(row.task_id)
        activities = await reader.activities(row.contact_id, since=row.anchor_at)
        position = CadencePosition(cycle=row.cycle, touch=row.touch)
        plan = plan_advance(position, row.anchor_at, row.anchor_ref, task, activities, now)
        return _Decision(task=task, pending_key=key, plan=plan)

    async def _plan_one(
        self,
        row: _RowView,
        tasks: Mapping[str, TaskSnapshot],
        reader: OutcomeReader,
        now: datetime,
    ) -> ProspectPlan:
        """A dry run's view of one prospect: the decision, rendered, and nothing applied."""
        decision = await self._decide(row, tasks, reader, now)
        plan, task = decision.plan, decision.task
        pending: PendingTask | None = None
        pending_task_id: str | None = None
        if decision.pending_key is not None:
            pending = PendingTask.would_create if task is None else PendingTask.found
            pending_task_id = None if task is None else task.task_id
        return ProspectPlan(
            hubspot_contact_id=row.contact_id,
            steps=list(plan.steps),
            next_position=plan.next_position,
            next_due_at=plan.next_due_at,
            parks=plan.parks,
            pending_task=pending,
            pending_task_id=pending_task_id,
            pending_task_superseded=pending is PendingTask.would_create and plan.changed,
            superseded_task_id=(
                task.task_id if plan.changed and task is not None and not task.completed else None
            ),
            task_missing=not plan.changed and task is None and decision.pending_key is None,
        )

    async def _resolve_pending(
        self,
        state: CadenceState,
        key: str,
        found: TaskSnapshot | None,
        client: HubSpotClient,
        owner: str | None,
        now: datetime,
        result: _ProspectResult,
    ) -> TaskSnapshot:
        """Settle a create an earlier sync started but never confirmed.

        ``found`` is the task :meth:`_decide` found carrying ``key``: adopt it if HubSpot made it.
        Only when none exists is the task created, once. The snapshot returned is the current
        touch's task, so a task the founder already ticked closes its touch in this same sync.
        """
        contact_id = state.hubspot_contact_id
        if found is not None:
            await self._repository.attach_task(state, found.task_id)
            await self._session.commit()
            logger.warning(
                "cadence.sync.task_adopted", contact_id=contact_id, task_id=found.task_id
            )
            result.adopted_task_id = found.task_id
            return found

        position = CadencePosition(cycle=state.cycle, touch=Touch(state.touch))
        due = state.due_at or due_at_enrolment(now)
        created = await client.create_task(
            task_for(position, due, owner, key=key),
            contact_id=contact_id,
            company_id=state.hubspot_company_id,
        )
        await self._repository.attach_task(state, created.id)
        await self._session.commit()
        logger.info("cadence.sync.task_created", contact_id=contact_id, task_id=created.id)
        result.tasks_created += 1
        return TaskSnapshot(task_id=created.id, completed=False, completed_at=None)

    async def park(self, contact_id: str) -> ParkResult:
        """Finish a prospect's cadence by hand, for good — a conversation reached, a refusal.

        **Final:** a contact has one cadence ever (``uq_cadence_state_contact``) and cycle position
        is never reset, so there is no unpark. The open HubSpot task is **left alone** and returned
        for a person to close — this slice never writes to the founder's tasks — and **no reason is
        stored**: what happened belongs in HubSpot, as a note. Never reaches HubSpot.

        Taken under the sync lock: a running sync has already loaded its rows, and parking one
        under it could see the sync advance a prospect a human just finished.
        """
        now = self._clock()
        async with self._repository.sync_lock() as acquired:
            if not acquired:
                raise SyncRunningError(
                    "a cadence sync is running — park again when it has finished"
                )
            state = await self._repository.get_by_contact(contact_id)
            if state is None:
                raise NotEnrolledError(
                    f"contact {contact_id} has no cadence — nothing to park",
                    contact_id=contact_id,
                )
            if state.status == CadenceStatus.parked.value:
                raise AlreadyParkedError(
                    f"contact {contact_id} is already parked — parking is final",
                    contact_id=contact_id,
                )
            result = ParkResult(
                hubspot_contact_id=contact_id,
                cycle=state.cycle,
                touch=Touch(state.touch),
                open_task_id=state.hubspot_task_id,
                pending_task_key=state.pending_task_key,
                parked_at=now,
            )
            await self._repository.park(
                state, anchor_at=state.anchor_at, anchor_ref=state.anchor_ref, parked_at=now
            )
            await self._session.commit()
        logger.info(
            "cadence.service.prospect_parked",
            contact_id=contact_id,
            by="human",
            cycle=result.cycle,
            touch=result.touch.value,
            open_task_id=result.open_task_id,
            pending_task_key=result.pending_task_key,
        )
        return result

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
