"""weekly_run — one row per weekly run, brief to pending-review list (T10)

Links a weekly run to the ``sourcing_run`` it produced, snapshots promotion's counts, and records
the timestamps M7 is measured from. The partial unique index ``uq_weekly_run_one_running`` allows
at most one ``running`` row: the one-at-a-time guard.

Hand-written to match ``app/runs/models.py`` exactly, so ``alembic check`` stays clean.

Revision ID: 0011_weekly_run
Revises: 0010_promotion
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_weekly_run"
down_revision: str | None = "0010_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``weekly_run`` and its indexes."""
    op.create_table(
        "weekly_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("vertical", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("allow_degraded", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "sourcing_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=True,
        ),
        sa.Column("sourced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "promotion_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("promotion_stopped", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('running', 'completed', 'degraded', 'failed')",
            name="ck_weekly_run_status",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at is null)",
            name="ck_weekly_run_finished_at_matches_status",
        ),
        sa.CheckConstraint(
            "batch_size is null or batch_size > 0",
            name="ck_weekly_run_batch_size",
        ),
        sa.CheckConstraint(
            "promoted_at is null or sourced_at is not null",
            name="ck_weekly_run_promoted_after_sourced",
        ),
        sa.UniqueConstraint("sourcing_run_id", name="uq_weekly_run_sourcing_run_id"),
    )
    op.create_index(
        "uq_weekly_run_one_running",
        "weekly_run",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("ix_weekly_run_requested_at", "weekly_run", ["requested_at"])


def downgrade() -> None:
    """Drop ``weekly_run``."""
    op.drop_index("ix_weekly_run_requested_at", table_name="weekly_run")
    op.drop_index("uq_weekly_run_one_running", table_name="weekly_run")
    op.drop_table("weekly_run")
