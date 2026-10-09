"""cadence_state — the three-touch schedule, and nothing else

Spike 3: the portal is Sales Hub Starter, with no sequences, so the cadence state machine is ours
(D4). This table holds **the schedule only** — which touch is due, which cycle, live or parked.
Outcomes stay in HubSpot and are re-read on every sync.

Hand-written, matching 0002: the check constraints are the point, and the one that says a live row
always has an open task is what makes "nobody is silently dropped" a database guarantee.

**Revision chain.** Written against ``0003_seed_freight_and_fire`` because T4's ``0004_sourcing``
was being built in parallel. At merge, rebase ``down_revision`` onto whichever head lands first so
the chain stays linear (``tests/test_structure.py`` enforces a single head).

Revision ID: 0005_cadence
Revises: 0003_seed_freight_and_fire
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_cadence"
down_revision: str | None = "0003_seed_freight_and_fire"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``cadence_state``, its constraints and the overdue lookup index."""
    op.create_table(
        "cadence_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("hubspot_contact_id", sa.String(length=32), nullable=False),
        sa.Column("hubspot_company_id", sa.String(length=32), nullable=True),
        sa.Column("hubspot_owner_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cycle", sa.SmallInteger(), nullable=False),
        sa.Column("touch", sa.String(length=16), nullable=False),
        sa.Column("hubspot_task_id", sa.String(length=32), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor_ref", sa.String(length=64), nullable=True),
        sa.Column(
            "enrolled_at",
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
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("hubspot_contact_id", name="uq_cadence_state_contact"),
        sa.CheckConstraint("status in ('live', 'parked')", name="ck_cadence_state_status"),
        sa.CheckConstraint(
            "touch in ('call', 'voicemail', 'email')", name="ck_cadence_state_touch"
        ),
        sa.CheckConstraint("cycle between 1 and 3", name="ck_cadence_state_cycle"),
        sa.CheckConstraint(
            "status = 'parked' or (hubspot_task_id is not null and due_at is not null)",
            name="ck_cadence_state_live_has_task",
        ),
    )
    op.create_index("ix_cadence_state_status_due_at", "cadence_state", ["status", "due_at"])


def downgrade() -> None:
    """Drop the index, then the table."""
    op.drop_index("ix_cadence_state_status_due_at", table_name="cadence_state")
    op.drop_table("cadence_state")
