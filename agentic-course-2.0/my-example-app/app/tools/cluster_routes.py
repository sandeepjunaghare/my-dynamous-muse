"""Stage 5 of 5: ``cluster_routes`` — a run's verified addresses grouped into drive routes (T8).

Deterministic geometry, no model: the same candidates give the same routes on every run. Coordinates
come from the US Census Geocoder, never from Google Places (D13). The stage owns no candidate field;
it writes only ``route_assignment``. The work is ``app/routing/``; this module is how the runner
finds it.
"""

from app.routing.service import RoutingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult


class _ClusterRoutes:
    """The ``cluster_routes`` stage."""

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.cluster_routes

    async def run(self, context: StageContext) -> StageResult:
        """Cluster the run's candidates and report the routing counts.

        A geocode that failed (unreachable, unreadable) leaves a door off the routes, so the stage
        finishes degraded. Every one failing raises in the service instead. Unmatched and ambiguous
        addresses are answers, not failures: counted, never degraded.
        """
        result = await RoutingService(context.session).cluster_run(context.run_id)
        failed = result.counts["routing_geocode_failed"]
        reason = None
        if failed:
            eligible = result.counts["routing_eligible"]
            reason = f"{failed} of {eligible} geocode(s) failed; those candidates are on no route"
        return StageResult(counts=result.counts, degraded_reason=reason)


STAGE = _ClusterRoutes()
