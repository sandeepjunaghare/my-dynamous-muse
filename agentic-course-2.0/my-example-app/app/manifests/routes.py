"""Read-only HTTP access to manifests.

**There is no write route, by design.** A draft is created by T12's authoring agent and accepted by
a human on the CLI, where the terms-of-use decision is recorded. A ``POST /manifests`` would be a
second door into ACTIVE that bypasses the gate this slice exists to be.

Handlers carry no ``try/except``: ``app.main`` renders every ``LocalProspectEngineError`` as
structured JSON at the exception's own status code, and catching one here would produce a second,
inconsistent error shape.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import DbSession
from app.manifests.schemas import ManifestResponse, ManifestStatus
from app.manifests.service import ManifestService

router = APIRouter(prefix="/manifests", tags=["manifests"])


def get_manifest_service(db: DbSession) -> ManifestService:
    """Provide the slice's service, bound to the request's session."""
    return ManifestService(db)


ManifestServiceDep = Annotated[ManifestService, Depends(get_manifest_service)]


@router.get("", response_model=list[ManifestResponse])
async def list_manifests(
    service: ManifestServiceDep,
    vertical: str | None = None,
    status: ManifestStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ManifestResponse]:
    """List manifests, newest version first within each vertical."""
    return await service.list(vertical=vertical, status=status, limit=limit, offset=offset)


@router.get("/active/{vertical}", response_model=ManifestResponse)
async def get_active_manifest(vertical: str, service: ManifestServiceDep) -> ManifestResponse:
    """The ACTIVE manifest for a vertical; 404 when only drafts exist, or nothing does."""
    return await service.get_active(vertical)


@router.get("/{manifest_id}", response_model=ManifestResponse)
async def get_manifest(manifest_id: UUID, service: ManifestServiceDep) -> ManifestResponse:
    """One manifest by id, whatever its status.

    ``manifest_id`` is typed as ``UUID`` so a malformed id is a 422 from FastAPI's own validation
    rather than a 500 from the database.
    """
    return await service.get(manifest_id)
