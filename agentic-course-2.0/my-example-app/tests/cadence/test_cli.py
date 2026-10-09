"""``lpe cadence`` against the throwaway database.

The CLI opens its own engine in its own event loop, so it cannot see the rolled-back test session.
These tests therefore **commit** their rows and delete them afterwards, each under a contact id no
other test uses.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.cadence.machine import CadencePosition
from app.cadence.repository import CadenceRepository
from app.cli import main
from app.core.config import get_settings
from app.promotion.client import HubSpotClient
from tests.cadence.conftest import FakeClock, FakeHubSpot
from tests.conftest import requires_db
from tests.promotion.conftest import make_client

pytestmark = requires_db


@pytest.fixture
def cli_database(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point ``DATABASE_URL`` at the throwaway database for one CLI test."""
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    yield migrated_database
    get_settings.cache_clear()


@pytest.fixture
def committed_live(cli_database: str) -> Iterator[tuple[str, str]]:
    """One committed live cadence, a day overdue. Yields ``(contact_id, task_id)``."""
    contact_id = f"cli{uuid4().hex[:12]}"
    task_id = "88123456"
    yesterday = datetime.now(UTC) - timedelta(days=1)

    async def create() -> None:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await CadenceRepository(session).create(
                    contact_id=contact_id,
                    company_id=None,
                    owner_id=None,
                    position=CadencePosition.first(),
                    task_id=task_id,
                    due_at=yesterday,
                    anchor_at=yesterday,
                    enrolled_at=yesterday,
                )
                await session.commit()
        finally:
            await engine.dispose()

    async def remove() -> None:
        engine = create_async_engine(cli_database)
        try:
            async with AsyncSession(engine) as session:
                await session.execute(
                    text("delete from cadence_state where hubspot_contact_id = :contact"),
                    {"contact": contact_id},
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(create())
    try:
        yield contact_id, task_id
    finally:
        asyncio.run(remove())


class TestOverdue:
    def test_lists_the_overdue_touch(
        self, committed_live: tuple[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        contact_id, task_id = committed_live

        assert main(["cadence", "overdue"]) == 0

        out = capsys.readouterr().out
        line = next(line for line in out.splitlines() if contact_id in line)
        assert "1/3 call" in line
        assert task_id in line

    def test_needs_no_hubspot_token(
        self,
        committed_live: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")

        assert main(["cadence", "overdue"]) == 0
        assert committed_live[0] in capsys.readouterr().out


class TestSync:
    def test_sync_reads_the_portal_and_reports(
        self,
        committed_live: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        contact_id, task_id = committed_live
        hubspot = FakeHubSpot(FakeClock(datetime.now(UTC)))
        hubspot.tasks[task_id] = {
            "id": task_id,
            "properties": {"hs_task_status": "NOT_STARTED", "hs_task_completion_date": None},
            "createdAt": "2026-10-07T14:00:00.000Z",
            "updatedAt": "2026-10-07T14:00:00.000Z",
            "archived": False,
        }

        def fake_client() -> HubSpotClient:
            return make_client(hubspot.portal)

        monkeypatch.setattr("app.cadence.cli.get_hubspot_client", fake_client)

        assert main(["cadence", "sync"]) == 0

        out = capsys.readouterr().out
        assert "tasks created 0" in out
        assert contact_id in out, "the overdue touch is surfaced by the sync"
        assert hubspot.created == []

    def test_a_missing_token_is_one_line_not_a_traceback(
        self,
        committed_live: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")

        assert main(["cadence", "sync"]) == 1

        err = capsys.readouterr().err
        assert "error: HUBSPOT_PRIVATE_APP_TOKEN is not set" in err
        assert "Traceback" not in err
