"""Stage 3 of 5: ``classify_rollup`` — one judgment call per surviving candidate (T7).

For each candidate of the run that no rule has already removed, the judgment node decides every
judgment rule in the manifest and answers its qualifying signals in one call (D12). Fired rules
that carry a citation become ``disqualification`` rows; cited signal answers become the candidate's
``priority``, written as this stage (it owns that field). The same call's cited homepage fills an
empty ``website``, and a cited headcount becomes ``headcount_band`` (T6). Nothing uncited is
recorded, and a disqualified business keeps none of it.

**Free before paid** (D8): a candidate the free predicates already removed is never sent, and
neither is one ``verify_business`` could not verify — neither Places at its address nor a homepage
on its own email domain (``CandidateFields.is_verified``, website-verification B1): T9 cannot
promote it (OQ-5). What Places saw
reaches the prompt as context through ``StageContext.places`` and is never stored (D13). **Retries
do not pay twice** for a candidate already scored or disqualified. **A per-run call cap**
(``max_judgment_calls_per_run``) is a breaker against a runaway loop: reaching it ends the stage
**degraded**, never raising, so a paid batch is not handed back (Amendment 3). One failed call is
counted and the batch goes on — one bad SDK run must not sink a paid batch.
"""

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.manifests.schemas import RuleKind
from app.qualification import judgment
from app.qualification.exceptions import JudgmentNodeError
from app.qualification.service import QualificationService
from app.sourcing.service import SourcingService
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult

logger = get_logger(__name__)


class _ClassifyRollup:
    """The ``classify_rollup`` stage."""

    stage = PipelineStage.classify_rollup

    async def run(self, context: StageContext) -> StageResult:
        body = context.manifest.body
        has_rules = any(c.value.kind is RuleKind.judgment for c in body.disqualifier_rules)
        if not has_rules and not body.qualifying_signals:
            logger.info("qualification.stage.judging_skipped", run_id=str(context.run_id))
            return StageResult(counts={"classified": 0})

        cap = get_settings().max_judgment_calls_per_run
        sourcing = SourcingService(context.session)
        qualification = QualificationService(context.session)
        run = await sourcing.get_run(context.run_id)
        removed = await qualification.disqualified_candidate_ids(run.id)

        calls = classified = disqualified = failed = scored = skipped = 0
        unverified = capped = website_filled = headcount_filled = 0
        for candidate in await sourcing.list_candidates(run.id):
            fields = candidate.fields
            if candidate.id in removed or fields.priority is not None:
                skipped += 1
                continue
            if not fields.is_verified():
                unverified += 1
                continue
            if calls >= cap:
                capped += 1
                continue
            calls += 1
            places = context.places.get(fields.registry_id.value)
            try:
                judged = await judgment.run_judgment(fields, body, cost=context.cost, places=places)
            except JudgmentNodeError as exc:
                failed += 1
                logger.warning(
                    "qualification.stage.candidate_failed",
                    run_id=str(run.id),
                    candidate_id=str(candidate.id),
                    reason=exc.reason,
                )
                continue

            classified += 1
            admitted = judgment.admit_judgment(
                judged.answer,
                body,
                reads=judged.reads,
                evidence=judgment.evidence_reads(fields),
                listed=judgment.listing_name(places, fields),
            )
            if await qualification.record_judgment(candidate, run, context.manifest, admitted):
                # A rejected business keeps nothing: a "live" score on a rollup's row is a
                # rejection nobody can see from the candidate (PR #18 review, L5).
                disqualified += 1
                continue
            update: dict[str, object] = {}
            if admitted.priority is not None:
                update["priority"] = admitted.priority
            if admitted.website is not None and fields.website is None:
                update["website"] = admitted.website
            if admitted.headcount_band is not None:
                update["headcount_band"] = admitted.headcount_band
            if not update:
                continue
            try:
                await sourcing.record_candidates(
                    run.id,
                    [fields.model_copy(update=update)],
                    stage=PipelineStage.classify_rollup,
                )
            except ValidationError as exc:
                # The candidate model refused the write (e.g. a D13 guard). One refusal must not
                # stop a paid batch half-way (PR #18 review, M2).
                failed += 1
                logger.warning(
                    "qualification.stage.write_refused",
                    run_id=str(run.id),
                    candidate_id=str(candidate.id),
                    errors=exc.error_count(),
                )
                continue
            scored += "priority" in update
            website_filled += "website" in update
            headcount_filled += "headcount_band" in update

        counts = {
            "classified": classified,
            "judgment_disqualified": disqualified,
            "judgment_failed": failed,
            "scored": scored,
            "not_scored": classified - scored - disqualified,
            "judgment_skipped": skipped,
            "judgment_unverified_skipped": unverified,
            "judgment_capped": capped,
            "judgment_website_filled": website_filled,
            "judgment_headcount_filled": headcount_filled,
        }
        reason = None
        if capped:
            reason = f"judgment cap of {cap} calls reached; {capped} candidate(s) unjudged"
            logger.warning("qualification.stage.cap_reached", run_id=str(run.id), cap=cap)
        logger.info("qualification.stage.stage_completed", run_id=str(run.id), **counts)
        return StageResult(counts=counts, degraded_reason=reason)


STAGE = _ClassifyRollup()
