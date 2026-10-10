"""route_assignment — which drive route each clustered candidate is on

One row per candidate that routing clustered: the run, the candidate, its Census Geocoder point
(cited; never Places, D13) and its route's index and label. A re-run of the stage replaces the run's
rows, so a candidate is on at most one route.

Hand-written to match ``app/routing/models.py`` exactly, so ``alembic check`` stays clean.

Pre-numbered for Wave 5 (T5 ``0006``, T8 ``0007``, T7 ``0008``). ``down_revision`` is the head at
fork time; re-chain it onto T5's ``0006`` at merge.

Revision ID: 0007_routing
Revises: 0005_cadence
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_routing"
down_revision: str | None = "0005_cadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``route_assignment`` and its run lookup index."""
    op.create_table(
        "route_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate.id"),
            nullable=False,
        ),
        sa.Column("cluster_index", sa.SmallInteger(), nullable=False),
        sa.Column("cluster_label", sa.String(length=32), nullable=False),
        sa.Column("point", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("candidate_id", name="uq_route_assignment_candidate_id"),
        sa.CheckConstraint(
            "cluster_index >= 0",
            name="ck_route_assignment_cluster_index_non_negative",
        ),
    )
    op.create_index("ix_route_assignment_run_id", "route_assignment", ["run_id"])


def downgrade() -> None:
    """Drop the index, then the table."""
    op.drop_index("ix_route_assignment_run_id", table_name="route_assignment")
    op.drop_table("route_assignment")
