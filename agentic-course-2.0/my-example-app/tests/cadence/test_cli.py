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
from app.cadence.repository import CADENCE_SYNC_LOCK_ID, CadenceRepository
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

    def test_a_second_sync_while_one_runs_says_so_and_exits_cleanly(
        self, cli_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """M1, from launchd's side: an overlapping run is a skip, not a failure."""

        async def sync_while_locked() -> int:
            engine = create_async_engine(cli_database)
            try:
                async with engine.connect() as other:
                    await other.execute(
                        text("select pg_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                    )
                    try:
                        return await asyncio.to_thread(main, ["cadence", "sync"])
                    finally:
                        await other.execute(
                            text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                        )
            finally:
                await engine.dispose()

        assert asyncio.run(sync_while_locked()) == 0
        assert "another cadence sync is running" in capsys.readouterr().out


def _portal_with_ticked_task(task_id: str) -> FakeHubSpot:
    """A fake portal holding the committed row's task, already ticked an hour ago."""
    hubspot = FakeHubSpot(FakeClock(datetime.now(UTC)))
    ticked = datetime.now(UTC) - timedelta(hours=1)
    hubspot.tasks[task_id] = {
        "id": task_id,
        "properties": {
            "hs_task_status": "COMPLETED",
            "hs_task_completion_date": ticked.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        "createdAt": "2026-10-07T14:00:00.000Z",
        "updatedAt": "2026-10-07T14:00:00.000Z",
        "archived": False,
    }
    return hubspot


async def _status_of(database_url: str, contact_id: str) -> tuple[str, str, str | None]:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            state = await CadenceRepository(session).get_by_contact(contact_id)
            assert state is not None
            return state.status, state.touch, state.hubspot_task_id
    finally:
        await engine.dispose()


class TestSyncDryRun:
    def test_a_dry_run_prints_the_plan_and_changes_nothing(
        self,
        committed_live: tuple[str, str],
        cli_database: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        contact_id, task_id = committed_live
        hubspot = _portal_with_ticked_task(task_id)

        def fake_client() -> HubSpotClient:
            return make_client(hubspot.portal)

        monkeypatch.setattr("app.cadence.cli.get_hubspot_client", fake_client)

        assert main(["cadence", "sync", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "dry run" in out
        assert f"contact {contact_id}" in out
        assert "would close 1/3 call — task ticked" in out
        assert "would create 1/3 voicemail" in out
        assert hubspot.created == []
        assert asyncio.run(_status_of(cli_database, contact_id)) == ("live", "call", task_id)


class TestPark:
    def test_park_finishes_the_cadence_and_points_at_the_open_task(
        self,
        committed_live: tuple[str, str],
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        contact_id, task_id = committed_live

        assert main(["cadence", "park", contact_id]) == 0

        out = capsys.readouterr().out
        assert f"parked {contact_id} at 1/3 call" in out
        assert f"open task {task_id}" in out
        assert "close it in HubSpot" in out
        assert "note" in out, "the reason belongs in HubSpot, so say so"
        assert asyncio.run(_status_of(cli_database, contact_id)) == ("parked", "call", None)

    def test_parking_twice_is_one_error_line(
        self, committed_live: tuple[str, str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        contact_id, _ = committed_live
        assert main(["cadence", "park", contact_id]) == 0
        capsys.readouterr()

        assert main(["cadence", "park", contact_id]) == 1

        err = capsys.readouterr().err
        assert "error:" in err
        assert "already parked" in err
        assert "Traceback" not in err

    def test_an_unknown_contact_is_one_error_line(
        self, cli_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["cadence", "park", "400999999"]) == 1

        err = capsys.readouterr().err
        assert "has no cadence" in err
        assert "Traceback" not in err

    def test_park_needs_no_hubspot_token(
        self,
        committed_live: tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        contact_id, _ = committed_live
        monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")

        assert main(["cadence", "park", contact_id]) == 0
