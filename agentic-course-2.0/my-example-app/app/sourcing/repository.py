"""Data access for ``sourcing_run`` and ``candidate``. Queries only — no business rules, no commits.

**The repository flushes; the service commits** — T2's house convention. A method may write and
flush so defaults land and constraints fire here, but the transaction boundary is the service's.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.sourcing.models import Candidate, SourcingRun
from app.sourcing.schemas import (
    CandidateFields,
    RunCostSummary,
    RunOutcome,
    RunStatus,
    SourcingBrief,
)

logger = get_logger(__name__)


class SourcingRepository:
    """Reads and writes runs and candidates through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, brief: SourcingBrief, manifest_id: UUID) -> SourcingRun:
        """Insert a new run in ``running`` state, recording the brief and its manifest version."""
        run = SourcingRun(
            manifest_id=manifest_id,
            vertical=brief.vertical,
            geography=brief.geography,
            icp_band=brief.icp_band.model_dump(mode="json"),
            status=RunStatus.running.value,
        )
        self._session.add(run)
        await self._session.flush()
        # `started_at`, `counts` and `cost` are server defaults; load them now, inside the async
        # context, rather than lazily on first attribute access (an error under asyncio).
        await self._session.refresh(run)
        logger.info(
            "sourcing.repository.run_created",
            run_id=str(run.id),
            manifest_id=str(manifest_id),
            vertical=brief.vertical,
            geography=brief.geography,
        )
        return run

    async def get_run(self, run_id: UUID) -> SourcingRun | None:
        """Return one run by primary key, whatever its status."""
        result = await self._session.execute(select(SourcingRun).where(SourcingRun.id == run_id))
        return result.scalar_one_or_none()

    async def mark_finished(
        self,
        run: SourcingRun,
        outcome: RunOutcome,
        counts: Mapping[str, int],
        cost: RunCostSummary,
        detail: str | None,
    ) -> None:
        """Close a run: its outcome, finish time, stage counts and cost, in one write."""
        run.status = outcome.value
        run.status_detail = detail
        run.finished_at = datetime.now(UTC)
        run.counts = dict(counts)
        run.cost = cost.model_dump(mode="json")
        await self._session.flush()
        logger.info(
            "sourcing.repository.run_finished",
            run_id=str(run.id),
            status=outcome.value,
            counts=dict(counts),
            total_usd=str(cost.total_usd()),
        )

    async def upsert_candidate(self, run_id: UUID, fields: CandidateFields) -> Candidate:
        """Insert a candidate, or merge into the one this run already holds for the registry id.

        **Merge, not replace.** The stored ``fields`` is ``existing || incoming``: a field the
        incoming write carries overwrites the stored one, and a field it does not carry keeps its
        citation.
        That works only because absent fields are dumped as absent keys (``exclude_none=True``) —
        dumped as JSON ``null`` they would erase every citation the incoming write did not repeat.
        The same input twice therefore leaves the same row with the same content.

        ``populate_existing`` matters on the second call: without it the session's identity map
        hands back the object it already holds for that primary key, stale, instead of the row the
        database just wrote.
        """
        inserting = insert(Candidate).values(
            run_id=run_id,
            registry_id=fields.registry_id.value,
            fields=fields.model_dump(mode="json", exclude_none=True),
        )
        upserting = inserting.on_conflict_do_update(
            constraint="uq_candidate_run_registry_id",
            set_={
                "fields": Candidate.fields.op("||")(inserting.excluded["fields"]),
                "updated_at": func.now(),
            },
        ).returning(Candidate)

        result = await self._session.scalars(
            upserting,
            execution_options={"populate_existing": True},
        )
        candidate = result.one()
        logger.info(
            "sourcing.repository.candidate_upserted",
            run_id=str(run_id),
            candidate_id=str(candidate.id),
            registry_id=candidate.registry_id,
            unprovenanced_fields=list(fields.unprovenanced_fields()),
        )
        return candidate

    async def get_candidate(self, candidate_id: UUID) -> Candidate | None:
        """Return one candidate by primary key."""
        result = await self._session.execute(select(Candidate).where(Candidate.id == candidate_id))
        return result.scalar_one_or_none()

    async def list_candidates(self, run_id: UUID) -> Sequence[Candidate]:
        """Every candidate of one run, ordered by registry id.

        The order is fixed, not caller-supplied: T5's determinism test asserts the same input gives
        the same candidates *in the same order*, and insertion order is not something Postgres
        promises to return.
        """
        result = await self._session.execute(
            select(Candidate)
            .where(Candidate.run_id == run_id)
            .order_by(Candidate.registry_id.asc(), Candidate.id.asc())
        )
        return result.scalars().all()
