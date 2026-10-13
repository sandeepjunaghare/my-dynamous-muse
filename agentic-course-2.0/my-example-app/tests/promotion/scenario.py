"""Shared setup for promotion's database-tier suites: a finished run of chosen candidates.

Everything is written through the slices' own services and repositories inside the test's
rolled-back transaction. Route assignments are inserted as rows, because clustering is T8's and
its geocoder is not what these suites test.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.manifests.schemas import ManifestResponse, RuleKind
from app.qualification.repository import QualificationRepository
from app.routing.models import RouteAssignment
from app.routing.schemas import GeoPoint
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import (
    CandidateFields,
    CandidateResponse,
    RunOutcome,
    SourcingRunResponse,
)
from app.sourcing.service import SourcingService
from tests.qualification.builders import a_body_with, an_active_manifest_with
from tests.sourcing.builders import REGISTRY, a_brief, sourced

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"


@dataclass(frozen=True)
class Scenario:
    manifest: ManifestResponse
    run: SourcingRunResponse
    candidates: list[CandidateResponse]

    @property
    def vertical(self) -> str:
        return self.manifest.vertical

    def candidate(self, registry_id: str) -> CandidateResponse:
        return next(c for c in self.candidates if c.registry_id == registry_id)


async def a_finished_run(
    session: AsyncSession,
    fields: Sequence[CandidateFields],
    *,
    manifest: ManifestResponse | None = None,
    outcome: RunOutcome | None = RunOutcome.completed,
) -> Scenario:
    """A run under ``manifest`` (a fresh vertical by default) holding ``fields``, then finished.

    ``outcome=None`` leaves the run ``running``.
    """
    manifest = manifest or await an_active_manifest_with(session, a_body_with())
    service = SourcingService(session)
    run = await service.start_run(a_brief(manifest.vertical))
    candidates = await service.record_candidates(run.id, list(fields), stage=REGISTRY)
    if outcome is not None:
        run = await service.finish_run(run.id, outcome, counts={}, cost=RunCost())
    return Scenario(manifest=manifest, run=run, candidates=candidates)


async def disqualify(session: AsyncSession, scenario: Scenario, registry_id: str) -> None:
    """Record a fired rule for one candidate: its ``(vertical, registry_id)`` is suppressed."""
    candidate = scenario.candidate(registry_id)
    await QualificationRepository(session).record(
        candidate_id=candidate.id,
        run_id=scenario.run.id,
        manifest_id=scenario.manifest.id,
        vertical=scenario.vertical,
        registry_id=registry_id,
        rule_id="no_broker_entity_type",
        rule_kind=RuleKind.predicate,
        source_url="https://data.transportation.gov/d/az4n-8mr2",
        retrieval_method=RetrievalMethod.bulk_file,
        evidence="carship C",
    )
    await session.commit()


async def route(session: AsyncSession, scenario: Scenario, registry_id: str, label: str) -> None:
    """Put one candidate on a route, as ``cluster_routes`` would have."""
    point = sourced(GeoPoint(latitude=32.75, longitude=-97.08), GEOCODER_URL)
    session.add(
        RouteAssignment(
            run_id=scenario.run.id,
            candidate_id=scenario.candidate(registry_id).id,
            cluster_index=0,
            cluster_label=label,
            point=point.model_dump(mode="json"),
        )
    )
    await session.commit()
