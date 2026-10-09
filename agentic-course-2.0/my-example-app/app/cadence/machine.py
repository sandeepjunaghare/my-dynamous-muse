"""The three-touch cadence as a pure state machine: positions, transitions, due dates, task text.

**call → voicemail → same-day email draft → wait four days → repeat twice → park after three
cycles.** Nine touches, then parked:

    (1, call) → (1, voicemail) → (1, email) ─4 days→ (2, call) → … → (3, email) → parked

Nothing in this module does I/O or reads a clock. It answers three questions — *what comes after
this touch?*, *when is it due?*, *what does its task ask a human to do?* — so that the walk can be
tested exhaustively without a database or a portal, and so T13 can replay history through the same
transitions when it reconstructs an adopted prospect's position.

Due dates are **end of the local day in America/Chicago**. DFW is the only metro (multi-metro is a
PRD non-goal, not a "later"), so this is a module constant rather than configuration. "Same-day"
means the same *local* day: a voicemail done at 8 pm Dallas time is 1 am UTC the next day, and a
UTC date would schedule the email a day late.
"""

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.promotion.schemas import TaskCreate, TaskType

CADENCE_TZ = ZoneInfo("America/Chicago")
"""The cadence runs on Dallas time. ``python:3.12-slim`` ships tzdata, so this resolves there."""

CYCLES = 3
"""Repeat twice: three cycles in all, then park."""

WAIT_DAYS = 4
"""Days between a cycle's email and the next cycle's call."""

END_OF_DAY = time(23, 59)
"""A touch "due today" is due by the end of the local day, so a same-day touch is not born late."""


class Touch(StrEnum):
    """The three touches of one cycle, in order."""

    call = "call"
    voicemail = "voicemail"
    email = "email"


TOUCH_ORDER: tuple[Touch, ...] = (Touch.call, Touch.voicemail, Touch.email)


class CadenceStatus(StrEnum):
    """Live or parked — the only two states a prospect's schedule can be in."""

    live = "live"
    parked = "parked"


class CadencePosition(BaseModel):
    """Where a prospect is: which cycle, which touch.

    Frozen and validated, so an impossible position (cycle 4, cycle 0) cannot be constructed —
    T13's reconstruction gets the same guarantee as the sync.
    """

    model_config = ConfigDict(frozen=True)

    cycle: int = Field(ge=1, le=CYCLES)
    touch: Touch

    @property
    def ordinal(self) -> int:
        """1 to 9: this touch's place in the whole cadence."""
        return (self.cycle - 1) * len(TOUCH_ORDER) + TOUCH_ORDER.index(self.touch) + 1

    @classmethod
    def first(cls) -> Self:
        """Cycle one, the call."""
        return cls(cycle=1, touch=Touch.call)

    def label(self) -> str:
        """``"cycle 2 of 3 · voicemail"`` — for task subjects and the CLI."""
        return f"cycle {self.cycle} of {CYCLES} · {self.touch.value}"


TOTAL_TOUCHES = CYCLES * len(TOUCH_ORDER)


def next_position(position: CadencePosition) -> CadencePosition | None:
    """The touch after this one, or ``None`` when the cadence is finished and the prospect parks."""
    index = TOUCH_ORDER.index(position.touch)
    if index + 1 < len(TOUCH_ORDER):
        return CadencePosition(cycle=position.cycle, touch=TOUCH_ORDER[index + 1])
    if position.cycle < CYCLES:
        return CadencePosition(cycle=position.cycle + 1, touch=TOUCH_ORDER[0])
    return None


def local_date(moment: datetime) -> date:
    """The Dallas calendar date of an instant."""
    return _require_aware(moment).astimezone(CADENCE_TZ).date()


def end_of_local_day(day: date) -> datetime:
    """23:59 Dallas time on ``day``, as a UTC instant. DST is handled by the zone, not by us."""
    return datetime.combine(day, END_OF_DAY, tzinfo=CADENCE_TZ).astimezone(UTC)


def due_at_enrolment(enrolled_at: datetime) -> datetime:
    """The first call is due by the end of the day the prospect enters the machine."""
    return end_of_local_day(local_date(enrolled_at))


def due_after(position: CadencePosition, previous_done_at: datetime, *, now: datetime) -> datetime:
    """When ``position`` is due, given when the touch before it was done and when we are asking.

    Voicemail and email are **same-day** touches: due by the end of the local day the previous
    touch was done. A cycle's call (other than the first) comes **four days** after the email.

    Never earlier than the end of *today*. The sync runs daily, so it routinely learns about
    yesterday's call this morning; a task due yesterday would be born overdue, and an overdue list
    that is wrong on day one is a list nobody trusts (E15). The floor only ever moves a date later —
    a sync that runs on time gets exactly the schedule above.
    """
    done_on = local_date(previous_done_at)
    if position.touch is Touch.call:
        scheduled = end_of_local_day(done_on + timedelta(days=WAIT_DAYS))
    else:
        scheduled = end_of_local_day(done_on)
    return max(scheduled, end_of_local_day(local_date(now)))


def _require_aware(moment: datetime) -> datetime:
    """A naive datetime here would be read as the host's local zone — a laptop and a VPS would
    schedule the same touch on different days from identical code."""
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("cadence times must be timezone-aware")
    return moment


_CONSTRAINTS = (
    "Lead with the friction the owner already feels. Never lead with AI (E16). "
    "Do not pitch — ask, and capture their exact words and a number."
)

_TASK_TYPES: dict[Touch, TaskType] = {
    Touch.call: TaskType.call,
    Touch.voicemail: TaskType.call,
    Touch.email: TaskType.email,
}

_BODIES: dict[Touch, str] = {
    Touch.call: (
        "Call the owner. If nobody answers, the next touch is a voicemail, today. " + _CONSTRAINTS
    ),
    Touch.voicemail: (
        "If the call went unanswered, leave a voicemail today; the email follows the same day. "
        + _CONSTRAINTS
    ),
    Touch.email: (
        "DRAFT — write and send this email yourself, today. Nothing is sent automatically: no "
        "email leaves any domain from this system until Spike 4 closes. The opener is being "
        "rewritten, so this task carries no copy. " + _CONSTRAINTS
    ),
}


TASK_KEY_PREFIX = "lpe-cadence"


def task_key(contact_id: str, position: CadencePosition) -> str:
    """The idempotency key for one touch's task: ``lpe-cadence:400112233:2-voicemail``.

    Deterministic because each (contact, position) gets at most one task, ever — cycle position
    is never reset. It is written into the task body, which is how a sync interrupted mid-create
    recognises the task HubSpot made but never confirmed.
    """
    return f"{TASK_KEY_PREFIX}:{contact_id}:{position.cycle}-{position.touch.value}"


def task_for(
    position: CadencePosition, due_at: datetime, owner_id: str | None, *, key: str
) -> TaskCreate:
    """The HubSpot task for one touch — a request to a human, never an action taken.

    The email touch is an EMAIL-typed task, which in HubSpot is a reminder, not a message: creating
    it sends nothing. No outreach copy is generated here; who rewrites the opener is an open
    question (PRD §9, E16), and a generated draft would answer it silently. The body ends with
    ``key`` (:func:`task_key`) so the task can be found again after an ambiguous create.
    """
    subject_touch = (
        "email draft (send by hand)" if position.touch is Touch.email else (position.touch.value)
    )
    return TaskCreate(
        subject=f"Cadence {position.cycle}/{CYCLES} · {subject_touch}",
        body=f"{_BODIES[position.touch]}\n\nRef: {key}",
        due_at=due_at,
        task_type=_TASK_TYPES[position.touch],
        owner_id=owner_id,
    )
