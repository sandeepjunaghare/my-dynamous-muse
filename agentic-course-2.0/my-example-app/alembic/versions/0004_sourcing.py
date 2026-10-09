"""sourcing_run and candidate — the sourcing workbench

A run is one execution of a brief (vertical · ICP band · geography) under one manifest version, with
its timings, stage counts, cost and status. A candidate is one business that run sourced, every field
cited, keyed for idempotent upsert on ``(run_id, registry_id)``.

Hand-written to match ``app/sourcing/models.py`` exactly, so ``alembic check`` stays clean. Two
CHECKs carry invariants the service also relies on, enforced here so a slice nobody has written yet
cannot write around them: a run's finish time agrees with its status, and a candidate's key column
agrees with its cited registry id, and that citation is complete.

Revision ID: 0004_sourcing
Revises: 0003_seed_freight_and_fire
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_sourcing"
down_revision: str | None = "0003_seed_freight_and_fire"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``sourcing_run``, then ``candidate`` which references it."""
    op.create_table(
        "sourcing_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "manifest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vertical_manifest.id"),
            nullable=False,
        ),
        sa.Column("vertical", sa.String(length=64), nullable=False),
        sa.Column("geography", sa.String(length=64), nullable=False),
        sa.Column("icp_band", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cost",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('running', 'completed', 'degraded', 'failed')",
            name="ck_sourcing_run_status",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at is null)",
            name="ck_sourcing_run_finished_at_matches_status",
        ),
    )
    op.create_index("ix_sourcing_run_vertical", "sourcing_run", ["vertical"])

    op.create_table(
        "candidate",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column("registry_id", sa.String(length=128), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("run_id", "registry_id", name="uq_candidate_run_registry_id"),
        # Null-safe: a CHECK passes on NULL, so a missing or JSON-null citation must be `false`,
        # not NULL. Kept textually identical to ``app/sourcing/models.py``.
        sa.CheckConstraint(
            "coalesce("
            "jsonb_typeof(fields -> 'registry_id' -> 'value') = 'string'"
            " and registry_id = (fields -> 'registry_id' ->> 'value')"
            " and jsonb_typeof(fields -> 'registry_id' -> 'source_url') = 'string'"
            " and btrim(fields -> 'registry_id' ->> 'source_url') <> ''"
            " and jsonb_typeof(fields -> 'registry_id' -> 'retrieved_at') = 'string'"
            " and jsonb_typeof(fields -> 'registry_id' -> 'retrieval_method') = 'string'"
            ", false)",
            name="ck_candidate_registry_id_is_cited",
        ),
    )


def downgrade() -> None:
    """Drop ``candidate`` first — it references ``sourcing_run``."""
    op.drop_table("candidate")
    op.drop_index("ix_sourcing_run_vertical", table_name="sourcing_run")
    op.drop_table("sourcing_run")
