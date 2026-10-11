"""Data access for ``sourcing_run`` and ``candidate``. Queries only — no business rules, no commits.

**The repository flushes; the service commits** — T2's house convention. A method may write and
flush so defaults land and constraints fire here, but the transaction boundary is the service's.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
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
from app.sourcing.stages import PipelineStage, owns

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
    ) -> SourcingRun | None:
        """Close a run in one conditional write; ``None`` when it was no longer running.

        ``WHERE status = 'running'`` makes finishing a compare-and-set: of two finishes racing one
        run, the second updates nothing instead of overwriting the first's outcome. The finish time
        is the **database's** clock, like ``started_at`` — so "how long did it take" never mixes two
        clocks (T4 review, deferred to T5).
        """
        result = await self._session.scalars(
            update(SourcingRun)
            .where(SourcingRun.id == run.id, SourcingRun.status == RunStatus.running.value)
            .values(
                status=outcome.value,
                status_detail=detail,
                finished_at=func.now(),
                counts=dict(counts),
                cost=cost.model_dump(mode="json"),
            )
            .returning(SourcingRun),
            execution_options={"populate_existing": True},
        )
        finished = result.one_or_none()
        if finished is None:
            return None
        logger.info(
            "sourcing.repository.run_finished",
            run_id=str(run.id),
            status=outcome.value,
            counts=dict(counts),
            total_usd=str(cost.total_usd()),
        )
        return finished

    async def reap_stale_runs(self, vertical: str, older_than: timedelta) -> list[UUID]:
        """Mark this vertical's runs still ``running`` after ``older_than`` as failed.

        A run killed outright — the Mac sleeping mid-run, a ``kill -9`` — never reaches its own
        ``except``, and stays ``running`` forever. Failing it here is also what hands its batch back
        to the pool, because availability is read from the batching run's status.
        """
        hours = older_than.total_seconds() / 3600
        result = await self._session.scalars(
            update(SourcingRun)
            .where(
                SourcingRun.vertical == vertical,
                SourcingRun.status == RunStatus.running.value,
                SourcingRun.started_at < func.now() - older_than,
            )
            .values(
                status=RunStatus.failed.value,
                status_detail=f"abandoned: still running after {hours:g}h",
                finished_at=func.now(),
            )
            .returning(SourcingRun.id)
        )
        reaped = list(result.all())
        if reaped:
            logger.warning(
                "sourcing.repository.runs_reaped",
                vertical=vertical,
                run_ids=[str(run_id) for run_id in reaped],
            )
        return reaped

    async def upsert_candidate(
        self,
        run_id: UUID,
        fields: CandidateFields,
        *,
        stage: PipelineStage,
    ) -> Candidate:
        """Insert a candidate, or merge into the one this run already holds for the registry id.

        **Merge by field ownership** (``app/sourcing/stages.py``). On conflict the stored ``fields``
        becomes ``fill || existing || owned``:

        - ``owned``: incoming fields that ``stage`` owns. They go last, so they win, and a stage
          may overwrite its own fact.
        - ``existing``: what is stored now. It beats every field ``stage`` does not own, so a
          citation another stage wrote is never replaced.
        - ``fill``: incoming fields ``stage`` does not own. They go first, so they land only
          where ``existing`` has no key, which fills an empty field and nothing else.

        A field the incoming write does not carry keeps its citation whoever owns it. That works
        only because absent fields are dumped as absent keys (``CandidateFields.to_stored``);
        dumped as JSON ``null`` they would erase it. The same input twice therefore leaves the same
        row with the same content.

        ``stage`` is required and keyword-only, so a caller cannot forget to say who is writing.

        ``populate_existing`` matters on the second call: without it the session's identity map
        hands back the object it already holds for that primary key, stale, instead of the row the
        database just wrote.
        """
        # Re-validated here, at the write: `model_copy(update=...)` skips validators, so a model can
        # reach this point carrying what `CandidateFields` refuses (Places content, D13). Stored,
        # such a row would also fail to load and take the whole run's reads down with it.
        fields = CandidateFields.model_validate(fields.model_dump(mode="json"))
        incoming = fields.to_stored()
        owned = {name: value for name, value in incoming.items() if owns(stage, name)}
        fill = {name: value for name, value in incoming.items() if not owns(stage, name)}

        inserting = insert(Candidate).values(
            run_id=run_id,
            registry_id=fields.registry_id.value,
            fields=incoming,
        )
        merged = literal(fill, JSONB).op("||")(Candidate.fields).op("||")(literal(owned, JSONB))
        upserting = inserting.on_conflict_do_update(
            constraint="uq_candidate_run_registry_id",
            set_={"fields": merged, "updated_at": func.now()},
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
            stage=stage.value,
            owned_fields=sorted(owned),
            fill_only_fields=sorted(fill),
            incoming_unprovenanced_fields=list(fields.unprovenanced_fields()),
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
