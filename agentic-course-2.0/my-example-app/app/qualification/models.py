"""The ``disqualification`` table — the engine's memory of what it already rejected.

This is what stops the weekly run re-sourcing the same national rollups, which a HubSpot-only model
cannot do (architecture → *Data model*). It is machine state: it never reaches HubSpot.

**Suppression is keyed on ``(vertical, registry_id)``, not on the candidate.** Every run writes new
``candidate`` rows (unique per run), so a lookup by candidate id would suppress nothing next week.
The candidate, run and manifest are still recorded, so a person can see exactly which version's
rule fired on which sourcing.

Every constraint is declared here **and** hand-written in ``alembic/versions/0008_qualification.py``
so ``alembic check`` sees them (T2's lesson).
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import func as sql_func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.manifests.models import VerticalManifest
from app.sourcing.models import Candidate, SourcingRun
from app.sourcing.schemas import REGISTRY_ID_MAX_LENGTH


class Disqualification(Base):
    """One rule that fired on one candidate, with the citation it fired on."""

    __tablename__ = "disqualification"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(Candidate.id), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(SourcingRun.id), nullable=False
    )
    manifest_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey(VerticalManifest.id), nullable=False
    )
    """The manifest *version* whose rule fired — what gives ``rule_id`` its meaning."""

    vertical: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_id: Mapped[str] = mapped_column(String(REGISTRY_ID_MAX_LENGTH), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_kind: Mapped[str] = mapped_column(String(16), nullable=False)

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_method: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The value a predicate saw, or the judgment node's reason and quote."""

    disqualified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_func.now()
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "rule_id", name="uq_disqualification_candidate_rule"),
        CheckConstraint(
            "rule_kind in ('predicate', 'judgment')", name="ck_disqualification_rule_kind"
        ),
        CheckConstraint("btrim(source_url) <> ''", name="ck_disqualification_source_url"),
        Index("ix_disqualification_vertical_registry_id", "vertical", "registry_id"),
    )
