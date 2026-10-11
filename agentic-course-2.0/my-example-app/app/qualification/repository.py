"""Data access for ``disqualification``. Queries only — no business rules, no commits.

**The repository flushes; the service commits** — T2's house convention.
"""

from collections.abc import Collection, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.manifests.schemas import RuleKind
from app.qualification.models import Disqualification
from app.shared.provenance import RetrievalMethod

logger = get_logger(__name__)


class QualificationRepository:
    """Reads and writes disqualifications through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        candidate_id: UUID,
        run_id: UUID,
        manifest_id: UUID,
        vertical: str,
        registry_id: str,
        rule_id: str,
        rule_kind: RuleKind,
        source_url: str,
        retrieval_method: RetrievalMethod,
        evidence: str | None,
    ) -> Disqualification:
        """Record that ``rule_id`` fired on a candidate — once, however often it is recorded.

        Idempotent on ``(candidate, rule)``: a retried stage keeps the first citation rather than
        replacing it, because the first is when the rule actually fired.
        """
        await self._session.execute(
            insert(Disqualification)
            .values(
                candidate_id=candidate_id,
                run_id=run_id,
                manifest_id=manifest_id,
                vertical=vertical,
                registry_id=registry_id,
                rule_id=rule_id,
                rule_kind=rule_kind.value,
                source_url=source_url,
                retrieval_method=retrieval_method.value,
                evidence=evidence,
            )
            .on_conflict_do_nothing(constraint="uq_disqualification_candidate_rule")
        )
        result = await self._session.execute(
            select(Disqualification).where(
                Disqualification.candidate_id == candidate_id,
                Disqualification.rule_id == rule_id,
            ),
            execution_options={"populate_existing": True},
        )
        row = result.scalar_one()
        logger.info(
            "qualification.repository.disqualification_recorded",
            disqualification_id=str(row.id),
            candidate_id=str(candidate_id),
            vertical=vertical,
            registry_id=registry_id,
            rule_id=rule_id,
            rule_kind=rule_kind.value,
        )
        return row

    async def list_for_candidate(self, candidate_id: UUID) -> Sequence[Disqualification]:
        """Every rule that fired on one candidate, in the order it fired."""
        result = await self._session.execute(
            select(Disqualification)
            .where(Disqualification.candidate_id == candidate_id)
            .order_by(Disqualification.disqualified_at.asc(), Disqualification.rule_id.asc())
        )
        return result.scalars().all()

    async def disqualified_candidate_ids(self, run_id: UUID) -> frozenset[UUID]:
        """The candidates of one run that any rule has already removed."""
        result = await self._session.execute(
            select(Disqualification.candidate_id).where(Disqualification.run_id == run_id)
        )
        return frozenset(result.scalars().all())

    async def suppressed_registry_ids(
        self, vertical: str, registry_ids: Collection[str] | None = None
    ) -> frozenset[str]:
        """Registry ids this vertical has disqualified in any run — never to be sourced again.

        ``registry_ids`` narrows the lookup to a batch; ``None`` returns every suppressed id.
        """
        query = select(Disqualification.registry_id).where(Disqualification.vertical == vertical)
        if registry_ids is not None:
            if not registry_ids:
                return frozenset()
            query = query.where(Disqualification.registry_id.in_(list(registry_ids)))
        result = await self._session.execute(query.distinct())
        return frozenset(result.scalars().all())
