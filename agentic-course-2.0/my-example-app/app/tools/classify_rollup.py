"""Stage 4 of 5: ``classify_rollup`` — one judgment call per surviving candidate (T7).

For each candidate of the run that no rule has already removed, the judgment node decides every
judgment rule in the manifest and answers its qualifying signals in one call (D12). Fired rules
that carry a citation become ``disqualification`` rows; cited signal answers become the candidate's
``priority``, written as this stage (it owns that field). Nothing uncited is recorded.

**Free before paid** (D8): a candidate the free predicates already removed is never sent. **Retries
do not pay twice** for a candidate already scored or disqualified. **A per-run call cap** is the
circuit breaker (``max_judgment_calls_per_run``); hitting it raises ``CostLimitExceededError`` and
the runner (T5) decides the run is degraded. One failed call is counted and the batch goes on —
one bad SDK run must not sink a paid batch.

Places evidence is ``None`` here until T6 builds the in-run channel that carries it (D13).
"""

from app.core.config import get_settings
from app.core.cost import BillableKind
from app.core.exceptions import CostLimitExceededError
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
        for candidate in await sourcing.list_candidates(run.id):
            if candidate.id in removed or candidate.fields.priority is not None:
                skipped += 1
                continue
            if calls >= cap:
                logger.warning(
                    "qualification.stage.cap_reached", run_id=str(run.id), cap=cap, calls=calls
                )
                raise CostLimitExceededError(
                    f"per-run cap of {cap} judgment calls reached",
                    kind=BillableKind.anthropic_tokens.value,
                    cap=cap,
                    recorded=calls,
                )
            calls += 1
            try:
                judged = await judgment.run_judgment(candidate.fields, body, cost=context.cost)
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
                evidence=judgment.evidence_reads(candidate.fields),
            )
            if await qualification.record_judgment(candidate, run, context.manifest, admitted):
                disqualified += 1
            if admitted.priority is not None:
                scored += 1
                await sourcing.record_candidates(
                    run.id,
                    [candidate.fields.model_copy(update={"priority": admitted.priority})],
                    stage=PipelineStage.classify_rollup,
                )

        counts = {
            "classified": classified,
            "judgment_disqualified": disqualified,
            "judgment_failed": failed,
            "scored": scored,
            "not_scored": classified - scored,
            "judgment_skipped": skipped,
        }
        logger.info("qualification.stage.stage_completed", run_id=str(run.id), **counts)
        return StageResult(counts=counts)


STAGE = _ClassifyRollup()
