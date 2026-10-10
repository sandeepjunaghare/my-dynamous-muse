"""Qualification's business rules: record what disqualified a candidate, and never source it again.

Two writers and one reader:

* :meth:`QualificationService.disqualify_by_predicates` — the free checks on a batch (QCMobile's
  ``allowToOperate``, the revocations join), recorded with the source's citation. Only the first
  rule to fire is recorded: one reason is enough to stop paying for a candidate.
* :meth:`QualificationService.record_judgment` — every judgment rule the ``classify_rollup`` node
  fired *and could cite*. An uncited verdict never reaches here (``admit_judgment`` drops it).
* :meth:`QualificationService.suppressed_registry_ids` — what the batch selection excludes.

A record no rule could judge is never disqualified; it is logged so a person can see it.
"""

from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.manifests.schemas import ManifestResponse, RuleKind
from app.qualification.repository import QualificationRepository
from app.qualification.rules import apply_predicates
from app.qualification.schemas import (
    AdmittedJudgment,
    DisqualificationResponse,
    RecordSet,
    RuleOutcome,
)
from app.shared.provenance import RetrievalMethod
from app.sourcing.schemas import CandidateResponse, SourcingRunResponse

logger = get_logger(__name__)


class QualificationService:
    """The qualification slice's business rules. Owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = QualificationRepository(session)

    async def disqualify_by_predicates(
        self,
        candidate: CandidateResponse,
        run: SourcingRunResponse,
        manifest: ManifestResponse,
        records: RecordSet,
        *,
        source_urls: Mapping[str, str] | None = None,
    ) -> RuleOutcome | None:
        """Apply the free predicates to one candidate's records; record the first that fires.

        ``source_urls`` maps a source name to the record-level URL the caller read (a QCMobile
        lookup URL); without one, the citation is the source's declared base URL. Returns the rule
        that fired, or ``None`` when the candidate survives.
        """
        result = apply_predicates(manifest.body, records)
        unjudged = result.not_evaluable()
        if unjudged:
            logger.info(
                "qualification.service.evaluation_incomplete",
                candidate_id=str(candidate.id),
                rules=[f"{o.rule_id}:{o.reason}" for o in unjudged],
            )
        fired = result.first_fired()
        if fired is None:
            return None

        source = next(s.value for s in manifest.body.sources if s.value.name == fired.source)
        url = (source_urls or {}).get(fired.source) or source.base_url
        await self._repository.record(
            candidate_id=candidate.id,
            run_id=run.id,
            manifest_id=manifest.id,
            vertical=run.vertical,
            registry_id=candidate.registry_id,
            rule_id=fired.rule_id,
            rule_kind=RuleKind.predicate,
            source_url=url,
            # `SourceKind` and `RetrievalMethod` are parallel by design: the method a source's
            # field was read by is the kind of source it is.
            retrieval_method=RetrievalMethod(source.kind.value),
            evidence=f"{fired.field}={fired.observed!r}",
        )
        await self._session.commit()
        logger.info(
            "qualification.service.candidate_disqualified",
            candidate_id=str(candidate.id),
            registry_id=candidate.registry_id,
            rule_id=fired.rule_id,
            rule_kind=RuleKind.predicate.value,
        )
        return fired

    async def record_judgment(
        self,
        candidate: CandidateResponse,
        run: SourcingRunResponse,
        manifest: ManifestResponse,
        admitted: AdmittedJudgment,
    ) -> list[DisqualificationResponse]:
        """Record every cited judgment rule the node fired on one candidate."""
        rows = [
            await self._repository.record(
                candidate_id=candidate.id,
                run_id=run.id,
                manifest_id=manifest.id,
                vertical=run.vertical,
                registry_id=candidate.registry_id,
                rule_id=rule.rule_id,
                rule_kind=RuleKind.judgment,
                source_url=rule.reason.source_url,
                retrieval_method=rule.reason.retrieval_method,
                evidence=rule.reason.value,
            )
            for rule in admitted.fired
        ]
        await self._session.commit()
        if rows:
            logger.info(
                "qualification.service.candidate_disqualified",
                candidate_id=str(candidate.id),
                registry_id=candidate.registry_id,
                rule_ids=[rule.rule_id for rule in admitted.fired],
                rule_kind=RuleKind.judgment.value,
            )
        return [DisqualificationResponse.model_validate(row) for row in rows]

    async def list_for_candidate(self, candidate_id: UUID) -> list[DisqualificationResponse]:
        rows = await self._repository.list_for_candidate(candidate_id)
        return [DisqualificationResponse.model_validate(row) for row in rows]

    async def disqualified_candidate_ids(self, run_id: UUID) -> frozenset[UUID]:
        return await self._repository.disqualified_candidate_ids(run_id)

    async def suppressed_registry_ids(
        self, vertical: str, registry_ids: Collection[str] | None = None
    ) -> frozenset[str]:
        """Registry ids this vertical already rejected, in any run, under any manifest version."""
        return await self._repository.suppressed_registry_ids(vertical, registry_ids)

    async def is_suppressed(self, vertical: str, registry_id: str) -> bool:
        return registry_id in await self.suppressed_registry_ids(vertical, [registry_id])
