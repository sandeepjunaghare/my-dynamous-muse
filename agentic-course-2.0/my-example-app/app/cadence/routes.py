"""HTTP access to the cadence: the sync trigger and the overdue view.

``POST /cadence/sync`` is what an external scheduler hits — **daily**, from launchd now and cron
on the VPS later — separately from T10's weekly sourcing run. Voicemail and email are same-day
touches, and their tasks only exist once a sync has seen the previous touch close, so a weekly
sync would create them up to a week overdue. There is no in-process scheduler, and none is coming.

**There is no enrol route, by design.** Enrolling an existing contact without T13's reconstruction
would start it at touch one, and cycle position is never reset. Fresh prospects arrive through T9;
the 22 already in the portal arrive through T13.

Handlers carry no ``try/except``: ``app.main`` renders every ``LocalProspectEngineError`` — the
HubSpot gateway's included — as structured JSON at the exception's own status code.
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.cadence.schemas import OverdueTouch, SyncReport
from app.cadence.service import CadenceService
from app.core.dependencies import DbSession

router = APIRouter(prefix="/cadence", tags=["cadence"])


def get_cadence_service(db: DbSession) -> CadenceService:
    """Provide the slice's service, bound to the request's session."""
    return CadenceService(db)


CadenceServiceDep = Annotated[CadenceService, Depends(get_cadence_service)]


@router.post("/sync", response_model=SyncReport)
async def sync_cadence(service: CadenceServiceDep) -> SyncReport:
    """Read outcomes from HubSpot, advance every touch that is done, and report what is overdue.

    Safe to call as often as wanted. A sync with nothing new in HubSpot creates nothing; one that
    overlaps a running sync returns at once with ``skipped: true``; and a task create interrupted
    last time is looked up by its key before anything is created again.
    """
    return await service.sync()


@router.get("/overdue", response_model=list[OverdueTouch])
async def list_overdue(service: CadenceServiceDep) -> list[OverdueTouch]:
    """Live touches past their due date, from our own schedule — no HubSpot call."""
    return await service.overdue()
