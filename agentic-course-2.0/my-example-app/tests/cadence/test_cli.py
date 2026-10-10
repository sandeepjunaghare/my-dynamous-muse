"""``lpe cadence`` against the throwaway database.

The CLI opens its own engine in its own event loop, so it cannot see the rolled-back test session.
These tests therefore **commit** their rows and delete them afterwards, each under a contact id no
other test uses.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.cadence.machine import CadencePosition
from app.cadence.repository import CADENCE_SYNC_LOCK_ID, CadenceRepository
from app.cadence.schemas import ActivityKind
from app.cli import main
from app.core.config import get_settings
from app.promotion.client import HubSpotClient
from tests.cadence.conftest import FakeClock, FakeHubSpot, raced_by_another_run
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
        assert "as a note on the contact in HubSpot" in out, "the reason belongs in HubSpot"
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

    def test_park_while_a_sync_runs_is_one_error_line(
        self,
        committed_live: tuple[str, str],
        cli_database: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        contact_id, _ = committed_live

        async def park_while_locked() -> int:
            engine = create_async_engine(cli_database)
            try:
                async with engine.connect() as other:
                    await other.execute(
                        text("select pg_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                    )
                    try:
                        return await asyncio.to_thread(main, ["cadence", "park", contact_id])
                    finally:
                        await other.execute(
                            text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                        )
            finally:
                await engine.dispose()

        assert asyncio.run(park_while_locked()) == 1
        err = capsys.readouterr().err
        assert "a cadence sync is running" in err
        assert asyncio.run(_status_of(cli_database, contact_id))[0] == "live"


@pytest.fixture
def fresh_contact(cli_database: str) -> Iterator[str]:
    """A digits-only contact id no other test uses; any row adoption commits is removed after."""
    contact_id = f"4{uuid4().int % 10**11:011d}"

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

    try:
        yield contact_id
    finally:
        asyncio.run(remove())


def _hand_worked_portal(
    contact_id: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeHubSpot, str]:
    """A portal with one hand-worked contact: a logged call and an open hand task."""
    hubspot = FakeHubSpot(FakeClock(datetime.now(UTC)))
    hubspot.add_contact(contact_id, datetime.now(UTC) - timedelta(days=20), first="Jorge")
    hubspot.log(ActivityKind.call, at=datetime.now(UTC) - timedelta(days=18), contact=contact_id)
    hand = hubspot.add_hand_task("Follow up with Jorge", contact=contact_id)

    def fake_client() -> HubSpotClient:
        return make_client(hubspot.portal)

    monkeypatch.setattr("app.cadence.cli.get_hubspot_client", fake_client)
    return hubspot, hand


def _roster_file(tmp_path: Path, *entries: tuple[str, str]) -> Path:
    """A roster with one ``[[prospect]]`` per ``(contact, action)``."""
    roster = tmp_path / "adopt.toml"
    roster.write_text(
        "".join(
            f'[[prospect]]\ncontact = "{contact}"\naction = "{action}"\n'
            for contact, action in entries
        ),
        encoding="utf-8",
    )
    return roster


async def _row_exists(database_url: str, contact_id: str) -> bool:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            return await CadenceRepository(session).get_by_contact(contact_id) is not None
    finally:
        await engine.dispose()


class TestAdopt:
    def test_adopt_with_no_flags_is_a_usage_error(
        self, cli_database: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exited:
            main(["cadence", "adopt"])

        assert exited.value.code == 2
        assert "adopt needs --roster" in capsys.readouterr().err

    def test_a_dry_run_lists_the_evidence_and_writes_nothing(
        self,
        fresh_contact: str,
        cli_database: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, hand = _hand_worked_portal(fresh_contact, monkeypatch)

        assert main(["cadence", "adopt", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "dry run" in out
        assert f"contact {fresh_contact} (Jorge)" in out
        assert "evidence: 1/3 call ← call" in out
        assert "would start 1/3 voicemail" in out
        assert f'{hand} "Follow up with Jorge"' in out
        assert hubspot.created == []
        assert asyncio.run(_row_exists(cli_database, fresh_contact)) is False

    def test_a_company_only_task_says_which_contact_already_covers_it(
        self,
        fresh_contact: str,
        cli_database: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, _ = _hand_worked_portal(fresh_contact, monkeypatch)
        hubspot.link_company(fresh_contact, "7000000001")
        duplicate = hubspot.add_hand_task("Follow up with APS FireCo", company="7000000001")
        hubspot.add_contact("400000000777", datetime.now(UTC))
        hubspot.link_company("400000000777", "7000000002")
        quiet = hubspot.add_hand_task("Follow up with Eagle", company="7000000002")
        orphan = hubspot.add_hand_task("Call Kodiak", company="7000000003")
        for contact in ("400000000778", "400000000779"):
            hubspot.add_contact(contact, datetime.now(UTC))
            hubspot.link_company(contact, "7000000004")
        pair = hubspot.add_hand_task("Follow up with Central", company="7000000004")

        assert main(["cadence", "adopt", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert (
            f'task {duplicate} "Follow up with APS FireCo" is on company 7000000001, whose contact '
            f"{fresh_contact} is listed above — adopting it covers this; close this task by hand"
        ) in out
        assert (
            f'task {quiet} "Follow up with Eagle" is on company 7000000002, whose contact '
            "400000000777 has no open hand task — put it in the roster to adopt it"
        ) in out
        assert (
            f'task {orphan} "Call Kodiak" is on company 7000000003 with no contact — add a '
            "contact in HubSpot, then put it in the roster"
        ) in out
        assert (
            f'task {pair} "Follow up with Central" is on company 7000000004, whose contacts '
            "400000000778, 400000000779 have no open hand task — put them in the roster to adopt "
            "them"
        ) in out

    def test_a_roster_adopts_and_lists_the_hand_tasks_to_close(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, hand = _hand_worked_portal(fresh_contact, monkeypatch)
        roster = tmp_path / "adopt.toml"
        roster.write_text(
            f'[[prospect]]\ncontact = "{fresh_contact}"\naction = "adopt"\n', encoding="utf-8"
        )

        assert main(["cadence", "adopt", "--roster", str(roster)]) == 0

        out = capsys.readouterr().out
        assert f"adopted {fresh_contact} at 1/3 voicemail" in out
        assert f"close in HubSpot: {hand}" in out
        assert "outcomes stay in HubSpot" in out
        assert hubspot.created_subjects() == ["Cadence 1/3 · voicemail"]
        assert asyncio.run(_status_of(cli_database, fresh_contact))[:2] == ("live", "voicemail")

    def test_a_lost_enrol_race_names_the_task_it_left_open(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """pr-14 round 2 N1: the person is told which task to close, not only the log."""
        hubspot, _ = _hand_worked_portal(fresh_contact, monkeypatch)
        monkeypatch.setattr(
            CadenceRepository, "create", raced_by_another_run(CadenceRepository.create)
        )
        roster = tmp_path / "adopt.toml"
        roster.write_text(
            f'[[prospect]]\ncontact = "{fresh_contact}"\naction = "adopt"\n', encoding="utf-8"
        )

        assert main(["cadence", "adopt", "--roster", str(roster)]) == 0

        out = capsys.readouterr().out
        orphan = hubspot.last_task_id()
        assert f"contact {fresh_contact}: already has a cadence — skipped" in out
        assert f"enrolled first — close in HubSpot: {orphan}" in out
        assert "would start" not in out and "  start " not in out, "no plan for a skipped entry"
        assert "old hand tasks to close" not in out, "this run superseded none of them"

    def test_a_park_entry_prints_parked_and_final(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, hand = _hand_worked_portal(fresh_contact, monkeypatch)
        roster = _roster_file(tmp_path, (fresh_contact, "park"))

        assert main(["cadence", "adopt", "--roster", str(roster)]) == 0

        out = capsys.readouterr().out
        assert (
            f"parked {fresh_contact} at 1/3 voicemail — final, it will not be enrolled again"
        ) in out
        assert f"old hand tasks to close in HubSpot: {hand}" in out
        assert hubspot.created == []
        assert asyncio.run(_status_of(cli_database, fresh_contact))[:2] == ("parked", "voicemail")

    def test_an_entry_that_fails_exits_1_and_the_rest_still_adopt(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _hand_worked_portal(fresh_contact, monkeypatch)
        unknown = "400999999999"
        roster = _roster_file(tmp_path, (unknown, "adopt"), (fresh_contact, "adopt"))

        assert main(["cadence", "adopt", "--roster", str(roster)]) == 1

        out = capsys.readouterr().out
        assert f"contact {unknown}: failed (contact_not_found)" in out
        assert f"adopted {fresh_contact} at 1/3 voicemail" in out
        assert asyncio.run(_row_exists(cli_database, unknown)) is False

    def test_adopt_while_a_sync_runs_is_one_error_line(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, _ = _hand_worked_portal(fresh_contact, monkeypatch)
        roster = _roster_file(tmp_path, (fresh_contact, "adopt"))

        async def adopt_while_locked() -> int:
            engine = create_async_engine(cli_database)
            try:
                async with engine.connect() as other:
                    await other.execute(
                        text("select pg_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                    )
                    try:
                        return await asyncio.to_thread(
                            main, ["cadence", "adopt", "--roster", str(roster)]
                        )
                    finally:
                        await other.execute(
                            text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                        )
            finally:
                await engine.dispose()

        assert asyncio.run(adopt_while_locked()) == 1
        err = capsys.readouterr().err
        assert "error: a cadence sync is running — adopt again when it has finished" in err
        assert "Traceback" not in err
        assert hubspot.created == []
        assert asyncio.run(_row_exists(cli_database, fresh_contact)) is False

    def test_a_company_only_task_whose_contact_is_enrolled_says_close_it(
        self,
        fresh_contact: str,
        cli_database: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        hubspot, _ = _hand_worked_portal(fresh_contact, monkeypatch)
        hubspot.link_company(fresh_contact, "7000000001")
        duplicate = hubspot.add_hand_task("Follow up with APS FireCo", company="7000000001")
        roster = _roster_file(tmp_path, (fresh_contact, "adopt"))
        assert main(["cadence", "adopt", "--roster", str(roster)]) == 0
        capsys.readouterr()

        assert main(["cadence", "adopt", "--dry-run"]) == 0

        out = capsys.readouterr().out
        assert f"contact {fresh_contact}: already has a cadence — skipped" in out
        assert (
            f'task {duplicate} "Follow up with APS FireCo" is on company 7000000001, whose contact '
            f"{fresh_contact} is already in the cadence — close this task by hand"
        ) in out

    def test_a_malformed_roster_is_one_error_line(
        self, cli_database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        roster = tmp_path / "adopt.toml"
        roster.write_text("[[prospect]\n", encoding="utf-8")

        assert main(["cadence", "adopt", "--roster", str(roster), "--dry-run"]) == 1

        err = capsys.readouterr().err
        assert f"error: roster {roster}: not valid TOML" in err
        assert "Traceback" not in err
