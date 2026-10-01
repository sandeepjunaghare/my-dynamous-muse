"""Data access for ``vertical_manifest``. Queries only — no business rules, no commits.

**The repository flushes; the service commits.** T2 is the first slice, so this is the house
convention T4, T9 and T11 inherit: a repository method may write and flush so the row gets its
defaults and any constraint fires here rather than three calls later, but the transaction boundary
belongs to the service, which is the only layer that knows whether an operation is finished.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.manifests.models import VerticalManifest
from app.manifests.schemas import ManifestBody, ManifestStatus

logger = get_logger(__name__)


class ManifestRepository:
    """Reads and writes manifest rows through one :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, manifest_id: UUID) -> VerticalManifest | None:
        """Return one manifest by primary key, whatever its status."""
        result = await self._session.execute(
            select(VerticalManifest).where(VerticalManifest.id == manifest_id)
        )
        return result.scalar_one_or_none()

    async def get(self, vertical: str, version: int) -> VerticalManifest | None:
        """Return one specific version of a vertical's manifest, whatever its status."""
        result = await self._session.execute(
            select(VerticalManifest).where(
                VerticalManifest.vertical == vertical,
                VerticalManifest.version == version,
            )
        )
        return result.scalar_one_or_none()

    async def get_active(self, vertical: str) -> VerticalManifest | None:
        """Return the ACTIVE manifest for a vertical, or ``None``.

        **A DRAFT is never returned.** The pipeline asking for "the manifest" must not be handed a
        proposal nobody accepted — that is the whole point of the lifecycle.
        """
        result = await self._session.execute(
            select(VerticalManifest).where(
                VerticalManifest.vertical == vertical,
                VerticalManifest.status == ManifestStatus.active.value,
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        vertical: str | None = None,
        status: ManifestStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[VerticalManifest]:
        """List manifests, newest version first within each vertical.

        The ordering is fixed rather than caller-supplied so the CLI table and the API return rows
        in the same order every run — a list whose order drifts is a list nobody can diff.
        """
        query = select(VerticalManifest)
        if vertical is not None:
            query = query.where(VerticalManifest.vertical == vertical)
        if status is not None:
            query = query.where(VerticalManifest.status == status.value)

        query = query.order_by(
            VerticalManifest.vertical.asc(),
            VerticalManifest.version.desc(),
        )
        result = await self._session.execute(query.limit(limit).offset(offset))
        return result.scalars().all()

    async def next_version(self, vertical: str) -> int:
        """The version a new draft for this vertical would take: ``max + 1``, or 1."""
        result = await self._session.execute(
            select(VerticalManifest.version)
            .where(VerticalManifest.vertical == vertical)
            .order_by(VerticalManifest.version.desc())
            .limit(1)
        )
        highest = result.scalar_one_or_none()
        return 1 if highest is None else highest + 1

    async def create_draft(self, vertical: str, body: ManifestBody) -> VerticalManifest:
        """Insert a new DRAFT row at the next version for this vertical.

        ``model_dump(mode="json")`` — **not** plain ``model_dump()``, which leaves ``datetime``
        objects in place and makes asyncpg raise ``DataError: invalid input for query argument``
        the moment they reach JSONB.
        """
        manifest = VerticalManifest(
            vertical=vertical,
            version=await self.next_version(vertical),
            status=ManifestStatus.draft.value,
            body=body.model_dump(mode="json"),
        )
        self._session.add(manifest)
        await self._session.flush()
        logger.info(
            "manifests.repository.draft_created",
            manifest_id=str(manifest.id),
            vertical=vertical,
            version=manifest.version,
        )
        return manifest

    async def mark_superseded(self, manifest: VerticalManifest) -> None:
        """Retire a previously ACTIVE row. The row is retained, never overwritten (AC6)."""
        manifest.status = ManifestStatus.superseded.value
        await self._session.flush()
        logger.info(
            "manifests.repository.manifest_superseded",
            manifest_id=str(manifest.id),
            vertical=manifest.vertical,
            version=manifest.version,
        )

    async def mark_active(
        self,
        manifest: VerticalManifest,
        body: ManifestBody,
        actor: str,
    ) -> None:
        """Flip a row to ACTIVE, storing the body that now carries its terms decisions."""
        manifest.status = ManifestStatus.active.value
        manifest.body = body.model_dump(mode="json")
        manifest.activated_at = datetime.now(UTC)
        manifest.activated_by = actor
        await self._session.flush()
        logger.info(
            "manifests.repository.manifest_activated",
            manifest_id=str(manifest.id),
            vertical=manifest.vertical,
            version=manifest.version,
        )
