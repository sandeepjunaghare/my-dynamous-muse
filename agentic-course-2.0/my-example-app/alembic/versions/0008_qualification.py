"""disqualification and manifest_dry_run — qualification's memory and the manifest quality gate

``disqualification`` records each rule that fired on a candidate, with its citation; suppression
reads it by ``(vertical, registry_id)`` so a rejected business is never re-sourced.
``manifest_dry_run`` records each ``lpe manifest dry-run``; ``activate`` refuses a manifest with none.

Pre-numbered for Wave 5 (T5 ``0006``, T8 ``0007``, T7 ``0008``), each forked from ``0005``.
**Re-chained at merge:** ``down_revision`` moves to T8's revision once T5 and T8 are on main.

Hand-written to match ``app/qualification/models.py`` and ``app/manifests/models.py`` exactly, so
``alembic check`` stays clean.

Revision ID: 0008_qualification
Revises: 0005_cadence
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_qualification"
down_revision: str | None = "0005_cadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``manifest_dry_run``, then ``disqualification``."""
    op.create_table(
        "manifest_dry_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "manifest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vertical_manifest.id"),
            nullable=False,
        ),
        sa.Column(
            "ran_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ran_by", sa.String(length=128), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_manifest_dry_run_manifest_id", "manifest_dry_run", ["manifest_id"])

    op.create_table(
        "disqualification",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidate.id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column(
            "manifest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vertical_manifest.id"),
            nullable=False,
        ),
        sa.Column("vertical", sa.String(length=64), nullable=False),
        sa.Column("registry_id", sa.String(length=128), nullable=False),
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("rule_kind", sa.String(length=16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("retrieval_method", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column(
            "disqualified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "candidate_id", "rule_id", name="uq_disqualification_candidate_rule"
        ),
        sa.CheckConstraint(
            "rule_kind in ('predicate', 'judgment')", name="ck_disqualification_rule_kind"
        ),
        sa.CheckConstraint("btrim(source_url) <> ''", name="ck_disqualification_source_url"),
    )
    op.create_index(
        "ix_disqualification_vertical_registry_id",
        "disqualification",
        ["vertical", "registry_id"],
    )


def downgrade() -> None:
    """Drop ``disqualification`` first, then ``manifest_dry_run``."""
    op.drop_index("ix_disqualification_vertical_registry_id", table_name="disqualification")
    op.drop_table("disqualification")
    op.drop_index("ix_manifest_dry_run_manifest_id", table_name="manifest_dry_run")
    op.drop_table("manifest_dry_run")
