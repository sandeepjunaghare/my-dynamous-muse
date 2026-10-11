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
from tests.manifests.builders import a_body, a_dry_run, a_vertical

DELETE_DRY_RUNS = (
    "delete from manifest_dry_run where manifest_id in "
    "(select id from vertical_manifest where vertical = :vertical)"
)
"""A dry-run references its manifest, so it goes first when a test's vertical is cleaned up."""


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


def _committed_draft(
    cli_database: str, *, dry_run: bool, body: ManifestBody | None = None
) -> Iterator[tuple[UUID, str]]:
    """Commit a DRAFT (``fmcsa`` + ``places`` by default), optionally dry-run; remove it after."""
    vertical = a_vertical()

    async def create() -> UUID:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                repository = ManifestRepository(session)
                manifest = await repository.create_draft(
                    vertical, body or a_body("fmcsa", "places")
                )
                if dry_run:
                    await repository.record_dry_run(manifest.id, a_dry_run(manifest.id), "test")
                await session.commit()
                return manifest.id
        finally:
            await engine.dispose()

    manifest_id = asyncio.run(create())
    try:
        yield manifest_id, vertical
    finally:
        asyncio.run(_delete_vertical(cli_database, vertical))


@pytest.fixture
def committed_draft(cli_database: str) -> Iterator[tuple[UUID, str]]:
    """A committed DRAFT with a recorded dry-run, so ``activate`` reaches the terms gate."""
    yield from _committed_draft(cli_database, dry_run=True)


@pytest.fixture
def committed_draft_without_dry_run(cli_database: str) -> Iterator[tuple[UUID, str]]:
    """A committed DRAFT nobody has dry-run — what ``activate`` must refuse."""
    yield from _committed_draft(cli_database, dry_run=False)


@pytest.fixture
def throwaway_vertical(cli_database: str) -> Iterator[str]:
    """A vertical name for a CLI test that *creates* rows; every row under it is removed after."""
    vertical = a_vertical()
    try:
        yield vertical
    finally:
        asyncio.run(_delete_vertical(cli_database, vertical))


async def _delete_vertical(url: str, vertical: str) -> None:
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text(DELETE_DRY_RUNS), {"vertical": vertical})
            await session.execute(
                text("delete from vertical_manifest where vertical = :vertical"),
                {"vertical": vertical},
            )
            await session.commit()
    finally:
        await engine.dispose()


def load_committed_rows(url: str, vertical: str) -> list[tuple[str, ManifestBody]]:
    """Every committed row for one vertical, as (status, body), outside any test transaction."""

    async def read() -> list[tuple[str, ManifestBody]]:
        engine = create_async_engine(url)
        try:
            async with AsyncSession(engine) as session:
                rows = await ManifestRepository(session).list(vertical=vertical)
                return [(row.status, ManifestBody.model_validate(row.body)) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(read())


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


@pytest.fixture
def committed_api_only_draft(cli_database: str) -> Iterator[tuple[UUID, str]]:
    """A committed DRAFT shaped like the fire manifest: no bulk-file source to extract from."""
    body = a_body("fmcsa")
    api_only = ManifestBody.model_validate(
        {
            **body.model_dump(mode="json"),
            "sources": [
                {
                    **source.model_dump(mode="json"),
                    "value": {**source.value.model_dump(mode="json"), "name": name, "kind": kind},
                }
                for source, (name, kind) in zip(
                    body.sources * 2,
                    (("registry", "registry_api"), ("places", "web_lookup")),
                    strict=True,
                )
            ],
        }
    )
    yield from _committed_draft(cli_database, dry_run=False, body=api_only)
