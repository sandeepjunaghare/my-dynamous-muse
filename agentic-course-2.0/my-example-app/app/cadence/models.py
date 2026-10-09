"""The ``cadence_state`` table — **the schedule only**.

One row per enrolled HubSpot contact, holding which touch is due, which cycle we are in, and whether
the prospect is live or parked. **No outcomes**: whether a touch happened and what was said live in
HubSpot and are re-read on every sync, never copied here (D4, M4 — one home per fact).

What looks closest to an outcome is the **anchor** (``anchor_at`` / ``anchor_ref``). It is not a
record of what happened; it is the cursor the done-policy needs to know *which* activity is new —
"evidence for the current touch starts after here". Dropping it would mean either re-crediting old
activity or storing the full history, which is the shadow CRM M4 exists to prevent.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CadenceState(Base):
    """One prospect's place in the three-touch cadence."""

    __tablename__ = "cadence_state"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    hubspot_contact_id: Mapped[str] = mapped_column(String(32), nullable=False)
    hubspot_company_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hubspot_owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Applied to every task this cadence creates. Unassigned when nobody was named."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    cycle: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    touch: Mapped[str] = mapped_column(String(16), nullable=False)

    hubspot_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """The open HubSpot task for the current touch. ``None`` once parked, and ``None`` while the
    current touch's task is **pending** (see ``pending_task_key``)."""

    pending_task_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    """Set while the current touch's task may or may not exist in HubSpot.

    Written and committed **before** the task create, cleared in the same commit that records the
    task's id. A row still carrying it was interrupted mid-create — a 5xx, a timeout or a crash —
    so the next sync looks for a task carrying this key among the contact's tasks before it would
    ever create one. Schedule state (an intent), not an outcome."""

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the current touch is due. ``None`` once parked."""

    anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    anchor_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """``"<type>:<id>"`` of the activity that set the anchor, or ``None`` when a task completion
    or the enrolment set it. See ``app/cadence/sync.py`` for the ordering rule."""

    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    parked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One cadence per contact, ever: re-enrolling would reset the cycle, which the ground
        # rules forbid. The service raises the readable error; this is the guarantee.
        UniqueConstraint("hubspot_contact_id", name="uq_cadence_state_contact"),
        CheckConstraint("status in ('live', 'parked')", name="ck_cadence_state_status"),
        CheckConstraint("touch in ('call', 'voicemail', 'email')", name="ck_cadence_state_touch"),
        CheckConstraint("cycle between 1 and 3", name="ck_cadence_state_cycle"),
        # A live prospect always has a due date and either its open task or a pending intent to
        # create one, which the next sync resolves. Without this, a row could be live with
        # nothing in HubSpot for anyone to do — the silent drop E15 measured.
        CheckConstraint(
            "status = 'parked' or (due_at is not null"
            " and (hubspot_task_id is not null or pending_task_key is not null))",
            name="ck_cadence_state_live_has_task",
        ),
        CheckConstraint(
            "status = 'live' or pending_task_key is null",
            name="ck_cadence_state_parked_has_no_pending",
        ),
        Index("ix_cadence_state_status_due_at", "status", "due_at"),
    )
