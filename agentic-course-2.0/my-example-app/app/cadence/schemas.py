"""The cadence slice's value types: what we read from HubSpot, what we decide, what we report.

Two families, kept apart on purpose:

* **Snapshots** (:class:`TaskSnapshot`, :class:`Activity`) are what HubSpot said at the moment of a
  sync. They are frozen and never persisted — outcomes are read, not mirrored (D4).
* **Decisions and reports** (:class:`Signal`, :class:`AdvancePlan`, :class:`SyncReport`) are what
  the done-policy concluded from them. The only part of a decision that is stored is the schedule
  it produces: the next position, its due date, and the anchor.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.cadence.machine import CadencePosition, CadenceStatus, Touch


class ActivityKind(StrEnum):
    """Engagement types read as evidence a touch happened. Values are HubSpot's path segments."""

    call = "calls"
    email = "emails"
    note = "notes"
    meeting = "meetings"


class TaskSnapshot(BaseModel):
    """A cadence task as HubSpot reported it during one sync."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    completed: bool
    completed_at: datetime | None
    """``hs_task_completion_date``, falling back to the record's ``updatedAt``. ``None`` only when
    HubSpot gave neither, in which case the sync's own clock stands in."""


class Activity(BaseModel):
    """One logged engagement on the contact: a call, an email, a note or a meeting."""

    model_config = ConfigDict(frozen=True)

    kind: ActivityKind
    activity_id: str
    occurred_at: datetime
    """``hs_timestamp`` — when the human says it happened, which is what D5 compares."""

    @property
    def ref(self) -> str:
        """``"calls:51230001"`` — unique across engagement types; the tie-breaker in ordering."""
        return f"{self.kind.value}:{self.activity_id}"

    @property
    def order_key(self) -> tuple[datetime, str]:
        """The total order activities are credited in: time first, then ref."""
        return (self.occurred_at, self.ref)


class SignalSource(StrEnum):
    """What closed a touch."""

    task_completed = "task_completed"
    activity = "activity"
    both = "both"
    """The task was completed **and** a matching activity was logged — one act, recorded twice."""


class Signal(BaseModel):
    """The evidence that one touch is done, and where the next touch's evidence must start."""

    model_config = ConfigDict(frozen=True)

    source: SignalSource
    done_at: datetime
    """When the touch was done. Drives the next touch's due date."""

    anchor_at: datetime
    anchor_ref: str | None
    """The next touch only counts activity strictly after ``(anchor_at, anchor_ref)``. ``None``
    sorts before every ref, so an activity logged at exactly ``anchor_at`` still counts — D5's
    tie, broken toward done."""


class AdvanceStep(BaseModel):
    """One touch closed during a sync."""

    model_config = ConfigDict(frozen=True)

    position: CadencePosition
    signal: Signal


class AdvancePlan(BaseModel):
    """What a sync should do to one prospect. Computed purely; applied by the service.

    * ``steps`` empty → nothing happened; leave the row alone.
    * ``next_position`` set → create **one** task for it, due ``next_due_at``.
    * ``steps`` non-empty and ``next_position`` ``None`` → the last touch closed; park.
    """

    model_config = ConfigDict(frozen=True)

    steps: tuple[AdvanceStep, ...] = ()
    next_position: CadencePosition | None = None
    next_due_at: datetime | None = None
    anchor_at: datetime | None = None
    anchor_ref: str | None = None

    @property
    def changed(self) -> bool:
        """Whether any touch closed."""
        return bool(self.steps)

    @property
    def parks(self) -> bool:
        """Whether this plan finishes the cadence."""
        return self.changed and self.next_position is None


class PendingTask(StrEnum):
    """What a dry run found for a row whose task create an earlier sync never confirmed."""

    found = "found"
    """HubSpot has a task carrying the row's key: the real sync adopts it."""
    would_create = "would_create"
    """No such task: the real sync creates it, once."""


class ProspectPlan(BaseModel):
    """What a sync **would** do to one prospect. Produced by a dry run; nothing is applied."""

    hubspot_contact_id: str
    steps: list[AdvanceStep] = Field(default_factory=list[AdvanceStep])
    """The touches that would close, in order, each with what closed it."""
    next_position: CadencePosition | None = None
    next_due_at: datetime | None = None
    parks: bool = False
    """The last touch would close, finishing the cadence."""
    pending_task: PendingTask | None = None
    pending_task_id: str | None = None
    """The task a ``found`` pending create would adopt."""
    pending_task_superseded: bool = False
    """A ``would_create`` task whose touch logged activity already closes: the real sync creates
    it and then lists it under ``open_tasks_superseded``, for a person to close."""
    superseded_task_id: str | None = None
    """An open task whose touch logged activity would close. Left alone, as in a real sync."""
    task_missing: bool = False
    """The current task no longer exists in HubSpot, and nothing would close the touch."""

    @property
    def changed(self) -> bool:
        """Whether any touch would close."""
        return bool(self.steps)


class ParkResult(BaseModel):
    """A cadence a human finished by hand."""

    hubspot_contact_id: str
    cycle: int
    touch: Touch
    """Where the prospect stood when it was parked."""
    open_task_id: str | None
    """The current touch's task, left open in HubSpot. We never write to the founder's tasks."""
    pending_task_key: str | None
    """Set when a task create had been interrupted: a task carrying this key may exist in
    HubSpot."""
    parked_at: datetime


class CadenceStateResponse(BaseModel):
    """One prospect's schedule, as the API and CLI render it. Schedule only — no outcomes."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hubspot_contact_id: str
    hubspot_company_id: str | None
    status: CadenceStatus
    cycle: int
    touch: Touch
    hubspot_task_id: str | None
    due_at: datetime | None
    enrolled_at: datetime
    parked_at: datetime | None


class OverdueTouch(BaseModel):
    """A live touch whose due date has passed."""

    hubspot_contact_id: str
    hubspot_company_id: str | None
    hubspot_task_id: str | None
    cycle: int
    touch: Touch
    due_at: datetime
    days_overdue: int
    """Whole days past due — ``0`` means due earlier today (Dallas time) or late last night."""


class SyncFailure(BaseModel):
    """A prospect the sync could not process. Whatever it had not committed is rolled back, and it
    is picked up again on the next sync.

    ``code`` is the HubSpot gateway's code for a HubSpot failure, ``invalid_hubspot_response``
    when a body failed validation, and ``database_error`` for a database failure.
    """

    hubspot_contact_id: str
    error: str
    code: str


class SyncReport(BaseModel):
    """What one sync did. Counts are per prospect except ``touches_closed``."""

    ran_at: datetime
    dry_run: bool = False
    """Nothing was applied: no task created, no row written, no touch counted. ``plans`` says what
    would have happened, and the counts below stay at zero."""
    skipped: bool = False
    """Another sync held the run lock, so this one did nothing. Not a failure: the running sync
    is doing the work."""
    checked: int = 0
    touches_closed: int = 0
    """Touches the cadence machine closed in this run — **the source for metric M5**, which counts
    closures under D5 (a ticked task *or* a matching logged activity), not HubSpot task status."""
    tasks_created: int = 0
    parked: int = 0
    unchanged: int = 0
    overdue: list[OverdueTouch] = Field(default_factory=list[OverdueTouch])
    missing_tasks: list[str] = Field(default_factory=list[str])
    """Contacts whose current task no longer exists in HubSpot. Not recreated — a person deleted
    it, and what that meant is theirs to say."""
    open_tasks_superseded: list[str] = Field(default_factory=list[str])
    """Task ids still open although a logged activity already closed their touch. Left alone — we
    never write to the founder's tasks — and listed so a person can close them."""
    pending_tasks_adopted: list[str] = Field(default_factory=list[str])
    """Task ids found by their idempotency key after an earlier create was interrupted (a 5xx, a
    timeout, a crash before commit) — adopted rather than created a second time."""
    failures: list[SyncFailure] = Field(default_factory=list[SyncFailure])
    warnings: list[str] = Field(default_factory=list[str])
    """Configuration a person should fix, e.g. no default task owner."""
    plans: list[ProspectPlan] = Field(default_factory=list[ProspectPlan])
    """A dry run's decision per prospect. Empty for a real sync."""


class RosterAction(StrEnum):
    """What the adoption roster says to do with one hand-worked contact."""

    adopt = "adopt"
    """Enrol at the touch its logged history reached (or the roster's ``start``)."""
    park = "park"
    """A refusal: record it as finished, so nothing can ever enrol it."""


class HandTask(BaseModel):
    """A task a person made by hand on the contact — any task without our key. Display only; never
    stored, and never written: adoption lists it for the founder to close."""

    task_id: str
    subject: str
    completed: bool
    due_at: datetime | None
    """``hs_timestamp``, the task's due date in HubSpot."""


class AdoptionPlan(BaseModel):
    """What adopting one contact would do, and the evidence behind it. Nothing in it is stored
    except the schedule an applied run writes."""

    hubspot_contact_id: str
    display_name: str | None = None
    """First and last name, read for the screen only. Never stored."""
    company_id: str | None = None
    action: RosterAction
    steps: list[AdvanceStep] = Field(default_factory=list[AdvanceStep])
    """The touches the contact's logged activity already closes, in order — the evidence."""
    start: CadencePosition | None = None
    """The touch the contact would start at. ``None`` when it would park."""
    due_at: datetime | None = None
    anchor_at: datetime
    anchor_ref: str | None = None
    """Evidence after ``(anchor_at, anchor_ref)`` is the first sync's; everything up to it is
    adoption's, and is never credited twice."""
    overridden: bool = False
    """The roster's ``start`` replaced the reconstructed position."""
    finished: bool = False
    """All nine touches are already logged: an ``adopt`` that parks."""
    hand_tasks: list[HandTask] = Field(default_factory=list[HandTask])
    warnings: list[str] = Field(default_factory=list[str])
    parked_position: CadencePosition | None = None
    """Where a parked row would record the contact: the touch it reached. Set only when it parks."""

    @property
    def parks(self) -> bool:
        """Whether applying this plan writes a parked row rather than enrolling."""
        return self.action is RosterAction.park or self.finished


class CompanyOnlyTask(BaseModel):
    """An open hand task on a company, with no contact on the task. A cadence is keyed on a
    contact, so the task itself cannot be adopted."""

    task_id: str
    subject: str
    company_ids: list[str] = Field(default_factory=list[str])
    company_contacts: list[str] = Field(default_factory=list[str])
    """Contacts the task's companies already have. Often the contact named after the company,
    carrying its own task: then this one is a duplicate to close, not a reason to add a contact.
    Empty means a person must add one in HubSpot before the company can be adopted."""


class AdoptionReport(BaseModel):
    """What one adoption run found, or did."""

    ran_at: datetime
    dry_run: bool = False
    """Nothing was written and no lock taken. ``plans`` says what applying would do."""
    plans: list[AdoptionPlan] = Field(default_factory=list[AdoptionPlan])
    """In a dry run, every plan drawn up; on apply, only the plans that were applied — an entry
    that failed or was enrolled meanwhile has none."""
    adopted: list[CadenceStateResponse] = Field(default_factory=list[CadenceStateResponse])
    """Cadences this run started, each with the task it created or found by key."""
    parked: list[CadenceStateResponse] = Field(default_factory=list[CadenceStateResponse])
    """Cadences this run recorded as finished."""
    already_enrolled: list[str] = Field(default_factory=list[str])
    """Contacts that already had a cadence. Skipped, so a second run adopts nothing new."""
    orphaned_tasks: list[str] = Field(default_factory=list[str])
    """Tasks this run created for a contact another run enrolled first — left open, to close by
    hand."""
    failures: list[SyncFailure] = Field(default_factory=list[SyncFailure])
    company_only: list[CompanyOnlyTask] = Field(default_factory=list[CompanyOnlyTask])
    warnings: list[str] = Field(default_factory=list[str])
