"""Outcome sync: read what happened from HubSpot, and decide what it means for the schedule.

**We own the schedule; HubSpot owns the outcomes (D4).** Nothing read here is stored. Each sync
re-reads the current task and the contact's logged activity, and the pure functions below turn that
into an :class:`~app.cadence.schemas.AdvancePlan`. The service persists only the schedule the plan
produces.

The done-signal (D5)
--------------------
A touch is done when its **task is complete**, *or* a **matching activity** was logged after the
point the touch became current. Ties break toward done — "a machine that nags about a call you
already made is worse than one that occasionally advances early".

Each live prospect carries an **anchor** ``(anchor_at, anchor_ref)``. At enrolment it is the
enrolment instant, which precedes the first task's creation; afterwards it is the signal that closed
the previous touch, which precedes the next task's creation. Either way it is never later than the
current task's ``createdAt``, so everything D5 counts is counted. Activities are totally ordered by
``(hs_timestamp, "<type>:<id>")``, and one counts only when it sorts strictly after the anchor:

* an activity at exactly ``anchor_at`` counts when the anchor came from a task completion or from
  enrolment (``anchor_ref`` is ``None``, which sorts below every ref) — the tie, toward done;
* the activity that *set* the anchor never counts again — **each activity closes at most one
  touch**, which is what stops one logged call from walking a prospect through three touches.

After a touch closes, the next touch is checked against the same activities *before* a task is
created for it. That is the "already done by hand" case: it advances rather than re-firing.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from app.cadence.machine import TOTAL_TOUCHES, CadencePosition, Touch, due_after, next_position
from app.cadence.schemas import (
    Activity,
    ActivityKind,
    AdvancePlan,
    AdvanceStep,
    Signal,
    SignalSource,
    TaskSnapshot,
)
from app.core.logging import get_logger
from app.promotion.client import HubSpotClient
from app.promotion.schemas import HubSpotObject, ObjectType, TaskStatus

logger = get_logger(__name__)

MATCHING_KINDS: dict[Touch, frozenset[ActivityKind]] = {
    Touch.call: frozenset({ActivityKind.call, ActivityKind.meeting, ActivityKind.note}),
    Touch.voicemail: frozenset({ActivityKind.call, ActivityKind.note}),
    Touch.email: frozenset({ActivityKind.email, ActivityKind.note}),
}
"""What counts as evidence of each touch.

**Notes count for every touch** because that is how the founders log touches today: E14 found the
22 fire-vertical outreach touches as NOTE objects, with zero logged calls or emails (E17). Excluding
notes would re-fire every touch already done and logged the way people actually log them. A meeting
counts for the call — a conversation is more than the call was trying to get.
"""

TASK_PROPERTIES = ("hs_task_status", "hs_task_completion_date", "hs_timestamp")
ACTIVITY_PROPERTIES = ("hs_timestamp",)

_OBJECT_TYPES: dict[ActivityKind, ObjectType] = {
    ActivityKind.call: ObjectType.calls,
    ActivityKind.email: ObjectType.emails,
    ActivityKind.note: ObjectType.notes,
    ActivityKind.meeting: ObjectType.meetings,
}


def _is_after(activity: Activity, anchor_at: datetime, anchor_ref: str | None) -> bool:
    return activity.order_key > (anchor_at, anchor_ref or "")


def find_signal(
    touch: Touch,
    anchor_at: datetime,
    anchor_ref: str | None,
    task: TaskSnapshot | None,
    activities: Iterable[Activity],
    now: datetime,
) -> Signal | None:
    """Whether ``touch`` is done, and the anchor the next touch must start from. Pure.

    1. Task completed **and** a matching activity → one act recorded twice: the activity is
       consumed, so it cannot close the next touch as well. Done at the earlier of the two.
    2. Task completed, no activity → done at the completion time.
    3. Matching activity only → done at the earliest one, which is consumed.
    4. Neither → not done.

    The anchor only ever moves forward: a completion timestamp earlier than the current anchor
    (clock skew between a laptop and HubSpot) must not re-open activity already credited.
    """
    candidates = sorted(
        (
            activity
            for activity in activities
            if activity.kind in MATCHING_KINDS[touch] and _is_after(activity, anchor_at, anchor_ref)
        ),
        key=lambda activity: activity.order_key,
    )
    earliest = candidates[0] if candidates else None
    completed = task is not None and task.completed

    if completed and task is not None:
        completed_at = task.completed_at or now
        if earliest is not None:
            return Signal(
                source=SignalSource.both,
                done_at=min(completed_at, earliest.occurred_at),
                anchor_at=earliest.occurred_at,
                anchor_ref=earliest.ref,
            )
        if (completed_at, "") > (anchor_at, anchor_ref or ""):
            return Signal(
                source=SignalSource.task_completed,
                done_at=completed_at,
                anchor_at=completed_at,
                anchor_ref=None,
            )
        return Signal(
            source=SignalSource.task_completed,
            done_at=completed_at,
            anchor_at=anchor_at,
            anchor_ref=anchor_ref,
        )

    if earliest is not None:
        return Signal(
            source=SignalSource.activity,
            done_at=earliest.occurred_at,
            anchor_at=earliest.occurred_at,
            anchor_ref=earliest.ref,
        )
    return None


def plan_advance(
    position: CadencePosition,
    anchor_at: datetime,
    anchor_ref: str | None,
    task: TaskSnapshot | None,
    activities: Sequence[Activity],
    now: datetime,
) -> AdvancePlan:
    """Walk forward from ``position`` as far as the evidence goes. Pure.

    Only the current touch has a task; every later touch is checked against activity alone, before
    any task exists for it. The loop ends at the first touch that is not done — which gets exactly
    one task — or at the end of the cadence, which parks. It cannot run more than nine steps, the
    length of the whole cadence.
    """
    steps: list[AdvanceStep] = []
    current = position
    current_task = task
    for _ in range(TOTAL_TOUCHES):
        signal = find_signal(current.touch, anchor_at, anchor_ref, current_task, activities, now)
        if signal is None:
            break
        steps.append(AdvanceStep(position=current, signal=signal))
        anchor_at, anchor_ref = signal.anchor_at, signal.anchor_ref
        following = next_position(current)
        if following is None:
            return AdvancePlan(steps=tuple(steps), anchor_at=anchor_at, anchor_ref=anchor_ref)
        current = following
        current_task = None

    if not steps:
        return AdvancePlan()
    return AdvancePlan(
        steps=tuple(steps),
        next_position=current,
        next_due_at=due_after(current, steps[-1].signal.done_at),
        anchor_at=anchor_at,
        anchor_ref=anchor_ref,
    )


def parse_hubspot_datetime(raw: str | None) -> datetime | None:
    """Parse a HubSpot datetime property. ``None`` for blank or unreadable.

    The objects API returns ISO-8601 (``2026-09-29T15:00:00.000Z``); epoch milliseconds — the form
    we *write* ``hs_timestamp`` in — is accepted too, because which one a read returns has varied
    across HubSpot's API versions. A naive value is taken as UTC, which is what HubSpot stores.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=UTC)
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_task_snapshot(record: HubSpotObject) -> TaskSnapshot:
    """A task read back from HubSpot, reduced to the two facts the done-policy needs."""
    completed = record.properties.get("hs_task_status") == TaskStatus.completed.value
    completed_at: datetime | None = None
    if completed:
        completed_at = (
            parse_hubspot_datetime(record.properties.get("hs_task_completion_date"))
            or record.updated_at
        )
    return TaskSnapshot(task_id=record.id, completed=completed, completed_at=completed_at)


class OutcomeReader:
    """Reads outcomes from HubSpot for the done-policy. Reads only — it writes nothing anywhere.

    Also the seam T13 needs: adoption reconstructs a prospect's cycle position from the same
    activity this reads, through the same :func:`find_signal`.
    """

    def __init__(self, client: HubSpotClient) -> None:
        self._client = client

    async def tasks(self, task_ids: Sequence[str]) -> dict[str, TaskSnapshot]:
        """The current state of each task, keyed by id. A deleted task is simply absent."""
        records = await self._client.batch_read(ObjectType.tasks, task_ids, TASK_PROPERTIES)
        return {record.id: to_task_snapshot(record) for record in records}

    async def activities(self, contact_id: str, since: datetime | None = None) -> list[Activity]:
        """Every call, email, note and meeting on the contact, optionally from ``since`` on.

        ``since`` is a cheap pre-filter, inclusive so the D5 tie survives it; the exact rule is
        :func:`find_signal`'s. An engagement whose timestamp cannot be read is skipped and logged —
        it cannot be ordered, so it cannot be credited honestly.
        """
        found: list[Activity] = []
        for kind, object_type in _OBJECT_TYPES.items():
            ids = await self._client.list_associated_ids(
                ObjectType.contacts, contact_id, object_type
            )
            if not ids:
                continue
            records = await self._client.batch_read(object_type, ids, ACTIVITY_PROPERTIES)
            for record in records:
                occurred_at = parse_hubspot_datetime(record.properties.get("hs_timestamp"))
                if occurred_at is None:
                    logger.warning(
                        "cadence.sync.activity_unreadable",
                        contact_id=contact_id,
                        kind=kind.value,
                        activity_id=record.id,
                    )
                    continue
                if since is not None and occurred_at < since:
                    continue
                found.append(Activity(kind=kind, activity_id=record.id, occurred_at=occurred_at))
        return sorted(found, key=lambda activity: activity.order_key)
