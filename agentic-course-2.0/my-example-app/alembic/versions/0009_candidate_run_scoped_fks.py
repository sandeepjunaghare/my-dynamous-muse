"""run-scoped candidate foreign keys — a row naming a candidate must name that candidate's run

``route_assignment`` and ``disqualification`` each carry a ``run_id`` and a ``candidate_id`` with
separate foreign keys, so a row could pair run A with run B's candidate. Both tables are read and
replaced by ``run_id``, so such a row would be missed by one run and acted on by another. This
replaces each single-column ``candidate_id`` key with ``(run_id, candidate_id) → candidate(run_id,
id)``, whose target is a new ``unique(run_id, id)`` on ``candidate`` (#20, from PR #19's review).

Deferred until Wave 5 merged, because T5, T7 and T8 all built on ``candidate`` in parallel.
Existing rows that break the rule make the upgrade fail, which is intended: there should be none.

Hand-written to match ``app/sourcing/models.py``, ``app/routing/models.py`` and
``app/qualification/models.py`` exactly, so ``alembic check`` stays clean.

Revision ID: 0009_candidate_run_scoped_fks
Revises: 0008_qualification
Create Date: 2026-10-11

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_candidate_run_scoped_fks"
down_revision: str | None = "0008_qualification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, the Postgres-named key it replaces, the run-scoped key that replaces it)
_TABLES = (
    ("route_assignment", "route_assignment_candidate_id_fkey", "fk_route_assignment_run_candidate"),
    ("disqualification", "disqualification_candidate_id_fkey", "fk_disqualification_run_candidate"),
)


def upgrade() -> None:
    op.create_unique_constraint("uq_candidate_run_id_id", "candidate", ["run_id", "id"])
    for table, old_key, new_key in _TABLES:
        op.drop_constraint(old_key, table, type_="foreignkey")
        op.create_foreign_key(
            new_key, table, "candidate", ["run_id", "candidate_id"], ["run_id", "id"]
        )


def downgrade() -> None:
    for table, old_key, new_key in _TABLES:
        op.drop_constraint(new_key, table, type_="foreignkey")
        op.create_foreign_key(old_key, table, "candidate", ["candidate_id"], ["id"])
    op.drop_constraint("uq_candidate_run_id_id", "candidate", type_="unique")
