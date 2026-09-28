"""Fixtures for the CLI suite, which cannot use the rolled-back session.

``lpe`` opens its own engine from ``DATABASE_URL`` in its own event loop, so it cannot see anything
written inside the ``db_session`` transaction. These fixtures therefore **commit** their setup and
delete it again afterwards — the reason every one of them uses a throwaway vertical name.
"""

import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.manifests.repository import ManifestRepository
from app.manifests.schemas import ManifestBody
from tests.manifests.builders import a_body, a_vertical


@pytest.fixture
def cli_database(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point ``DATABASE_URL`` at the throwaway database for the length of one CLI test.

    ``monkeypatch`` puts the fake URL back afterwards, so the protection ``tests/conftest.py``
    installs at import survives this fixture.
    """
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    yield migrated_database
    get_settings.cache_clear()


@pytest.fixture
def committed_draft(cli_database: str) -> Iterator[tuple[UUID, str]]:
    """A committed DRAFT declaring ``fmcsa`` and ``places``; removed after the test."""
    vertical = a_vertical()

    async def create() -> UUID:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                manifest = await ManifestRepository(session).create_draft(
                    vertical, a_body("fmcsa", "places")
                )
                await session.commit()
                return manifest.id
        finally:
            await engine.dispose()

    async def remove() -> None:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine) as session:
                await session.execute(
                    text("delete from vertical_manifest where vertical = :vertical"),
                    {"vertical": vertical},
                )
                await session.commit()
        finally:
            await engine.dispose()

    manifest_id = asyncio.run(create())
    try:
        yield manifest_id, vertical
    finally:
        asyncio.run(remove())


def load_committed_body(url: str, manifest_id: UUID) -> tuple[str, ManifestBody]:
    """Read one committed row's status and body, outside any test transaction."""

    async def read() -> tuple[str, ManifestBody]:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine) as session:
                manifest = await ManifestRepository(session).get_by_id(manifest_id)
                assert manifest is not None
                return manifest.status, ManifestBody.model_validate(manifest.body)
        finally:
            await engine.dispose()

    return asyncio.run(read())
