"""Builders for the routing suites: route points, the two-neighbourhood week of E4, and runs."""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.schemas import GeoPoint, RoutePoint
from app.sourcing.schemas import CandidateFields, PostalAddress
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from tests.sourcing.builders import a_brief, an_active_manifest, place_checked, sourced

LAKE_HIGHLANDS = (32.8839, -96.7164)
"""Around 75238, the hand-built list's "Route Cluster B"."""

OLYMPIC_DRIVE = (32.7590, -97.0890)
"""Around Olympic Dr in Arlington (76011), the hand-built list's "Route Cluster A"."""


def candidate_uuid(registry_id: str) -> UUID:
    """A stable candidate id per registry id, so two builds of one point compare equal."""
    return uuid5(NAMESPACE_URL, f"candidate:{registry_id}")


def a_point(
    registry_id: str,
    latitude: float,
    longitude: float,
    postal_code: str = "75238",
) -> RoutePoint:
    """One door at a given place."""
    return RoutePoint(
        candidate_id=candidate_uuid(registry_id),
        registry_id=registry_id,
        point=GeoPoint(latitude=latitude, longitude=longitude),
        postal_code=postal_code,
    )


def around(
    centre: tuple[float, float],
    prefix: str,
    count: int,
    postal_code: str,
) -> list[RoutePoint]:
    """``count`` doors within about a kilometre of ``centre``, on a small deterministic grid."""
    latitude, longitude = centre
    return [
        a_point(
            f"{prefix}{i:03d}",
            latitude + (i % 4) * 0.003,
            longitude + (i // 4) * 0.003,
            postal_code,
        )
        for i in range(count)
    ]


def two_neighbourhoods(per_side: int = 10) -> tuple[list[RoutePoint], list[RoutePoint]]:
    """The E4 week: ``per_side`` doors around Olympic Drive and as many around 75238."""
    return (
        around(OLYMPIC_DRIVE, "oly", per_side, "76011"),
        around(LAKE_HIGHLANDS, "lkh", per_side, "75238"),
    )


def a_verified_candidate(
    registry_id: str, street: str, postal_code: str = "75238"
) -> CandidateFields:
    """A candidate routing may cluster: a census address and a business check that found it."""
    return CandidateFields(
        registry_id=sourced(registry_id),
        legal_name=sourced(f"Acme Logistics {registry_id} LLC"),
        address=sourced(
            PostalAddress(street=street, city="Dallas", state="TX", postal_code=postal_code)
        ),
        business_check=place_checked(f"ChIJ-{registry_id}"),
    )


def street_for(registry_id: str) -> str:
    """A distinct street per candidate, which is how ``MockCensus`` tells requests apart."""
    return f"{registry_id} Commerce St"


async def a_running_run(session: AsyncSession) -> UUID:
    """Start a run under a freshly activated manifest and return its id."""
    _, vertical = await an_active_manifest(session)
    run = await SourcingService(session).start_run(a_brief(vertical))
    return run.id


async def record(
    session: AsyncSession, run_id: UUID, candidates: Sequence[CandidateFields]
) -> None:
    """Write candidates into a run the way the registry stage would."""
    await SourcingService(session).record_candidates(
        run_id, candidates, stage=PipelineStage.search_registry
    )
