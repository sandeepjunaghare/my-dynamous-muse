"""sourcing_pool — the backlog a vertical's runs work through, a batch at a time

D12: the registry's free filters run over the whole pool and persist it (4,449 for freight v3), and
each weekly run takes a fixed batch, best first, through the paid stages. ``candidate`` is keyed per
run, so it cannot remember "already batched" across runs; this table does. A record is available
when the latest refresh saw it and it was never batched, or was batched by a run that failed.

Hand-written to match ``app/sourcing/models.py`` exactly, so ``alembic check`` stays clean. The
registry-id CHECK is textually identical to ``candidate``'s (``REGISTRY_ID_IS_CITED``).

Pre-numbered for Wave 5 (T5 ``0006``, T8 ``0007``, T7 ``0008``); each starts from ``0005`` and is
re-chained at merge, T5 first.

Revision ID: 0006_sourcing_pool
Revises: 0005_cadence
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_sourcing_pool"
down_revision: str | None = "0005_cadence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``sourcing_pool``."""
    op.create_table(
        "sourcing_pool",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("vertical", sa.String(length=64), nullable=False),
        sa.Column("registry_id", sa.String(length=128), nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("currency_date", sa.Date(), nullable=True),
        sa.Column("has_principal", sa.Boolean(), nullable=False),
        sa.Column(
            "first_seen_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column(
            "batched_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=True,
        ),
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
        sa.UniqueConstraint(
            "vertical", "registry_id", name="uq_sourcing_pool_vertical_registry_id"
        ),
        # Null-safe, and textually identical to ``REGISTRY_ID_IS_CITED`` in the model.
        sa.CheckConstraint(
            "coalesce("
            "jsonb_typeof(fields -> 'registry_id' -> 'value') = 'string'"
            " and registry_id = (fields -> 'registry_id' ->> 'value')"
            " and jsonb_typeof(fields -> 'registry_id' -> 'source_url') = 'string'"
            " and btrim(fields -> 'registry_id' ->> 'source_url') <> ''"
            " and jsonb_typeof(fields -> 'registry_id' -> 'retrieved_at') = 'string'"
            " and jsonb_typeof(fields -> 'registry_id' -> 'retrieval_method') = 'string'"
            ", false)",
            name="ck_sourcing_pool_registry_id_is_cited",
        ),
    )
    op.create_index(
        "ix_sourcing_pool_vertical_last_seen",
        "sourcing_pool",
        ["vertical", "last_seen_run_id"],
    )


def downgrade() -> None:
    """Drop ``sourcing_pool``."""
    op.drop_index("ix_sourcing_pool_vertical_last_seen", table_name="sourcing_pool")
    op.drop_table("sourcing_pool")
