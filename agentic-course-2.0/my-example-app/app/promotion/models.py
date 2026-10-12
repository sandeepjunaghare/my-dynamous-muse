"""The ``promotion`` ledger — one row per business ever promoted, or attempted, per vertical.

**A state machine, not a log** (:class:`~app.promotion.schemas.PromotionStatus`). The questions
every caller asks — "has this business been promoted?", "which are waiting for review?", "which
accepted ones still need their first touch?" — are each one indexed read on ``status``.

**Keyed on ``(vertical, registry_id)``, not on the candidate.** Every run writes new ``candidate``
rows, so a key on the candidate would let next week's run promote the same business again. The
unique key is the idempotency guarantee across runs; the candidate and its run are still recorded,
run-scoped by the composite foreign key (#20).

**Intent before an ambiguous create.** The row is committed in ``pending_create`` *before* the
HubSpot create, carrying ``company_key`` (the company's ``lpe_source_url``), so an interrupted
create is found again rather than repeated. ``dedupe_phone`` / ``dedupe_domain`` are stored before
the create too: they catch two candidates of one batch resolving to one company, which HubSpot's
lagging search cannot see.

Machine state: it never reaches HubSpot. Every constraint is declared here **and** hand-written in
``alembic/versions/0010_promotion.py`` so ``alembic check`` sees them (T2's lesson).
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy import func as sql_func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.sourcing.models import Candidate, SourcingRun
from app.sourcing.schemas import REGISTRY_ID_MAX_LENGTH

STATUS_IN_SET = (
    "status in ('pending_create', 'pending_review', 'enrolling', 'enrolled', 'rejected', "
    "'duplicate', 'withdrawn')"
)
"""Every value of ``PromotionStatus``; a structure test keeps the two in step."""

IDS_MATCH_STATUS = (
    "status not in ('pending_review', 'enrolling', 'enrolled', 'rejected', 'withdrawn') "
    "or (hubspot_company_id is not null and hubspot_contact_id is not null)"
)
"""Past ``pending_create`` (and not a duplicate), both HubSpot records exist and are recorded."""

DUPLICATE_HAS_MATCH = (
    "(status = 'duplicate') = (matched_company_id is not null or duplicate_of_id is not null)"
)
"""A duplicate names what it duplicates; nothing else carries a match."""

ENROLLED_AT_MATCHES_STATUS = "(status = 'enrolled') = (enrolled_at is not null)"


class Promotion(Base):
    """One business's way from a qualified candidate to the founder's cadence."""

    __tablename__ = "promotion"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_id: Mapped[str] = mapped_column(String(REGISTRY_ID_MAX_LENGTH), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=False
    )
    """The run whose candidate claimed the row. Run-scoped with ``candidate_id`` (#20)."""
    candidate_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    company_key: Mapped[str] = mapped_column(Text, nullable=False)
    """The company's ``lpe_source_url`` — how an interrupted create is found again."""

    dedupe_phone: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """``normalize_phone`` of the candidate's phone; ``None`` when it is not a usable match key."""
    dedupe_domain: Mapped[str | None] = mapped_column(String(253), nullable=True)

    hubspot_company_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hubspot_contact_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_company_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """The existing HubSpot company a ``duplicate`` matched; ``None`` for a ledger-row match."""
    duplicate_of_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("promotion.id", name="fk_promotion_duplicate_of"),
        nullable=True,
    )
    """The ledger row an in-run or cross-run duplicate matched."""

    name_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    """OQ4: the owner name's split was uncertain — flagged for review, counted in the report."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When a sync read the founder's accept or reject."""
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            [Candidate.run_id, Candidate.id],
            name="fk_promotion_run_candidate",
        ),
        UniqueConstraint("vertical", "registry_id", name="uq_promotion_vertical_registry_id"),
        UniqueConstraint("hubspot_company_id", name="uq_promotion_hubspot_company_id"),
        UniqueConstraint("hubspot_contact_id", name="uq_promotion_hubspot_contact_id"),
        CheckConstraint(STATUS_IN_SET, name="ck_promotion_status"),
        CheckConstraint("btrim(company_key) <> ''", name="ck_promotion_company_key"),
        CheckConstraint(IDS_MATCH_STATUS, name="ck_promotion_ids_match_status"),
        CheckConstraint(DUPLICATE_HAS_MATCH, name="ck_promotion_duplicate_has_match"),
        CheckConstraint(ENROLLED_AT_MATCHES_STATUS, name="ck_promotion_enrolled_at"),
        Index("ix_promotion_status", "status"),
        Index("ix_promotion_vertical_dedupe_phone", "vertical", "dedupe_phone"),
        Index("ix_promotion_vertical_dedupe_domain", "vertical", "dedupe_domain"),
    )
