"""The ``weekly_run`` table: one row per weekly run, brief to pending-review list.

It is the orchestration's own state. It holds the run's options, links to the ``sourcing_run`` it
produced, and snapshots promotion's counts. It also holds the four timestamps M7 is measured from:
``requested_at`` → ``sourced_at`` → ``promoted_at`` → ``finished_at``. All four come from the
**database** clock, like ``sourcing_run``'s, so "how long did it take" never mixes two clocks.

**Why not columns on** ``sourcing_run``: M7 ends when promotion finishes, and recording that there
would have the promotion slice writing to sourcing's table. This table references sourcing's run
and is written only by ``app/runs/``.

**One running weekly run at a time** is a partial unique index, not an advisory lock. The work
outlives the request (202, then a background task), so a lock taken in the request would have to be
handed across to a task on a connection nobody's context manager owns. A row in ``running`` already
*is* the fact "a run is in progress"; the index makes that fact unique, and the stale-run reaper
handles the crash case (``WEEKLY_RUN_STALE_HOURS``).

Machine state: it never reaches HubSpot. Every constraint is declared here **and** written by hand
in ``alembic/versions/0011_weekly_run.py``, so ``alembic check`` sees them.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.sourcing.models import SourcingRun

ONE_RUNNING = "uq_weekly_run_one_running"
"""The one-at-a-time guard. Only a violation of this one means "a weekly run is in progress"; any
other integrity error is a bug and is raised as it is."""


class WeeklyRun(Base):
    """One weekly run: what was asked, which sourcing run it became, and how long each part took."""

    __tablename__ = "weekly_run"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why the run ended degraded or failed, every reason joined, in a person's words."""

    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    batch_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """``None`` means ``SOURCING_BATCH_SIZE``, read when the run executes."""
    allow_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    """M7's start: the moment the trigger was accepted."""

    sourcing_run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=True
    )
    """Set by the pipeline's ``on_started`` callback, before the first stage runs. A run that failed
    before sourcing opened (the manifest gone, say) has none."""

    sourced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """M7's end: promotion finished. Set for a dry run too: M7 measures the run's duration."""
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    promotion_counts: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """A snapshot of ``PromotionReport.counts``. Empty until promotion has run."""
    promotion_stopped: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``PromotionReport.stopped``: why the promote pass stopped early, if it did."""

    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'completed', 'degraded', 'failed')",
            name="ck_weekly_run_status",
        ),
        CheckConstraint(
            "(status = 'running') = (finished_at is null)",
            name="ck_weekly_run_finished_at_matches_status",
        ),
        CheckConstraint(
            "batch_size is null or batch_size > 0",
            name="ck_weekly_run_batch_size",
        ),
        CheckConstraint(
            "promoted_at is null or sourced_at is not null",
            name="ck_weekly_run_promoted_after_sourced",
        ),
        UniqueConstraint("sourcing_run_id", name="uq_weekly_run_sourcing_run_id"),
        Index(ONE_RUNNING, "status", unique=True, postgresql_where=text("status = 'running'")),
        Index("ix_weekly_run_requested_at", "requested_at"),
    )
