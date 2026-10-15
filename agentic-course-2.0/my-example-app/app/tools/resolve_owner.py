"""Stage 4 of 5: ``resolve_owner`` — an owner for each survivor the census names nobody for (T6).

**Census first** (A2). ``search_registry`` already filled ``owner`` from the census officer where
``Principal`` accepts it, so most candidates arrive owned and cost nothing here. The judgment node
(``app/sourcing/owner/``) runs only for a candidate that:

* no rule removed — free predicates, and the judgment rules ``classify_rollup`` just fired;
* has a ``business_check`` — an unverified candidate cannot be promoted (OQ-5), so it is not paid
  for;
* still has no ``owner``.

What the node names is kept only through its gate (A10), cited ``llm_inference`` to the page it
read; anything else leaves ``owner`` absent, never guessed. **Retries do not pay twice**: an owner
already stored is kept. **A per-run call cap** (``max_owner_calls_per_run``) is a breaker against a
runaway loop: reaching it ends the stage **degraded**, never raising, so a paid batch is not handed
back (A6). One failed call or one refused write is counted and the batch goes on.
"""

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.qualification.service import QualificationService
from app.shared.provenance import RetrievalMethod
from app.sourcing.exceptions import OwnerJudgmentError
from app.sourcing.owner import judgment
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult

logger = get_logger(__name__)

COUNT_KEYS = (
    "owner_from_registry",
    "owner_kept",
    "owner_resolved",
    "owner_absent",
    "owner_failed",
    "owner_write_refused",
    "owner_skipped_disqualified",
    "owner_unverified_skipped",
    "owner_capped",
    "owner_calls",
)
"""Every count the stage reports, zero included. ``owner_``-prefixed, so merging them into
``sourcing_run.counts`` never collides with another stage's."""


class _ResolveOwner:
    """The ``resolve_owner`` stage."""

    stage = PipelineStage.resolve_owner

    async def run(self, context: StageContext) -> StageResult:
        """Resolve the owner of each eligible survivor (see the module doc)."""
        cap = get_settings().max_owner_calls_per_run
        sourcing = SourcingService(context.session)
        run = await sourcing.get_run(context.run_id)
        removed = await QualificationService(context.session).disqualified_candidate_ids(run.id)
        counts: dict[str, int] = dict.fromkeys(COUNT_KEYS, 0)

        for candidate in await sourcing.list_candidates(run.id):
            fields = candidate.fields
            if candidate.id in removed:
                counts["owner_skipped_disqualified"] += 1
                continue
            if fields.owner is not None:
                from_registry = fields.owner.retrieval_method is RetrievalMethod.bulk_file
                counts["owner_from_registry" if from_registry else "owner_kept"] += 1
                continue
            if fields.business_check is None:
                counts["owner_unverified_skipped"] += 1
                continue
            if counts["owner_calls"] >= cap:
                counts["owner_capped"] += 1
                continue

            counts["owner_calls"] += 1
            try:
                resolved = await judgment.run_owner_judgment(fields, cost=context.cost)
            except OwnerJudgmentError as exc:
                counts["owner_failed"] += 1
                logger.warning(
                    "tools.resolve_owner.candidate_failed",
                    run_id=str(run.id),
                    candidate_id=str(candidate.id),
                    reason=exc.reason,
                )
                continue

            admitted = judgment.admit_owner(resolved.answer, reads=resolved.reads)
            if admitted.owner is None:
                counts["owner_absent"] += 1
                continue
            try:
                await sourcing.record_candidates(
                    run.id,
                    [fields.model_copy(update={"owner": admitted.owner})],
                    stage=PipelineStage.resolve_owner,
                )
            except ValidationError as exc:
                # One refused write (a D13 guard, say) must not stop a paid batch half-way.
                counts["owner_write_refused"] += 1
                logger.warning(
                    "tools.resolve_owner.write_refused",
                    run_id=str(run.id),
                    candidate_id=str(candidate.id),
                    errors=exc.error_count(),
                )
                continue
            counts["owner_resolved"] += 1

        reason = None
        if counts["owner_capped"]:
            reason = (
                f"owner judgment cap of {cap} calls reached; "
                f"{counts['owner_capped']} candidate(s) unresolved"
            )
            logger.warning("tools.resolve_owner.cap_reached", run_id=str(run.id), cap=cap)
        logger.info("tools.resolve_owner.stage_completed", run_id=str(run.id), **counts)
        return StageResult(counts=counts, degraded_reason=reason)


STAGE = _ResolveOwner()
