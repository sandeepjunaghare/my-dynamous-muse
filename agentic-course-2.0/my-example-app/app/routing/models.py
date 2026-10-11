"""The ``route_assignment`` table: which drive route each clustered candidate is on, and where.

Routing is machine state, so it lives here rather than in ``candidate.fields``: the point is what
routing clustered on, not a prospect field a person reads, and ``cluster_routes`` owns no candidate
field (``app/sourcing/stages.py``). A candidate that was not clustered has no row; the run's counts
say why.

Every constraint below is declared here **and** written by hand in
``alembic/versions/0007_routing.py`` (the run-scoped foreign key in ``0009``), so ``alembic check``
can see them (T2's finding).
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.sourcing.models import Candidate, SourcingRun


class RouteAssignment(Base):
    """One candidate on one drive route of one run."""

    __tablename__ = "route_assignment"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(SourcingRun.id),
        nullable=False,
    )
    candidate_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    """Unique: a candidate is on at most one route.

    It must be one of ``run_id``'s own candidates, **enforced in the database** by the composite
    foreign key ``(run_id, candidate_id) → candidate(run_id, id)`` (#20). The run's rows are
    replaced by ``run_id``, so a row naming another run's candidate would be missed by one re-run
    and erased by the wrong one."""

    cluster_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """The route's position in label order: 0 is route A, west of route B."""

    cluster_label: Mapped[str] = mapped_column(String(32), nullable=False)
    """``"A · 75238"``: the letter and the route's most common ZIP."""

    point: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """A cited ``GeoPoint`` from the Census Geocoder, dumped with ``mode="json"``.

    Never a Places point (D13)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            [Candidate.run_id, Candidate.id],
            name="fk_route_assignment_run_candidate",
        ),
        UniqueConstraint("candidate_id", name="uq_route_assignment_candidate_id"),
        CheckConstraint(
            "cluster_index >= 0",
            name="ck_route_assignment_cluster_index_non_negative",
        ),
        Index("ix_route_assignment_run_id", "run_id"),
    )
