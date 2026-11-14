"""HTTP access to the weekly run and the Friday report.

``POST /runs`` is what an external scheduler hits **weekly** (launchd now, cron on the VPS later),
and what a founder can call ad hoc. It does every check in the request and refuses there. It then
returns **202** with the accepted run and does the work in a background task, in this process.
Progress and the result are at ``GET /runs/{id}``. A second ``POST`` while one runs gets a 409
naming it. The daily review and cadence syncs are separate jobs; this route calls neither.

Handlers carry no ``try/except``: ``app.main`` renders every ``LocalProspectEngineError`` as
structured JSON at the exception's own status code.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.core.database import get_sessionmaker
from app.core.dependencies import DbSession
from app.runs.report import WeeklyReportService
from app.runs.schemas import WeeklyReport, WeeklyRunRequest, WeeklyRunResponse
from app.runs.service import WeeklyRunService

router = APIRouter(prefix="/runs", tags=["runs"])


def get_weekly_run_service() -> WeeklyRunService:
    """The service on the process's session factory, **not** the request's session.

    The background task outlives the request, and FastAPI closes a request's yield-dependencies
    before background tasks run.
    """
    return WeeklyRunService(get_sessionmaker())


def get_weekly_report_service(db: DbSession) -> WeeklyReportService:
    """The report service, bound to the request's session (it finishes inside the request)."""
    return WeeklyReportService(db)


WeeklyRunServiceDep = Annotated[WeeklyRunService, Depends(get_weekly_run_service)]
WeeklyReportServiceDep = Annotated[WeeklyReportService, Depends(get_weekly_report_service)]


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=WeeklyRunResponse)
async def start_run(
    request: WeeklyRunRequest, background_tasks: BackgroundTasks, service: WeeklyRunServiceDep
) -> WeeklyRunResponse:
    """Accept a weekly run: source, qualify, verify, enrich, route, then promote to review.

    Refused here, before anything is sourced: 404 when the vertical has no ACTIVE manifest, 409
    when a declared source cannot run (unless ``allow_degraded``) or a weekly run is in progress,
    422 for a bad body. ``dry_run`` sources for real and promotes as a dry run (no HubSpot write).
    """
    run = await service.start(request)
    background_tasks.add_task(service.execute, run.id)
    return run


@router.get("/weekly-report", response_model=WeeklyReport)
async def weekly_report(service: WeeklyReportServiceDep, week: date | None = None) -> WeeklyReport:
    """The Friday report for the week (Monday to Sunday, Dallas time) containing ``week``.

    This week when omitted. Reads HubSpot notes live; writes nothing.
    """
    return await service.build(week)


@router.get("/{run_id}", response_model=WeeklyRunResponse)
async def get_run(run_id: UUID, service: WeeklyRunServiceDep) -> WeeklyRunResponse:
    """One weekly run: its status, its sourcing run, M7 and promotion's counts."""
    return await service.get(run_id)
