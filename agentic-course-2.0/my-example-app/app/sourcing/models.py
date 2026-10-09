"""The ``sourcing_run`` and ``candidate`` tables.

T2's split, again: what the database keys, constrains or joins on is a real column; content that is
always read whole is JSONB, validated by a Pydantic model on the way in and out.

Every constraint below is declared here **and** written by hand in
``alembic/versions/0004_sourcing.py``. The model copy is what lets ``alembic check`` see them —
T2 found that an index living only in a migration is reported as "removed", and the next
``--autogenerate`` then quietly drops it.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.manifests.models import VerticalManifest


class SourcingRun(Base):
    """One execution of a brief: what was asked, under which manifest, how it went, what it cost."""

    __tablename__ = "sourcing_run"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)

    manifest_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(VerticalManifest.id),
        nullable=False,
    )
    """The exact manifest *version* the run sourced under — which gives its rule ids meaning."""

    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    geography: Mapped[str] = mapped_column(String(64), nullable=False)
    icp_band: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The brief's band. Serialised with ``IcpBand.model_dump(mode="json")``."""

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why a run ended degraded or failed, in a person's words. Optional."""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    counts: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    """``{stage_name: n}``. JSONB so each stage ticket adds its count without editing this table."""

    cost: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    """A ``RunCostSummary``: calls and USD per billable kind. USD is a string, kept exact."""

    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'completed', 'degraded', 'failed')",
            name="ck_sourcing_run_status",
        ),
        # A running run has no finish time and a finished one must. Without this a run can claim to
        # be both, and "how long did it take" silently answers from whichever field was read.
        CheckConstraint(
            "(status = 'running') = (finished_at is null)",
            name="ck_sourcing_run_finished_at_matches_status",
        ),
        Index("ix_sourcing_run_vertical", "vertical"),
    )


class Candidate(Base):
    """One business a run sourced, pre-qualification, with every field cited.

    ``registry_id`` is stored twice on purpose: as a column, because it is half the upsert key; and
    as the cited ``fields.registry_id``, because it is a field like any other and a registry id
    nobody can cite is E10. ``ck_candidate_registry_id_is_cited`` makes the two impossible to
    disagree, so the duplication costs nothing.
    """

    __tablename__ = "candidate"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(SourcingRun.id),
        nullable=False,
    )
    registry_id: Mapped[str] = mapped_column(String(128), nullable=False)

    fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """A ``CandidateFields``, dumped with ``mode="json", exclude_none=True`` — absent fields are
    absent keys, never JSON ``null``, which is what lets the upsert merge without erasing."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("run_id", "registry_id", name="uq_candidate_run_registry_id"),
        CheckConstraint(
            "registry_id = (fields -> 'registry_id' ->> 'value')",
            name="ck_candidate_registry_id_is_cited",
        ),
    )
