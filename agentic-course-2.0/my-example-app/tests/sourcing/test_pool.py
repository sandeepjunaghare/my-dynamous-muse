"""The backlog (D12) and the run-lifecycle hardening T5 owns, against a real Postgres."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import BillableKind, RunCost
from app.sourcing.exceptions import SourcingRunNotRunningError
from app.sourcing.models import SourcingPool, SourcingRun
from app.sourcing.pool import PoolRepository
from app.sourcing.repository import SourcingRepository
from app.sourcing.schemas import RunCostSummary, RunOutcome, RunStatus, SourcingRunResponse
from app.sourcing.service import SourcingService
from app.sourcing.sources.base import PoolRecord
from app.sourcing.sources.fmcsa import census_pool_record
from tests.conftest import requires_db
from tests.sourcing.builders import a_brief, an_active_manifest
from tests.sourcing.conftest import RUN_CLOCK, census_rows

pytestmark = requires_db

CUTOFF = RUN_CLOCK.date() - timedelta(days=730)
"""2024-10-11: an MCS-150 on or after this is current."""

BEST_FIRST = [
    "usdot:1000001",  # current, principal
    "usdot:1000005",  # current, principal
    "usdot:1000002",  # current, no principal
    "usdot:1000008",  # current, no principal
    "usdot:1000003",  # stale, principal
    "usdot:1000007",  # stale (2024-02-01), principal
    "usdot:1000004",  # no date, no principal
    "usdot:1000006",  # stale, no principal
]
"""The fixture pool in D12's order: current MCS-150, then a named officer, then registry id."""


def pool_records(rows: list[dict[str, str]] | None = None) -> list[PoolRecord]:
    records = [
        census_pool_record(row, source="fmcsa_company_census", retrieved_at=RUN_CLOCK)
        for row in (rows if rows is not None else census_rows())
    ]
    return [record for record in records if record is not None]


async def _run(session: AsyncSession, vertical: str | None = None) -> SourcingRunResponse:
    if vertical is None:
        _, vertical = await an_active_manifest(session)
    return await SourcingService(session).start_run(a_brief(vertical))


async def _finish(session: AsyncSession, run_id: UUID, outcome: RunOutcome) -> None:
    await SourcingService(session).finish_run(run_id, outcome, counts={}, cost=RunCost())


async def _take(
    session: AsyncSession,
    run: SourcingRunResponse,
    size: int,
    records: list[PoolRecord] | None = None,
) -> list[str]:
    selection = await SourcingService(session).refresh_pool_and_select(
        run.id,
        records if records is not None else pool_records(),
        size=size,
        currency_cutoff=CUTOFF,
    )
    return [entry.registry_id for entry in selection.selected]


class TestRefresh:
    async def test_a_second_refresh_changes_only_last_seen(self, db_session: AsyncSession) -> None:
        first = await _run(db_session)
        pool = PoolRepository(db_session)
        assert await pool.refresh(first.vertical, first.id, pool_records()) == 8
        await _finish(db_session, first.id, RunOutcome.completed)

        second = await _run(db_session, first.vertical)
        assert await pool.refresh(second.vertical, second.id, pool_records()) == 0

        entries = (await db_session.scalars(select(SourcingPool))).all()
        mine = [entry for entry in entries if entry.vertical == first.vertical]
        assert len(mine) == 8
        assert {entry.first_seen_run_id for entry in mine} == {first.id}
        assert {entry.last_seen_run_id for entry in mine} == {second.id}

    async def test_a_refresh_never_clears_batched(self, db_session: AsyncSession) -> None:
        first = await _run(db_session)
        await _take(db_session, first, size=2)
        await _finish(db_session, first.id, RunOutcome.completed)

        second = await _run(db_session, first.vertical)
        await PoolRepository(db_session).refresh(second.vertical, second.id, pool_records())
        batched = await db_session.scalars(
            select(SourcingPool.registry_id).where(SourcingPool.batched_run_id == first.id)
        )
        assert sorted(batched.all()) == ["usdot:1000001", "usdot:1000005"]

    async def test_duplicate_records_are_refused_before_any_write(
        self, db_session: AsyncSession
    ) -> None:
        run = await _run(db_session)
        record = pool_records()[0]
        with pytest.raises(ValueError, match="unique by registry_id"):
            await PoolRepository(db_session).refresh(run.vertical, run.id, [record, record])

    async def test_the_cited_registry_id_check_refuses_an_uncited_entry(
        self, db_session: AsyncSession
    ) -> None:
        run = await _run(db_session)
        with pytest.raises(IntegrityError, match="ck_sourcing_pool_registry_id_is_cited"):
            await db_session.execute(
                text(
                    "insert into sourcing_pool (id, vertical, registry_id, fields, has_principal,"
                    " first_seen_run_id, last_seen_run_id) values (gen_random_uuid(), :v, 'x',"
                    " '{}'::jsonb, false, :r, :r)"
                ),
                {"v": run.vertical, "r": run.id},
            )


class TestSelectBatch:
    async def test_best_first(self, db_session: AsyncSession) -> None:
        run = await _run(db_session)
        assert await _take(db_session, run, size=8) == BEST_FIRST

    async def test_a_batch_larger_than_the_pool_takes_what_exists(
        self, db_session: AsyncSession
    ) -> None:
        run = await _run(db_session)
        assert len(await _take(db_session, run, size=50)) == 8

    async def test_an_entry_not_seen_by_this_refresh_is_not_selectable(
        self, db_session: AsyncSession
    ) -> None:
        """It stopped matching the manifest — went inactive, say. It drops out, nothing deleted."""
        first = await _run(db_session)
        await PoolRepository(db_session).refresh(first.vertical, first.id, pool_records())
        await _finish(db_session, first.id, RunOutcome.completed)

        second = await _run(db_session, first.vertical)
        without_alpha = [row for row in census_rows() if row["dot_number"] != "1000001"]
        taken = await _take(db_session, second, size=8, records=pool_records(without_alpha))
        assert "usdot:1000001" not in taken
        assert len(taken) == 7

    @pytest.mark.parametrize(
        ("outcome", "returned"),
        [
            (RunOutcome.failed, True),
            (RunOutcome.completed, False),
            (RunOutcome.degraded, False),
        ],
    )
    async def test_only_a_failed_run_gives_its_batch_back(
        self, db_session: AsyncSession, outcome: RunOutcome, returned: bool
    ) -> None:
        first = await _run(db_session)
        assert await _take(db_session, first, size=2) == BEST_FIRST[:2]
        await _finish(db_session, first.id, outcome)

        second = await _run(db_session, first.vertical)
        expected = BEST_FIRST[:2] if returned else BEST_FIRST[2:4]
        assert await _take(db_session, second, size=2) == expected

    async def test_a_batch_held_by_a_running_run_is_not_given_away(
        self, db_session: AsyncSession
    ) -> None:
        first = await _run(db_session)
        await _take(db_session, first, size=2)
        second = await _run(db_session, first.vertical)
        assert await _take(db_session, second, size=2) == BEST_FIRST[2:4]

    async def test_verticals_have_separate_pools(self, db_session: AsyncSession) -> None:
        one = await _run(db_session)
        other = await _run(db_session)
        assert await _take(db_session, one, size=2) == BEST_FIRST[:2]
        assert await _take(db_session, other, size=2) == BEST_FIRST[:2]


class TestConditionalFinish:
    async def test_a_second_finish_updates_nothing(self, db_session: AsyncSession) -> None:
        run = await _run(db_session)
        repository = SourcingRepository(db_session)
        stale = await repository.get_run(run.id)
        assert stale is not None
        empty = RunCostSummary.from_run_cost(RunCost())

        first = await repository.mark_finished(stale, RunOutcome.completed, {"a": 1}, empty, None)
        second = await repository.mark_finished(stale, RunOutcome.failed, {"b": 2}, empty, "late")

        assert first is not None and first.status == RunStatus.completed.value
        assert second is None
        row = await db_session.scalar(select(SourcingRun).where(SourcingRun.id == run.id))
        assert row is not None and row.status == "completed" and row.counts == {"a": 1}

    async def test_the_service_refuses_when_the_write_loses_the_race(
        self, db_session: AsyncSession
    ) -> None:
        run = await _run(db_session)
        with (
            patch.object(SourcingRepository, "mark_finished", AsyncMock(return_value=None)),
            pytest.raises(SourcingRunNotRunningError),
        ):
            await _finish(db_session, run.id, RunOutcome.completed)

    async def test_finished_at_is_the_database_clock_and_cost_is_kept(
        self, db_session: AsyncSession
    ) -> None:
        run = await _run(db_session)
        cost = RunCost()
        cost.record(BillableKind.places_lookup, 2, Decimal("0.064"))
        finished = await SourcingService(db_session).finish_run(
            run.id, RunOutcome.completed, counts={"batched": 3}, cost=cost
        )
        assert finished.finished_at is not None
        assert finished.finished_at >= finished.started_at
        assert finished.cost.total_usd() == Decimal("0.064")
        assert finished.counts == {"batched": 3}


class TestReaper:
    async def _age(self, session: AsyncSession, run_id: UUID, hours: int) -> None:
        await session.execute(
            update(SourcingRun)
            .where(SourcingRun.id == run_id)
            .values(started_at=text(f"now() - interval '{hours} hours'"))
        )

    async def test_only_stale_running_runs_of_the_vertical_are_reaped(
        self, db_session: AsyncSession
    ) -> None:
        stale = await _run(db_session)
        recent = await _run(db_session, stale.vertical)
        elsewhere = await _run(db_session)
        await self._age(db_session, stale.id, 7)
        await self._age(db_session, elsewhere.id, 7)

        reaped = await SourcingService(db_session).reap_stale_runs(
            stale.vertical, older_than=timedelta(hours=6)
        )

        assert reaped == [stale.id]
        service = SourcingService(db_session)
        reaped_run = await service.get_run(stale.id)
        assert reaped_run.status is RunStatus.failed
        assert reaped_run.status_detail == "abandoned: still running after 6h"
        assert reaped_run.finished_at is not None
        assert (await service.get_run(recent.id)).status is RunStatus.running
        assert (await service.get_run(elsewhere.id)).status is RunStatus.running

    async def test_a_reaped_run_gives_its_batch_back(self, db_session: AsyncSession) -> None:
        killed = await _run(db_session)
        await _take(db_session, killed, size=2)
        await self._age(db_session, killed.id, 7)
        await SourcingService(db_session).reap_stale_runs(
            killed.vertical, older_than=timedelta(hours=6)
        )

        next_run = await _run(db_session, killed.vertical)
        assert await _take(db_session, next_run, size=2) == BEST_FIRST[:2]


def test_the_cutoff_is_two_years_by_days_not_by_calendar() -> None:
    assert date(2024, 2, 29) + timedelta(days=730) == date(2026, 2, 28)
