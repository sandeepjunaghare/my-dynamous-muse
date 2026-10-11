"""The weekly run: a deterministic pipeline, not an agent loop (D1).

```
reap stale runs ─▶ start_run ─▶ stage 1 … stage n ─▶ finish COMPLETED | DEGRADED
                                     │ any exception
                                     ▼
                          rollback ─▶ finish FAILED on a fresh session ─▶ re-raise
```

The stages are whatever ``registered_stages()`` finds, in ``PipelineStage`` order — today only
``search_registry``; T6, T7 and T8 add theirs by adding a file. Ordinary async Python: same input,
same output, and every step testable against fixtures. The Agent SDK is called inside two stages
(``resolve_owner``, ``classify_rollup``) and never decides what runs next.

**Why a session factory, not a session.** A stage that fails mid-write leaves its transaction
aborted, and finishing the run on that session before a rollback raises ``PendingRollbackError``
(T4 review, decision 3). So the runner rolls the stage's session back, then records the failure on
a *fresh* session, with the counts and the cost so far: a run that dies at Places call 499 still
says what it spent. The rollback is what the tests prove necessary; the fresh session is for when
the rollback itself cannot run — a dropped connection — where it draws a new one from the pool. A
run killed outright never reaches the ``except`` at all; the next run's reaper marks it failed.
"""

from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.cost import RunCost
from app.core.database import get_sessionmaker
from app.core.logging import get_logger
from app.manifests.service import ManifestService
from app.sourcing.schemas import RunOutcome, SourcingBrief, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.tools.registry import Stage, StageContext, registered_stages

logger = get_logger(__name__)

type SessionFactory = Callable[[], AsyncSession]

_DETAIL_MAX = 500
"""How much of an exception's text a failed run keeps in ``status_detail``."""


class SourcingPipeline:
    """Runs one brief through every registered stage and records how it ended."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        stages: Sequence[Stage] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_sessionmaker()
        self._stages = tuple(stages) if stages is not None else None

    async def run(self, brief: SourcingBrief) -> SourcingRunResponse:
        """Source one brief. Returns the finished run; re-raises whatever failed it.

        Nothing is opened if the vertical has no ACTIVE manifest: ``start_run`` refuses with
        ``ActiveManifestNotFoundError`` before a row exists.
        """
        settings = get_settings()
        stages = self._stages if self._stages is not None else registered_stages()

        async with self._session_factory() as session:
            service = SourcingService(session)
            await service.reap_stale_runs(
                brief.vertical, older_than=timedelta(hours=settings.sourcing_stale_run_hours)
            )
            run = await service.start_run(brief)
            manifest = await ManifestService(session).get(run.manifest_id)

            structlog.contextvars.bind_contextvars(run_id=str(run.id))
            cost = RunCost()
            counts: dict[str, int] = {}
            reasons: list[str] = []
            try:
                logger.info(
                    "sourcing.pipeline.run_started",
                    vertical=brief.vertical,
                    stages=[stage.stage.value for stage in stages],
                )
                for stage in stages:
                    result = await stage.run(
                        StageContext(run_id=run.id, manifest=manifest, session=session, cost=cost)
                    )
                    clashing = sorted(set(counts) & set(result.counts))
                    if clashing:
                        # Two stages counting under one name would silently overwrite each other,
                        # and the run would report a number neither stage produced.
                        raise ValueError(
                            f"stage {stage.stage.value} reports counts already reported: {clashing}"
                        )
                    counts.update(result.counts)
                    if result.degraded_reason is not None:
                        reasons.append(f"{stage.stage.value}: {result.degraded_reason}")
                    logger.info(
                        "sourcing.pipeline.stage_completed",
                        stage=stage.stage.value,
                        counts=dict(result.counts),
                        degraded_reason=result.degraded_reason,
                    )

                outcome = RunOutcome.degraded if reasons else RunOutcome.completed
                finished = await service.finish_run(
                    run.id,
                    outcome,
                    counts=counts,
                    cost=cost,
                    detail="; ".join(reasons) or None,
                )
                logger.info("sourcing.pipeline.run_finished", status=outcome.value, counts=counts)
                return finished
            # BaseException, deliberately: a Ctrl-C or a cancelled task must leave a record too.
            # Every path re-raises; nothing here swallows the failure, it only writes it down.
            except BaseException as exc:
                with suppress(Exception):
                    await session.rollback()
                await self._record_failure(run.id, exc, counts=counts, cost=cost)
                raise
            finally:
                structlog.contextvars.unbind_contextvars("run_id")

    async def _record_failure(
        self, run_id: UUID, exc: BaseException, *, counts: dict[str, int], cost: RunCost
    ) -> None:
        """Finish the run ``failed`` on a fresh session. Never raises over the original error."""
        detail = f"{type(exc).__name__}: {exc}"[:_DETAIL_MAX]
        try:
            async with self._session_factory() as fresh:
                await SourcingService(fresh).finish_run(
                    run_id, RunOutcome.failed, counts=counts, cost=cost, detail=detail
                )
        except Exception:
            # The original failure is the one worth seeing; the reaper will close this run.
            logger.exception("sourcing.pipeline.failure_unrecorded", run_id=str(run_id))
            return
        logger.error("sourcing.pipeline.run_failed", detail=detail, counts=counts)
