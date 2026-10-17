"""The weekly run: brief → sourced, qualified, routed → pending review in HubSpot, unattended.

```
start()    reap stale weekly runs ─▶ ACTIVE manifest ─▶ configure stages ─▶ preflight
           ─▶ INSERT weekly_run (the partial unique index: one running at a time) ─▶ commit
execute()  SourcingPipeline.run(on_started = link the sourcing run)   ── raises ─▶ FAILED
           ─▶ sourced_at
           ─▶ PromotionService.promote_run(dry_run = the row's)        ── raises ─▶ FAILED
           ─▶ COMPLETED | DEGRADED (sourcing degraded, promotion stopped, or failures > 0)
```

``POST /runs`` calls :meth:`WeeklyRunService.start` in the request, so every refusal (no active
manifest, a source that cannot run, a run in progress) is the caller's answer. It then hands
:meth:`WeeklyRunService.execute` to a background task. ``lpe runs start`` calls both in-process
(:meth:`WeeklyRunService.run`).

**Sessions come from a factory, never the request.** The request's session is closed before a
background task runs (FastAPI ≥ 0.106), and the pipeline records a failure on a *fresh* session. So
each step here opens its own.

**A background failure is recorded, never lost.** An ``Exception`` is written to the row as
``failed`` and swallowed, because a background task has nobody to raise to. A ``BaseException``
that is not one (cancellation, Ctrl-C) is written down too, then re-raised. If even the write
fails, the original error is re-raised, and the next ``start`` reaps the row.

**Never closes the engine or the HubSpot client.** The route path shares the process's engine and
client; only the CLI closes them.
"""

from collections.abc import Callable
from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError

from app.cadence.service import HubSpotFactory
from app.core.config import get_settings
from app.core.logging import get_logger
from app.manifests.service import ManifestService
from app.promotion.client import get_hubspot_client
from app.promotion.schemas import PromotionReport
from app.promotion.service import PromotionService
from app.runs.exceptions import WeeklyRunError, WeeklyRunInProgressError, WeeklyRunNotFoundError
from app.runs.models import ONE_RUNNING, WeeklyRun
from app.runs.repository import WeeklyRunRepository
from app.runs.schemas import WeeklyRunRequest, WeeklyRunResponse, WeeklyRunStatus
from app.sourcing.pipeline import (
    DEFAULT_GEOGRAPHY,
    SessionFactory,
    SourcingPipeline,
    configured_stages,
    preflight,
)
from app.sourcing.schemas import RunStatus, SourcingBrief, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.tools.registry import Stage, registered_stages

logger = get_logger(__name__)

type StageBuilder = Callable[[int | None, bool], tuple[Stage, ...]]
"""Builds the run's stages from its options. Injected by tests; the registered stages otherwise."""

_DETAIL_MAX = 500
"""How much of an exception's text a failed run keeps in ``status_detail``, as sourcing does."""


def _registered(batch_size: int | None, allow_degraded: bool) -> tuple[Stage, ...]:
    return configured_stages(
        registered_stages(), batch_size=batch_size, allow_degraded=allow_degraded
    )


def _failure(step: str, exc: BaseException) -> str:
    return f"{step} failed: {type(exc).__name__}: {exc}"[:_DETAIL_MAX]


class WeeklyRunService:
    """Starts, executes and reads weekly runs."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        hubspot: HubSpotFactory = get_hubspot_client,
        stages: StageBuilder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._hubspot = hubspot
        self._stages = stages or _registered

    async def run(self, request: WeeklyRunRequest) -> WeeklyRunResponse:
        """Start and execute in one call: ``lpe runs start``."""
        started = await self.start(request)
        return await self.execute(started.id)

    async def start(self, request: WeeklyRunRequest) -> WeeklyRunResponse:
        """Accept a weekly run, or refuse it before anything is sourced.

        Raises ``ActiveManifestNotFoundError`` (404), ``SourceUnavailableError`` (409) or
        :class:`WeeklyRunInProgressError` (409). Only the last can leave a row behind, and that
        row is the run already in progress.
        """
        settings = get_settings()
        async with self._session_factory() as session:
            repository = WeeklyRunRepository(session)
            await repository.reap_stale(timedelta(hours=settings.weekly_run_stale_hours))
            await session.commit()

            manifest = await ManifestService(session).get_active(request.vertical)
            preflight(self._stages(request.batch_size, request.allow_degraded), manifest.body)

            try:
                run = await repository.create(request)
            except IntegrityError as exc:
                await session.rollback()
                if ONE_RUNNING not in str(exc.orig):
                    raise
                raise _in_progress(await repository.running()) from exc
            await session.commit()

            logger.info(
                "runs.weekly.run_started",
                weekly_run_id=str(run.id),
                vertical=run.vertical,
                manifest_version=manifest.version,
                batch_size=run.batch_size,
                allow_degraded=run.allow_degraded,
                dry_run=run.dry_run,
            )
            return WeeklyRunResponse.model_validate(run)

    async def execute(self, run_id: UUID) -> WeeklyRunResponse:
        """Source, then promote, then finish the row. Records every failure; see the module doc."""
        structlog.contextvars.bind_contextvars(weekly_run_id=str(run_id))
        try:
            return await self._execute(run_id)
        finally:
            structlog.contextvars.unbind_contextvars("weekly_run_id")

    async def get(self, run_id: UUID) -> WeeklyRunResponse:
        """One weekly run, with its sourcing run when one was opened."""
        async with self._session_factory() as session:
            row = await WeeklyRunRepository(session).get(run_id)
            if row is None:
                raise WeeklyRunNotFoundError(f"no weekly run {run_id}")
            response = WeeklyRunResponse.model_validate(row)
            if row.sourcing_run_id is not None:
                response.sourcing = await SourcingService(session).get_run(row.sourcing_run_id)
            return response

    # -- the steps ------------------------------------------------------------------------------

    async def _execute(self, run_id: UUID) -> WeeklyRunResponse:
        async with self._session_factory() as session:
            row = await WeeklyRunRepository(session).get(run_id)
            if row is None:
                raise WeeklyRunNotFoundError(f"no weekly run {run_id}")
            if row.status != WeeklyRunStatus.running.value:
                raise WeeklyRunError(
                    f"weekly run {run_id} is {row.status}; only a running weekly run executes"
                )
            vertical, dry_run = row.vertical, row.dry_run
            batch_size, allow_degraded = row.batch_size, row.allow_degraded

        try:
            sourcing = await self._source(run_id, vertical, batch_size, allow_degraded)
        except BaseException as exc:
            return await self._fail(run_id, _failure("sourcing", exc), exc)

        async with self._session_factory() as session:
            await WeeklyRunRepository(session).mark_sourced(run_id)
            await session.commit()

        try:
            async with self._session_factory() as session:
                report = await PromotionService(session, hubspot=self._hubspot).promote_run(
                    sourcing.id, dry_run=dry_run
                )
        except BaseException as exc:
            return await self._fail(run_id, _failure("promotion", exc), exc)

        return await self._finish(run_id, sourcing, report)

    async def _source(
        self, run_id: UUID, vertical: str, batch_size: int | None, allow_degraded: bool
    ) -> SourcingRunResponse:
        """Run the pipeline under the vertical's ACTIVE manifest, linking its run to this one.

        The manifest is read again rather than carried from ``start``: a manifest deactivated in
        between fails this run here, with the manifest's own error, instead of sourcing under it.
        """
        async with self._session_factory() as session:
            manifest = await ManifestService(session).get_active(vertical)
        brief = SourcingBrief(
            vertical=vertical, geography=DEFAULT_GEOGRAPHY, icp_band=manifest.body.icp_band.value
        )

        async def link(sourcing_run_id: UUID) -> None:
            async with self._session_factory() as session:
                await WeeklyRunRepository(session).record_sourcing_run(run_id, sourcing_run_id)
                await session.commit()

        pipeline = SourcingPipeline(
            self._session_factory, stages=self._stages(batch_size, allow_degraded)
        )
        return await pipeline.run(brief, on_started=link)

    async def _finish(
        self, run_id: UUID, sourcing: SourcingRunResponse, report: PromotionReport
    ) -> WeeklyRunResponse:
        reasons: list[str] = []
        if sourcing.status is RunStatus.degraded:
            reasons.append(f"sourcing degraded: {sourcing.status_detail or 'no reason given'}")
        if report.stopped is not None:
            reasons.append(f"promotion stopped: {report.stopped}")
        failed = report.counts.get("failed", 0)
        if failed:
            reasons.append(f"{failed} promotion(s) failed")
        status = WeeklyRunStatus.degraded if reasons else WeeklyRunStatus.completed

        async with self._session_factory() as session:
            finished = await WeeklyRunRepository(session).finish(
                run_id,
                status,
                detail="; ".join(reasons) or None,
                promotion_counts=report.counts,
                promotion_stopped=report.stopped,
                promoted=True,
            )
            await session.commit()
        if finished is None:
            return await self._lost(run_id)

        response = WeeklyRunResponse.model_validate(finished)
        response.sourcing = sourcing
        logger.info(
            "runs.weekly.run_finished",
            status=status.value,
            dry_run=report.dry_run,
            brief_to_hubspot_minutes=response.brief_to_hubspot_minutes,
            total_usd=str(sourcing.cost.total_usd()),
            **report.counts,
        )
        return response

    async def _fail(self, run_id: UUID, detail: str, exc: BaseException) -> WeeklyRunResponse:
        """Record ``failed``, then swallow an ``Exception`` and re-raise anything else."""
        try:
            async with self._session_factory() as session:
                finished = await WeeklyRunRepository(session).finish(
                    run_id, WeeklyRunStatus.failed, detail=detail
                )
                await session.commit()
        except Exception:
            logger.exception("runs.weekly.failure_unrecorded", detail=detail)
            raise exc from None
        logger.error("runs.weekly.run_failed", detail=detail)
        if not isinstance(exc, Exception):
            raise exc
        if finished is None:
            return await self._lost(run_id)
        return await self.get(run_id)

    async def _lost(self, run_id: UUID) -> WeeklyRunResponse:
        """The row was no longer running when this run finished: the reaper got there first."""
        logger.warning("runs.weekly.finish_lost")
        return await self.get(run_id)


def _in_progress(blocking: WeeklyRun | None) -> WeeklyRunInProgressError:
    if blocking is None:
        # It finished between the failed insert and this read; the caller can simply retry.
        return WeeklyRunInProgressError(
            "a weekly run was in progress and has just finished; start again"
        )
    return WeeklyRunInProgressError(
        f"weekly run {blocking.id} ({blocking.vertical}) has been running since "
        f"{blocking.requested_at.isoformat(timespec='seconds')}; one runs at a time. Follow it "
        f"at GET /runs/{blocking.id}"
    )
