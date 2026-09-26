"""baseline — no domain tables

Establishes the Alembic version table and nothing else, so migration tooling is proven end to end
before any schema exists. T2 adds ``vertical_manifest``; T4 adds the sourcing tables.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-26

"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Baseline: nothing to create."""


def downgrade() -> None:
    """Baseline: nothing to drop."""
