"""The weekly run end to end: start's refusals, execute's outcomes, the guard and the reaper.

Real Postgres (rolled back), fake stages, and :class:`FakeCrm` for promotion, so the run promotes
for real against the ledger without touching the portal.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from app.manifests.exceptions import ActiveManifestNotFoundError
from app.promotion.exceptions import HubSpotAuthError
from app.promotion.schemas import PROMOTE_COUNTS, PromotionReport
from app.promotion.service import PromotionService
from app.runs.exceptions import WeeklyRunError, WeeklyRunInProgressError, WeeklyRunNotFoundError
from app.runs.models import WeeklyRun
from app.runs.repository import WeeklyRunRepository
from app.runs.schemas import WeeklyRunRequest, WeeklyRunResponse, WeeklyRunStatus
from app.runs.service import WeeklyRunService
from app.sourcing.exceptions import SourceUnavailableError
from app.sourcing.pipeline import configured_stages
from app.sourcing.schemas import RunStatus
from app.tools.verify_business import VerifyBusinessStage
from tests.conftest import SessionFactory, requires_db
from tests.promotion.builders import a_promotable
from tests.promotion.crm import FakeCrm
from tests.runs.conftest import MakeService, Raises, Records, an_active_vertical
from tests.sourcing.builders import a_fire_body, an_active_manifest_with

pytestmark = requires_db


def _report(run_id: UUID, *, stopped: str | None = None, **counts: int) -> PromotionReport:
    return PromotionReport(
        run_id=run_id,
        vertical="freight",
        dry_run=False,
        counts={name: counts.get(name, 0) for name in PROMOTE_COUNTS},
        created=(),
        duplicates=(),
        failures=(),
        missing_field_tally={},
        stopped=stopped,
    )


def _stub_promote(monkeypatch: pytest.MonkeyPatch, outcome: PromotionReport | Exception) -> None:
    async def promote_run(
        self: PromotionService, run_id: UUID, *, dry_run: bool = False
    ) -> PromotionReport:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.model_copy(update={"run_id": run_id})

    monkeypatch.setattr(PromotionService, "promote_run", promote_run)


async def _rows(factory: SessionFactory, vertical: str) -> int:
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(WeeklyRun).where(WeeklyRun.vertical == vertical)
        )
    return count or 0


def _ordered(run: WeeklyRunResponse) -> bool:
    stamps = [run.requested_at, run.sourced_at, run.promoted_at, run.finished_at]
    present = [stamp for stamp in stamps if stamp is not None]
    return present == sorted(present)


class TestHappyPath:
    async def test_it_sources_promotes_and_completes(
        self, session_factory: SessionFactory, make_service: MakeService, crm: FakeCrm
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records([a_promotable("1000001")]))

        run = await service.run(WeeklyRunRequest(vertical=vertical))

        assert run.status is WeeklyRunStatus.completed
        assert run.status_detail is None
        assert run.sourcing is not None and run.sourcing.status is RunStatus.completed
        assert run.sourcing_run_id == run.sourcing.id
        assert run.sourcing.counts == {"batched": 1}
        assert run.promotion_counts["created"] == 1
        assert set(run.promotion_counts) == set(PROMOTE_COUNTS)
        assert len(crm.companies) == 1
        assert None not in (run.sourced_at, run.promoted_at, run.finished_at)
        assert _ordered(run)
        assert run.brief_to_hubspot_minutes is not None

    async def test_a_dry_run_writes_nothing_and_still_measures_m7(
        self, session_factory: SessionFactory, make_service: MakeService, crm: FakeCrm
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records([a_promotable("1000001")]))

        run = await service.run(WeeklyRunRequest(vertical=vertical, dry_run=True))

        assert run.status is WeeklyRunStatus.completed
        assert run.dry_run
        assert crm.writes() == 0
        assert run.promoted_at is not None

    async def test_an_empty_batch_completes_with_zeros(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)

        run = await make_service(Records()).run(WeeklyRunRequest(vertical=vertical))

        assert run.status is WeeklyRunStatus.completed
        assert run.sourcing is not None and run.sourcing.counts == {"batched": 0}
        assert run.promotion_counts["considered"] == 0

    async def test_get_returns_the_run_with_its_sourcing_run(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        run = await service.run(WeeklyRunRequest(vertical=vertical))

        fetched = await service.get(run.id)

        assert fetched.status is WeeklyRunStatus.completed
        assert fetched.sourcing is not None and fetched.sourcing.id == run.sourcing_run_id

    async def test_get_an_unknown_run(self, make_service: MakeService) -> None:
        with pytest.raises(WeeklyRunNotFoundError):
            await make_service().get(uuid4())


class TestDegraded:
    async def test_degraded_sourcing_still_promotes(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records([a_promotable("1000001")], degraded_reason="places: no key"))

        run = await service.run(WeeklyRunRequest(vertical=vertical, allow_degraded=True))

        assert run.status is WeeklyRunStatus.degraded
        assert run.status_detail == "sourcing degraded: search_registry: places: no key"
        assert run.promoted_at is not None
        assert run.promotion_counts["created"] == 1

    async def test_a_stopped_promotion_is_degraded(
        self,
        session_factory: SessionFactory,
        make_service: MakeService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        _stub_promote(monkeypatch, _report(uuid4(), stopped="the daily quota is spent"))

        run = await make_service(Records()).run(WeeklyRunRequest(vertical=vertical))

        assert run.status is WeeklyRunStatus.degraded
        assert run.status_detail == "promotion stopped: the daily quota is spent"
        assert run.promotion_stopped == "the daily quota is spent"

    async def test_promotion_failures_are_degraded_and_every_reason_is_kept(
        self,
        session_factory: SessionFactory,
        make_service: MakeService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        _stub_promote(monkeypatch, _report(uuid4(), considered=3, created=1, failed=2))

        run = await make_service(Records(degraded_reason="qcmobile: no webKey")).run(
            WeeklyRunRequest(vertical=vertical, allow_degraded=True)
        )

        assert run.status is WeeklyRunStatus.degraded
        assert run.status_detail == (
            "sourcing degraded: search_registry: qcmobile: no webKey; 2 promotion(s) failed"
        )
        assert run.promotion_counts["failed"] == 2


class TestFailed:
    async def test_a_failed_sourcing_run_promotes_nothing_and_is_recorded(
        self,
        session_factory: SessionFactory,
        make_service: MakeService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        promoted: list[UUID] = []

        async def spy(
            self: PromotionService, run_id: UUID, *, dry_run: bool = False
        ) -> PromotionReport:
            promoted.append(run_id)
            return _report(run_id)

        monkeypatch.setattr(PromotionService, "promote_run", spy)

        run = await make_service(Records(), Raises(RuntimeError("places is down"))).run(
            WeeklyRunRequest(vertical=vertical)
        )

        assert run.status is WeeklyRunStatus.failed
        assert run.status_detail == "sourcing failed: RuntimeError: places is down"
        assert promoted == []
        assert run.sourcing is not None and run.sourcing.status is RunStatus.failed
        assert run.sourcing_run_id == run.sourcing.id
        assert (run.sourced_at, run.promoted_at) == (None, None)
        assert run.finished_at is not None

    async def test_a_promotion_that_raises_is_failed_after_sourcing(
        self,
        session_factory: SessionFactory,
        make_service: MakeService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        _stub_promote(monkeypatch, HubSpotAuthError("the token was refused"))

        run = await make_service(Records()).run(WeeklyRunRequest(vertical=vertical))

        assert run.status is WeeklyRunStatus.failed
        assert run.status_detail == "promotion failed: HubSpotAuthError: the token was refused"
        assert run.sourced_at is not None
        assert run.promoted_at is None
        assert run.promotion_counts == {}

    async def test_a_manifest_deactivated_after_start_fails_the_run_quietly(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        started = await service.start(WeeklyRunRequest(vertical=vertical))
        async with session_factory() as session:
            await session.execute(
                text("update vertical_manifest set status = 'superseded' where vertical = :v"),
                {"v": vertical},
            )
            await session.commit()

        run = await service.execute(started.id)

        assert run.status is WeeklyRunStatus.failed
        assert run.status_detail is not None
        assert run.status_detail.startswith("sourcing failed: ActiveManifestNotFoundError")
        assert run.sourcing_run_id is None

    async def test_cancellation_is_recorded_then_re_raised(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records(), Raises(asyncio.CancelledError()))
        started = await service.start(WeeklyRunRequest(vertical=vertical))

        with pytest.raises(asyncio.CancelledError):
            await service.execute(started.id)

        run = await service.get(started.id)
        assert run.status is WeeklyRunStatus.failed
        assert run.status_detail == "sourcing failed: CancelledError: "

    async def test_a_finished_run_is_not_executed_again(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        run = await service.run(WeeklyRunRequest(vertical=vertical))

        with pytest.raises(WeeklyRunError, match="only a running weekly run executes"):
            await service.execute(run.id)


class TestStartRefuses:
    async def test_a_second_start_while_one_runs(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        first = await service.start(WeeklyRunRequest(vertical=vertical))

        with pytest.raises(WeeklyRunInProgressError, match=str(first.id)) as refused:
            await service.start(WeeklyRunRequest(vertical=vertical))

        assert refused.value.status_code == 409
        assert await _rows(session_factory, vertical) == 1

    async def test_the_guard_is_global_not_per_vertical(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        service = make_service(Records())
        await service.start(WeeklyRunRequest(vertical=await an_active_vertical(session_factory)))

        with pytest.raises(WeeklyRunInProgressError):
            await service.start(
                WeeklyRunRequest(vertical=await an_active_vertical(session_factory))
            )

    async def test_a_new_start_succeeds_once_the_first_finishes(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        first = await service.start(WeeklyRunRequest(vertical=vertical))
        await service.execute(first.id)

        second = await service.start(WeeklyRunRequest(vertical=vertical))

        assert second.status is WeeklyRunStatus.running

    async def test_a_stale_run_is_reaped_and_the_next_one_starts(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        service = make_service(Records())
        stale = await service.start(WeeklyRunRequest(vertical=vertical))
        async with session_factory() as session:
            await session.execute(
                text(
                    "update weekly_run set requested_at = now() - interval '3 hours' where id = :id"
                ),
                {"id": stale.id},
            )
            await session.commit()

        fresh = await service.start(WeeklyRunRequest(vertical=vertical))

        reaped = await service.get(stale.id)
        assert reaped.status is WeeklyRunStatus.failed
        assert reaped.status_detail == "abandoned: still running after 2h"
        assert fresh.status is WeeklyRunStatus.running

    async def test_no_active_manifest_writes_no_row(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        with pytest.raises(ActiveManifestNotFoundError):
            await make_service(Records()).start(WeeklyRunRequest(vertical="no_such_vertical"))

        assert await _rows(session_factory, "no_such_vertical") == 0

    async def test_a_source_that_cannot_run_is_refused_before_any_row(
        self, session_factory: SessionFactory, crm: FakeCrm
    ) -> None:
        """Fire declares Places; with no key and no ``allow_degraded``, the preflight refuses."""
        async with session_factory() as session:
            _, vertical = await an_active_manifest_with(session, a_fire_body())
            await session.commit()
        service = WeeklyRunService(
            session_factory,
            stages=lambda size, degraded: configured_stages(
                (VerifyBusinessStage(),), batch_size=size, allow_degraded=degraded
            ),
        )

        with pytest.raises(SourceUnavailableError):
            await service.start(WeeklyRunRequest(vertical=vertical))

        assert await _rows(session_factory, vertical) == 0
        allowed = await service.start(WeeklyRunRequest(vertical=vertical, allow_degraded=True))
        assert allowed.allow_degraded


class TestRepository:
    async def test_finish_is_a_compare_and_set(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        run = await make_service(Records()).run(WeeklyRunRequest(vertical=vertical))

        async with session_factory() as session:
            late = await WeeklyRunRepository(session).finish(
                run.id, WeeklyRunStatus.failed, detail="late"
            )

        assert late is None

    async def test_requested_between_is_half_open_and_ordered(
        self, session_factory: SessionFactory, make_service: MakeService
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        run = await make_service(Records()).run(WeeklyRunRequest(vertical=vertical))

        async with session_factory() as session:
            repository = WeeklyRunRepository(session)
            inside = await repository.requested_between(
                run.requested_at, run.requested_at.replace(year=run.requested_at.year + 1)
            )
            before = await repository.requested_between(
                run.requested_at.replace(year=run.requested_at.year - 1), run.requested_at
            )

        assert run.id in [row.id for row in inside]
        assert run.id not in [row.id for row in before]
