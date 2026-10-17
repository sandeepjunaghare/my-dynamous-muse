"""The Friday report: notes counted by prefix across pages, the week's runs, M6 from the ledger.

Real Postgres (rolled back) for the runs and the ledger; :class:`MockPortal` for the notes search,
so every request body is asserted and nothing reaches the network.
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.cost import RunCost
from app.promotion.client import HubSpotClient
from app.promotion.models import Promotion
from app.promotion.schemas import PROMOTE_COUNTS, ReviewTally
from app.promotion.service import PromotionService
from app.runs.exceptions import WeeklyRunError
from app.runs.models import WeeklyRun
from app.runs.report import MAX_PAGES, WeeklyReportService
from app.runs.schemas import WeeklyReport, WeeklyRunStatus
from app.sourcing.schemas import RunOutcome, SourcingRunResponse
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.promotion.builders import a_promotable
from tests.promotion.conftest import MockPortal, make_client
from tests.promotion.scenario import a_finished_run
from tests.qualification.builders import a_body_with, an_active_manifest_with
from tests.sourcing.builders import a_brief

pytestmark = requires_db

WEEK = date(2026, 10, 12)
"""Monday. In CDT (UTC-5) the week runs 2026-10-12T05:00Z to 2026-10-19T05:00Z."""
START = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
END = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)
NOTES = ("POST", "/notes/search")


def _page(texts: Sequence[str | None], *, after: str | None = None) -> dict[str, object]:
    page: dict[str, object] = {
        "total": len(texts),
        "results": [
            {"id": str(index), "properties": {"hs_body_preview": text, "hs_timestamp": "x"}}
            for index, text in enumerate(texts)
        ],
    }
    if after is not None:
        page["paging"] = {"next": {"after": after, "link": "https://api.hubapi.com/next"}}
    return page


def _pages(*pages: dict[str, object]) -> MockPortal:
    served = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        page = pages[min(served["n"], len(pages) - 1)]
        served["n"] += 1
        return httpx.Response(200, json=page)

    return MockPortal([(*NOTES, respond)])


async def _build(
    session: AsyncSession, portal: MockPortal, week: date | None = WEEK
) -> tuple[WeeklyReport, HubSpotClient]:
    client = make_client(portal)
    service = WeeklyReportService(
        session,
        hubspot=lambda: client,
        clock=lambda: datetime(2026, 10, 16, 15, 0, tzinfo=UTC),
    )
    return await service.build(week), client


async def a_sourcing_run(session: AsyncSession, counts: dict[str, int]) -> SourcingRunResponse:
    manifest = await an_active_manifest_with(session, a_body_with())
    service = SourcingService(session)
    run = await service.start_run(a_brief(manifest.vertical))
    return await service.finish_run(run.id, RunOutcome.completed, counts=counts, cost=RunCost())


async def a_weekly_run(
    session: AsyncSession,
    *,
    requested_at: datetime,
    sourcing_run_id: UUID | None = None,
    minutes: float | None = None,
    status: WeeklyRunStatus = WeeklyRunStatus.completed,
    promotion_counts: dict[str, int] | None = None,
) -> WeeklyRun:
    """A finished weekly row with chosen timestamps; ``minutes`` is M7 (``None``: not promoted)."""
    promoted = None if minutes is None else requested_at + timedelta(minutes=minutes)
    row = WeeklyRun(
        id=uuid4(),
        vertical="freight",
        status=status.value,
        requested_at=requested_at,
        sourcing_run_id=sourcing_run_id,
        sourced_at=promoted,
        promoted_at=promoted,
        finished_at=requested_at + timedelta(minutes=(minutes or 0) + 1),
        promotion_counts=(
            promotion_counts
            if promotion_counts is not None
            else ({name: 0 for name in PROMOTE_COUNTS} if minutes is not None else {})
        ),
    )
    session.add(row)
    await session.commit()
    return row


class TestNotes:
    async def test_notes_are_counted_by_prefix_across_pages(self, db_session: AsyncSession) -> None:
        portal = _pages(
            _page(
                ["🚪 Door knock — 9/29/2026", "Owner conversation: Paul", "Cold call - no answer"],
                after="200",
            ),
            _page(["Door-knock visit, 9/24/26", "📅 Assessment booked — Acme", None]),
        )

        report, client = await _build(db_session, portal)
        await client.aclose()

        numbers = {number.name: number.value for number in report.playbook}
        assert numbers == {
            "doors knocked": 2,
            "owner conversations": 1,
            "assessments booked": 1,
            "paid assessments sold": None,
        }
        assert report.unclassified_touches == 2
        bodies = portal.bodies(*NOTES)
        assert len(bodies) == 2
        assert "after" not in bodies[0]
        assert bodies[1]["after"] == "200"

    async def test_the_search_asks_for_the_dallas_week_in_epoch_ms(
        self, db_session: AsyncSession
    ) -> None:
        portal = _pages(_page([]))

        report, client = await _build(db_session, portal)
        await client.aclose()

        [body] = portal.bodies(*NOTES)
        start_ms = str(int(START.timestamp()) * 1000)
        end_ms = str(int(END.timestamp()) * 1000)
        assert body["filterGroups"] == [
            {
                "filters": [
                    {"propertyName": "hs_timestamp", "operator": "GTE", "value": start_ms},
                    {"propertyName": "hs_timestamp", "operator": "LT", "value": end_ms},
                ]
            }
        ]
        assert body["properties"] == ["hs_body_preview", "hs_timestamp"]
        assert body["limit"] == 200
        assert (report.week_start, report.week_end) == (WEEK, date(2026, 10, 18))
        assert report.timezone == "America/Chicago"

    async def test_no_week_given_is_the_week_containing_now(self, db_session: AsyncSession) -> None:
        report, client = await _build(db_session, _pages(_page([])), week=None)
        await client.aclose()

        assert report.week_start == WEEK

    async def test_paid_sold_is_not_configured_never_zero(self, db_session: AsyncSession) -> None:
        report, client = await _build(db_session, _pages(_page([])))
        await client.aclose()

        paid = report.playbook[-1]
        assert (paid.name, paid.value, paid.configured) == ("paid assessments sold", None, False)
        assert report.model_dump(mode="json")["playbook"][-1]["value"] is None

    async def test_an_empty_week_is_real_zeros(self, db_session: AsyncSession) -> None:
        report, client = await _build(db_session, _pages(_page([])))
        await client.aclose()

        assert [n.value for n in report.playbook[:3]] == [0, 0, 0]
        assert all(n.configured for n in report.playbook[:3])
        assert report.unclassified_touches == 0
        assert report.runs == ()

    async def test_past_the_page_cap_it_refuses_rather_than_truncate(
        self, db_session: AsyncSession
    ) -> None:
        portal = _pages(_page(["Door knock"], after="next"))

        with pytest.raises(WeeklyRunError, match="4,000 notes"):
            await _build(db_session, portal)

        assert portal.count("POST", "/notes/search") == MAX_PAGES

    async def test_no_note_text_reaches_a_log_line(self, db_session: AsyncSession) -> None:
        portal = _pages(_page(["Owner conversation: the CFO is leaving in March"]))
        with capture_logs() as captured:
            _, client = await _build(db_session, portal)
        await client.aclose()

        assert "CFO" not in json.dumps(captured, default=str)
        [read] = [entry for entry in captured if entry["event"] == "runs.report.notes_read"]
        assert read["owner_conversation"] == 1


class TestRuns:
    async def test_the_weeks_runs_with_m7_cost_funnel_and_promotion(
        self, db_session: AsyncSession
    ) -> None:
        fast_sourcing = await a_sourcing_run(db_session, {"batched": 5, "places_verified": 3})
        slow_sourcing = await a_sourcing_run(db_session, {"batched": 0})
        fast = await a_weekly_run(
            db_session,
            requested_at=START + timedelta(hours=6),
            sourcing_run_id=fast_sourcing.id,
            minutes=42,
            promotion_counts={**{n: 0 for n in PROMOTE_COUNTS}, "created": 2},
        )
        slow = await a_weekly_run(
            db_session,
            requested_at=START + timedelta(days=2),
            sourcing_run_id=slow_sourcing.id,
            minutes=75,
            status=WeeklyRunStatus.degraded,
        )
        await a_weekly_run(db_session, requested_at=START - timedelta(minutes=1))
        await a_weekly_run(db_session, requested_at=END)

        report, client = await _build(db_session, _pages(_page([])))
        await client.aclose()

        assert [summary.run.id for summary in report.runs] == [fast.id, slow.id]
        first, second = report.runs
        assert (first.within_target, second.within_target) == (True, False)
        assert first.run.brief_to_hubspot_minutes == pytest.approx(42)
        assert first.total_usd == 0
        assert first.funnel["batched"] == 5
        assert first.funnel["places_verified"] == 3
        assert first.funnel["routing_clustered"] is None
        assert second.funnel["batched"] == 0
        assert first.promotion["created"] == 2
        assert first.promotion["refused_contradicted"] == 0
        assert first.run.sourcing is not None
        assert report.target_minutes == 60

    async def test_a_run_that_never_reached_sourcing_or_promotion(
        self, db_session: AsyncSession
    ) -> None:
        await a_weekly_run(
            db_session, requested_at=START + timedelta(hours=1), status=WeeklyRunStatus.failed
        )

        report, client = await _build(db_session, _pages(_page([])))
        await client.aclose()

        [summary] = report.runs
        assert summary.within_target is None
        assert summary.total_usd is None
        assert set(summary.funnel.values()) == {None}
        assert summary.promotion == {}
        assert summary.review == ReviewTally()


class TestReviewTally:
    async def test_decisions_are_tallied_per_claiming_run(self, db_session: AsyncSession) -> None:
        statuses = {
            "1000001": "pending_review",
            "1000002": "pending_review",
            "1000003": "enrolling",
            "1000004": "enrolled",
            "1000005": "rejected",
            "1000006": "withdrawn",
            "1000007": "pending_create",
            "1000008": "duplicate",
        }
        scenario = await a_finished_run(db_session, [a_promotable(rid) for rid in statuses])
        empty = await a_sourcing_run(db_session, {})
        for index, (registry_id, status) in enumerate(statuses.items()):
            reviewed = status not in {"pending_create", "duplicate"}
            db_session.add(
                Promotion(
                    vertical=scenario.vertical,
                    registry_id=registry_id,
                    run_id=scenario.run.id,
                    candidate_id=scenario.candidate(registry_id).id,
                    status=status,
                    company_key=f"https://example.test/{registry_id}",
                    hubspot_company_id=f"70{index}" if reviewed else None,
                    hubspot_contact_id=f"40{index}" if reviewed else None,
                    matched_company_id="999" if status == "duplicate" else None,
                    enrolled_at=datetime.now(UTC) if status == "enrolled" else None,
                )
            )
        await db_session.commit()

        tallies = await PromotionService(db_session).review_tally([scenario.run.id, empty.id])

        assert tallies[scenario.run.id] == ReviewTally(
            pending=2, accepted=2, rejected=1, withdrawn=1
        )
        assert tallies[empty.id] == ReviewTally()

    async def test_the_report_carries_the_tally(self, db_session: AsyncSession) -> None:
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        db_session.add(
            Promotion(
                vertical=scenario.vertical,
                registry_id="1000001",
                run_id=scenario.run.id,
                candidate_id=scenario.candidate("1000001").id,
                status="rejected",
                company_key="https://example.test/1000001",
                hubspot_company_id="7001",
                hubspot_contact_id="4001",
            )
        )
        await db_session.commit()
        await a_weekly_run(
            db_session,
            requested_at=START + timedelta(hours=3),
            sourcing_run_id=scenario.run.id,
            minutes=10,
        )

        report, client = await _build(db_session, _pages(_page([])))
        await client.aclose()

        assert report.runs[0].review == ReviewTally(rejected=1)

    async def test_no_runs_reads_nothing(self, db_session: AsyncSession) -> None:
        assert await PromotionService(db_session).review_tally([]) == {}
