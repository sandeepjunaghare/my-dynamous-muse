"""Cluster one run's candidates into drive routes.

```
candidates of the run ──has_verified_address?──no──▶ counted, never geocoded
                               │ yes
                               ▼
                 Census Geocoder (one address at a time)
          matched │      unmatched · ambiguous · failed ──▶ counted, left out
                  ▼
        cluster_points(max_doors) ──▶ route_assignment (the run's rows replaced)
```

**Eligibility is a verified address, and only that** (decided 2026-10-10). Leaving out candidates
T7 disqualified is T9/T10's to add once both exist; this slice does not read ``qualification``.

**One failed geocode leaves one candidate out; every geocode failing is an error.** A single
timeout should not cost the week's routes, and re-running the stage retries it, because the stage
replaces its rows. But if every address fails, unreachable or unreadable alike, an empty result
would look like a quiet week and erase the run's earlier routes. So the error is raised for the
runner to see, before anything is written.

Routing writes only its own table. It never writes ``candidate``: ``cluster_routes`` owns no
candidate field (``app/sourcing/stages.py``).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.routing.clustering import cluster_points
from app.routing.exceptions import (
    GeocoderResponseShapeError,
    GeocoderUnavailableError,
    RoutingError,
)
from app.routing.geocoder import CensusGeocoder
from app.routing.repository import RoutingRepository
from app.routing.schemas import (
    GeocodeMatchStatus,
    GeoPoint,
    RouteAssignmentResponse,
    RoutePoint,
    RoutingResult,
)
from app.shared.provenance import ProvenancedValue
from app.sourcing.schemas import CandidateResponse, PostalAddress
from app.sourcing.service import SourcingService

logger = get_logger(__name__)


@dataclass
class _Misses:
    """Tallies of the eligible candidates that geocoding left out, and why."""

    unmatched: int = 0
    ambiguous: int = 0
    unavailable: int = 0
    unreadable: int = 0
    last_error: RoutingError | None = None
    """The last geocoder failure, re-raised when every eligible geocode failed."""

    @property
    def failed(self) -> int:
        """Geocodes that failed outright, for either reason."""
        return self.unavailable + self.unreadable


class RoutingService:
    """The routing slice's business rules."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        geocoder: CensusGeocoder | None = None,
        max_doors: int | None = None,
    ) -> None:
        self._session = session
        self._repository = RoutingRepository(session)
        self._geocoder = geocoder
        self._max_doors = max_doors if max_doors is not None else get_settings().route_max_doors

    async def cluster_run(self, run_id: UUID) -> RoutingResult:
        """Geocode the run's verified addresses, cluster them, and replace the run's assignments.

        Raises ``SourcingRunNotFoundError`` for an unknown run. When every eligible geocode failed,
        for any mix of reasons, re-raises the last ``GeocoderUnavailableError`` or
        ``GeocoderResponseShapeError`` *before* touching the run's assignments, so a failed re-run
        leaves the earlier routes in place (PR #19 review, M1).
        """
        candidates = await SourcingService(self._session).list_candidates(run_id)
        # Pair each eligible candidate with its address here, so nothing downstream has to re-check
        # an address that ``has_verified_address`` already guaranteed (PR #19 review, L1).
        eligible = [
            (candidate, address.value)
            for candidate in candidates
            if candidate.fields.has_verified_address()
            and (address := candidate.fields.address) is not None
        ]

        if eligible:
            if self._geocoder is None:
                async with CensusGeocoder() as geocoder:
                    located, misses = await self._geocode(eligible, geocoder)
            else:
                located, misses = await self._geocode(eligible, self._geocoder)
        else:
            located, misses = [], _Misses()

        if misses.last_error is not None and misses.failed == len(eligible):
            raise misses.last_error

        points = [point for point, _ in located]
        clusters = cluster_points(points, max_doors=self._max_doors)
        await self._repository.replace_run_assignments(
            run_id,
            clusters,
            {point.candidate_id: cited for point, cited in located},
        )
        await self._session.commit()

        counts = {
            "routing_eligible": len(eligible),
            "routing_unverified_address": len(candidates) - len(eligible),
            "routing_geocode_unmatched": misses.unmatched,
            "routing_geocode_ambiguous": misses.ambiguous,
            "routing_geocode_failed": misses.failed,
            "routing_clustered": len(points),
            "routing_clusters": len(clusters),
        }
        logger.info("routing.service.run_clustered", run_id=str(run_id), **counts)
        return RoutingResult(counts=counts, clusters=clusters)

    async def list_assignments(self, run_id: UUID) -> list[RouteAssignmentResponse]:
        """Every route assignment of a run, by route and then registry id."""
        await SourcingService(self._session).get_run(run_id)
        rows = await self._repository.list_run_assignments(run_id)
        return [RouteAssignmentResponse.model_validate(row) for row in rows]

    async def _geocode(
        self,
        eligible: list[tuple[CandidateResponse, PostalAddress]],
        geocoder: CensusGeocoder,
    ) -> tuple[list[tuple[RoutePoint, ProvenancedValue[GeoPoint]]], _Misses]:
        """Geocode each eligible candidate in registry-id order, one at a time.

        Sequential on purpose: the Census Geocoder publishes no rate limit, a batch is ~150
        addresses, and a fixed order keeps the log readable and the calls repeatable.
        """
        located: list[tuple[RoutePoint, ProvenancedValue[GeoPoint]]] = []
        misses = _Misses()
        for candidate, address in eligible:
            try:
                outcome = await geocoder.geocode(address)
            except (GeocoderUnavailableError, GeocoderResponseShapeError) as exc:
                if isinstance(exc, GeocoderUnavailableError):
                    misses.unavailable += 1
                else:
                    misses.unreadable += 1
                misses.last_error = exc
                logger.warning(
                    "routing.service.geocode_failed",
                    registry_id=candidate.registry_id,
                    error=exc.code,
                )
                continue

            if outcome.status is GeocodeMatchStatus.unmatched:
                misses.unmatched += 1
            elif outcome.status is GeocodeMatchStatus.ambiguous:
                misses.ambiguous += 1
            if outcome.point is None:
                # Debug, not info: the run's counts already carry these (PR #19 review, L6).
                logger.debug(
                    "routing.service.geocode_missed",
                    registry_id=candidate.registry_id,
                    status=outcome.status.value,
                )
                continue

            located.append(
                (
                    RoutePoint(
                        candidate_id=candidate.id,
                        registry_id=candidate.registry_id,
                        point=outcome.point.value,
                        postal_code=address.postal_code,
                    ),
                    outcome.point,
                )
            )
        return located, misses
