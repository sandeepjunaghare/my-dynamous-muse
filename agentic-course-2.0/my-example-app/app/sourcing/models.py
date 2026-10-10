"""The ``sourcing_run`` and ``candidate`` tables.

T2's split, again: what the database keys, constrains or joins on is a real column; content that is
always read whole is JSONB, validated by a Pydantic model on the way in and out.

Every constraint below is declared here **and** written by hand in
``alembic/versions/0004_sourcing.py``. The model copy is what lets ``alembic check`` see them —
T2 found that an index living only in a migration is reported as "removed", and the next
``--autogenerate`` then quietly drops it.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
from app.sourcing.schemas import REGISTRY_ID_MAX_LENGTH

REGISTRY_ID_IS_CITED = (
    "coalesce("
    "jsonb_typeof(fields -> 'registry_id' -> 'value') = 'string'"
    " and registry_id = (fields -> 'registry_id' ->> 'value')"
    " and jsonb_typeof(fields -> 'registry_id' -> 'source_url') = 'string'"
    " and btrim(fields -> 'registry_id' ->> 'source_url') <> ''"
    " and jsonb_typeof(fields -> 'registry_id' -> 'retrieved_at') = 'string'"
    " and jsonb_typeof(fields -> 'registry_id' -> 'retrieval_method') = 'string'"
    ", false)"
)
"""The key column agrees with the cited ``fields.registry_id``, and that citation is complete.

Shared by ``candidate`` and ``sourcing_pool``. Null-safe on purpose: a comparison against a missing
key is NULL, and a CHECK *passes* on NULL — so without the ``coalesce(…, false)`` a row with no
citation at all, or one whose citation is JSON ``null``, would satisfy a constraint named for
refusing exactly that. Kept textually identical in the migrations that create each table.
"""


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
    disagree, and refuses a row whose registry id is missing or uncited, so the duplication costs
    nothing.
    """

    __tablename__ = "candidate"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(SourcingRun.id),
        nullable=False,
    )
    registry_id: Mapped[str] = mapped_column(String(REGISTRY_ID_MAX_LENGTH), nullable=False)

    fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """A ``CandidateFields``, dumped with ``CandidateFields.to_stored()`` — absent fields are
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
        # See REGISTRY_ID_IS_CITED: the key agrees with the citation, and the citation is complete.
        CheckConstraint(REGISTRY_ID_IS_CITED, name="ck_candidate_registry_id_is_cited"),
    )


class SourcingPool(Base):
    """The backlog a vertical's runs work through, one batch at a time (D12).

    Every record the registry's free filters let through, persisted across runs. Each run refreshes
    it from the registry, then takes a fixed batch, best first, into its own ``candidate`` rows.

    **Available** means: seen by *this* run's refresh, and either never batched or batched by a run
    that **failed**. So a run that dies — even one killed outright, which no ``except`` sees, once
    the reaper marks it failed — gives its batch back with no write of its own; a record that stops
    matching the manifest (gone inactive, moved away) drops out by not being seen; and nothing is
    batched twice by a run that finished.

    Keyed on ``vertical``, not ``manifest_id``: a new manifest version changes which records match,
    but must not forget which ones were already worked. The pool is the registry's — ``fields`` is
    overwritten whole on every refresh, and nothing but the registry stage writes it. Candidates,
    owned field by field, are where later stages add what they find.
    """

    __tablename__ = "sourcing_pool"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_id: Mapped[str] = mapped_column(String(REGISTRY_ID_MAX_LENGTH), nullable=False)

    fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """A ``CandidateFields`` from the registry, dumped like ``candidate.fields``."""

    currency_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """When the record's owner last refreshed it (FMCSA: the MCS-150 date). Orders the batch."""

    has_principal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Whether the record names a principal (FMCSA: ``company_officer_1``). Orders the batch."""

    first_seen_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=False
    )
    last_seen_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=False
    )
    """The latest run whose refresh returned this record. Only those are selectable."""

    batched_run_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=True
    )
    """The run that took this record into a batch. Never cleared: a failed run's batch is made
    available again by its status, not by erasing this."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("vertical", "registry_id", name="uq_sourcing_pool_vertical_registry_id"),
        CheckConstraint(REGISTRY_ID_IS_CITED, name="ck_sourcing_pool_registry_id_is_cited"),
        Index("ix_sourcing_pool_vertical_last_seen", "vertical", "last_seen_run_id"),
    )
