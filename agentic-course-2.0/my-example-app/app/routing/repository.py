"""Data access for ``route_assignment``. Queries only — no business rules, no commits.

**The repository flushes; the service commits** — the house convention from T2.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.routing.models import RouteAssignment
from app.routing.schemas import GeoPoint, RouteCluster
from app.shared.provenance import ProvenancedValue
from app.sourcing.models import Candidate

logger = get_logger(__name__)


class RoutingRepository:
    """Reads and writes route assignments through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_run_assignments(
        self,
        run_id: UUID,
        clusters: Sequence[RouteCluster],
        points: Mapping[UUID, ProvenancedValue[GeoPoint]],
    ) -> list[RouteAssignment]:
        """Replace every assignment of ``run_id`` with one row per member of ``clusters``.

        Delete-then-insert, not an upsert: a candidate that dropped out of routing since the last
        pass (its geocode now misses, say) must lose its row, and an upsert would leave it behind.
        ``points`` holds each member's cited point, keyed by candidate id.
        """
        await self._session.execute(delete(RouteAssignment).where(RouteAssignment.run_id == run_id))
        rows = [
            RouteAssignment(
                run_id=run_id,
                candidate_id=member.candidate_id,
                cluster_index=cluster.index,
                cluster_label=cluster.label,
                point=points[member.candidate_id].model_dump(mode="json"),
            )
            for cluster in clusters
            for member in cluster.members
        ]
        self._session.add_all(rows)
        await self._session.flush()
        logger.info(
            "routing.repository.assignments_replaced",
            run_id=str(run_id),
            count=len(rows),
            clusters=len(clusters),
        )
        return rows

    async def list_run_assignments(self, run_id: UUID) -> Sequence[RouteAssignment]:
        """Every assignment of one run, by route and then by the candidate's registry id.

        The order is fixed, not caller-supplied, for the same reason ``list_candidates`` gives:
        the stability tests compare whole listings.
        """
        result = await self._session.execute(
            select(RouteAssignment)
            .join(Candidate, Candidate.id == RouteAssignment.candidate_id)
            .where(RouteAssignment.run_id == run_id)
            .order_by(RouteAssignment.cluster_index.asc(), Candidate.registry_id.asc())
            .execution_options(populate_existing=True)
        )
        return result.scalars().all()
