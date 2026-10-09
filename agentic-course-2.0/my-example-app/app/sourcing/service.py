"""The sourcing run lifecycle: open a run under an ACTIVE manifest, write candidates, close it.

```
start_run ──▶ RUNNING ──finish_run──▶ COMPLETED | DEGRADED | FAILED
                 │
         record_candidates (idempotent, any number of times)
```

The service owns the transaction; the repository below it flushes and never commits. No pipeline
lives here — T5 builds the runner that calls these three methods in order.
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost
from app.core.logging import get_logger
from app.manifests.service import ManifestService
from app.sourcing.exceptions import SourcingRunNotFoundError, SourcingRunNotRunningError
from app.sourcing.models import SourcingRun
from app.sourcing.repository import SourcingRepository
from app.sourcing.schemas import (
    CandidateFields,
    CandidateResponse,
    RunCostSummary,
    RunOutcome,
    RunStatus,
    RunTotals,
    SourcingBrief,
    SourcingRunResponse,
)

logger = get_logger(__name__)


class SourcingService:
    """The sourcing slice's business rules."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = SourcingRepository(session)

    async def start_run(self, brief: SourcingBrief) -> SourcingRunResponse:
        """Open a run for a brief, under the vertical's ACTIVE manifest.

        Refuses — via the manifests slice's own ``ActiveManifestNotFoundError`` — when the vertical
        has only drafts. A run sourced against a proposal nobody accepted would hit sources whose
        terms nobody decided, and would look like an empty registry rather than a missing decision.
        The manifest is *read*, never written: sourcing depends on manifests, not the reverse.
        """
        manifest = await ManifestService(self._session).get_active(brief.vertical)
        run = await self._repository.create_run(brief, manifest.id)
        await self._session.commit()

        logger.info(
            "sourcing.service.run_started",
            run_id=str(run.id),
            manifest_id=str(manifest.id),
            manifest_version=manifest.version,
            vertical=brief.vertical,
            geography=brief.geography,
        )
        return SourcingRunResponse.model_validate(run)

    async def get_run(self, run_id: UUID) -> SourcingRunResponse:
        """Return one run by id, whatever its status."""
        return SourcingRunResponse.model_validate(await self._require_run(run_id))

    async def record_candidates(
        self,
        run_id: UUID,
        candidates: Sequence[CandidateFields],
    ) -> list[CandidateResponse]:
        """Upsert candidates into a running run, keyed on ``(run, registry id)``.

        Idempotent: recording the same candidates twice leaves the same rows with the same content.
        A candidate with uncited fields is stored as readily as a fully cited one — the workbench
        holds it; ``is_promotable`` is what keeps it out of HubSpot.
        """
        run = await self._require_running(run_id, action="record_candidates")

        recorded = [
            await self._repository.upsert_candidate(run.id, fields) for fields in candidates
        ]
        await self._session.commit()

        logger.info(
            "sourcing.service.candidates_recorded",
            run_id=str(run_id),
            count=len(recorded),
        )
        return [CandidateResponse.model_validate(candidate) for candidate in recorded]

    async def list_candidates(self, run_id: UUID) -> list[CandidateResponse]:
        """Every candidate of a run, ordered by registry id."""
        await self._require_run(run_id)
        candidates = await self._repository.list_candidates(run_id)
        return [CandidateResponse.model_validate(candidate) for candidate in candidates]

    async def finish_run(
        self,
        run_id: UUID,
        outcome: RunOutcome,
        *,
        counts: Mapping[str, int],
        cost: RunCost,
        detail: str | None = None,
    ) -> SourcingRunResponse:
        """Close a running run, persisting its stage counts and what it spent.

        ``cost`` is the run's in-memory :class:`~app.core.cost.RunCost`; its per-kind totals are
        snapshotted here, which is where ``core/cost.py`` said they would land. Counts are validated
        (slug keys, non-negative values) before anything is written.
        """
        run = await self._require_running(run_id, action="finish_run")
        totals = RunTotals(counts=dict(counts), cost=RunCostSummary.from_run_cost(cost))

        await self._repository.mark_finished(run, outcome, totals.counts, totals.cost, detail)
        await self._session.commit()

        logger.info(
            "sourcing.service.run_finished",
            run_id=str(run_id),
            status=outcome.value,
            counts=totals.counts,
            total_usd=str(totals.cost.total_usd()),
        )
        return SourcingRunResponse.model_validate(run)

    async def _require_run(self, run_id: UUID) -> SourcingRun:
        """Return the run, or raise :class:`SourcingRunNotFoundError`."""
        run = await self._repository.get_run(run_id)
        if run is None:
            raise SourcingRunNotFoundError(f"no sourcing run with id {run_id}", run_id=str(run_id))
        return run

    async def _require_running(self, run_id: UUID, *, action: str) -> SourcingRun:
        """Return the run if it is still open, or refuse the write."""
        run = await self._require_run(run_id)
        if run.status != RunStatus.running.value:
            logger.warning(
                "sourcing.service.write_refused",
                run_id=str(run_id),
                action=action,
                status=run.status,
            )
            raise SourcingRunNotRunningError(
                f"sourcing run {run_id} is {run.status}; {action} needs a running run",
                run_id=str(run_id),
                status=run.status,
            )
        return run
