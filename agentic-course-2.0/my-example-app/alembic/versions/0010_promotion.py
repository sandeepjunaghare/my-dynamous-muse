"""promotion — the ledger from a qualified candidate to the founder's cadence (T9)

One row per ``(vertical, registry_id)`` ever promoted or attempted: a state machine
(``pending_create → pending_review → enrolling → enrolled``, terminal ``rejected``, ``duplicate``,
``withdrawn``) whose unique key is what makes a re-run promote nothing twice.

T6 merged without a migration, so ``0009`` is still main's head and this revision keeps its number.
Dev applied it on 2026-10-13, so it can no longer be renumbered for free.

Hand-written to match ``app/promotion/models.py`` exactly, so ``alembic check`` stays clean.

Revision ID: 0010_promotion
Revises: 0009_candidate_run_scoped_fks
Create Date: 2026-10-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_promotion"
down_revision: str | None = "0009_candidate_run_scoped_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``promotion`` and its indexes."""
    op.create_table(
        "promotion",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("vertical", sa.String(length=64), nullable=False),
        sa.Column("registry_id", sa.String(length=128), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sourcing_run.id"),
            nullable=False,
        ),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("company_key", sa.Text(), nullable=False),
        sa.Column("dedupe_phone", sa.String(length=10), nullable=True),
        sa.Column("dedupe_domain", sa.String(length=253), nullable=True),
        sa.Column("hubspot_company_id", sa.String(length=32), nullable=True),
        sa.Column("hubspot_contact_id", sa.String(length=32), nullable=True),
        sa.Column("matched_company_id", sa.String(length=32), nullable=True),
        sa.Column(
            "duplicate_of_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("promotion.id", name="fk_promotion_duplicate_of"),
            nullable=True,
        ),
        sa.Column("name_ambiguous", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id", "candidate_id"],
            ["candidate.run_id", "candidate.id"],
            name="fk_promotion_run_candidate",
        ),
        sa.UniqueConstraint("vertical", "registry_id", name="uq_promotion_vertical_registry_id"),
        sa.UniqueConstraint("hubspot_company_id", name="uq_promotion_hubspot_company_id"),
        sa.UniqueConstraint("hubspot_contact_id", name="uq_promotion_hubspot_contact_id"),
        sa.CheckConstraint(
            "status in ('pending_create', 'pending_review', 'enrolling', 'enrolled', 'rejected', "
            "'duplicate', 'withdrawn')",
            name="ck_promotion_status",
        ),
        sa.CheckConstraint("btrim(company_key) <> ''", name="ck_promotion_company_key"),
        sa.CheckConstraint(
            "status not in ('pending_review', 'enrolling', 'enrolled', 'rejected', 'withdrawn') "
            "or (hubspot_company_id is not null and hubspot_contact_id is not null)",
            name="ck_promotion_ids_match_status",
        ),
        sa.CheckConstraint(
            "(status = 'duplicate') = "
            "(matched_company_id is not null or duplicate_of_id is not null)",
            name="ck_promotion_duplicate_has_match",
        ),
        sa.CheckConstraint(
            "(status = 'enrolled') = (enrolled_at is not null)", name="ck_promotion_enrolled_at"
        ),
    )
    op.create_index("ix_promotion_status", "promotion", ["status"])
    op.create_index(
        "ix_promotion_vertical_dedupe_phone", "promotion", ["vertical", "dedupe_phone"]
    )
    op.create_index(
        "ix_promotion_vertical_dedupe_domain", "promotion", ["vertical", "dedupe_domain"]
    )


def downgrade() -> None:
    """Drop ``promotion``."""
    op.drop_index("ix_promotion_vertical_dedupe_domain", table_name="promotion")
    op.drop_index("ix_promotion_vertical_dedupe_phone", table_name="promotion")
    op.drop_index("ix_promotion_status", table_name="promotion")
    op.drop_table("promotion")
