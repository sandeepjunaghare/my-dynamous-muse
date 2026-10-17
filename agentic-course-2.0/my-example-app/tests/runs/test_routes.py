"""``POST /runs``, ``GET /runs/{id}`` and ``GET /runs/weekly-report`` over HTTP.

**Background tasks under test.** ``httpx.ASGITransport`` awaits the whole ASGI call, and Starlette
runs a response's background tasks inside that call, after the body is sent. So here the ``POST``
returns once the run has finished, and the next ``GET`` sees the outcome. In production the 202
goes back first and the run continues in the process; that ordering is FastAPI's, not ours.
"""

from collections.abc import AsyncGenerator
from datetime import date
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.runs.routes import get_weekly_report_service, get_weekly_run_service
from app.runs.schemas import PlaybookNumber, WeeklyReport, WeeklyRunRequest
from app.runs.service import WeeklyRunService
from tests.conftest import SessionFactory, requires_db
from tests.runs.conftest import MakeService, Records, an_active_vertical


@pytest.fixture
def service(make_service: MakeService) -> WeeklyRunService:
    return make_service(Records())


@pytest.fixture
async def api() -> AsyncGenerator[AsyncClient, None]:
    """The app as it is wired, apart from whatever a test overrides. No database is touched."""
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def db_api(api: AsyncClient, service: WeeklyRunService) -> AsyncClient:
    """The client with the weekly-run service on the rolled-back test connection and fake CRM."""
    app.dependency_overrides[get_weekly_run_service] = lambda: service
    return api


@requires_db
class TestStart:
    async def test_202_running_then_the_finished_run(
        self, db_api: AsyncClient, session_factory: SessionFactory
    ) -> None:
        vertical = await an_active_vertical(session_factory)

        accepted = await db_api.post("/runs", json={"vertical": vertical, "dry_run": True})

        assert accepted.status_code == 202
        body = accepted.json()
        assert body["status"] == "running"
        assert body["dry_run"] is True
        finished = await db_api.get(f"/runs/{body['id']}")
        assert finished.status_code == 200
        assert finished.json()["status"] == "completed"
        assert finished.json()["sourcing"]["counts"] == {"batched": 0}
        assert finished.json()["brief_to_hubspot_minutes"] is not None

    async def test_409_while_a_run_is_in_progress(
        self, db_api: AsyncClient, service: WeeklyRunService, session_factory: SessionFactory
    ) -> None:
        vertical = await an_active_vertical(session_factory)
        running = await service.start(WeeklyRunRequest(vertical=vertical))

        refused = await db_api.post("/runs", json={"vertical": vertical})

        assert refused.status_code == 409
        assert refused.json()["code"] == "weekly_run_in_progress"
        assert str(running.id) in refused.json()["detail"]

    async def test_404_without_an_active_manifest(self, db_api: AsyncClient) -> None:
        refused = await db_api.post("/runs", json={"vertical": "no_such_vertical"})

        assert refused.status_code == 404
        assert refused.json()["code"] == "active_manifest_not_found"

    async def test_404_for_an_unknown_run(self, db_api: AsyncClient) -> None:
        missing = await db_api.get(f"/runs/{uuid4()}")

        assert missing.status_code == 404
        assert missing.json()["code"] == "weekly_run_not_found"


class TestValidation:
    async def test_a_bad_body_is_422(self, api: AsyncClient) -> None:
        response = await api.post("/runs", json={"vertical": "freight", "batch_size": 0})
        assert response.status_code == 422

    async def test_a_malformed_id_is_422(self, api: AsyncClient) -> None:
        response = await api.get("/runs/not-a-uuid")
        assert response.status_code == 422


class _StubReport:
    def __init__(self) -> None:
        self.weeks: list[date | None] = []

    async def build(self, week_of: date | None = None) -> WeeklyReport:
        self.weeks.append(week_of)
        return WeeklyReport(
            week_start=date(2026, 10, 12),
            week_end=date(2026, 10, 18),
            timezone="America/Chicago",
            playbook=(
                PlaybookNumber(name="doors knocked", value=4, configured=True),
                PlaybookNumber(name="owner conversations", value=1, configured=True),
                PlaybookNumber(name="assessments booked", value=0, configured=True),
                PlaybookNumber(name="paid assessments sold", value=None, configured=False),
            ),
            unclassified_touches=3,
            runs=(),
            target_minutes=60,
        )


class TestWeeklyReport:
    async def test_the_report_for_a_given_week(self, api: AsyncClient) -> None:
        stub = _StubReport()
        app.dependency_overrides[get_weekly_report_service] = lambda: stub

        response = await api.get("/runs/weekly-report", params={"week": "2026-10-14"})

        assert response.status_code == 200
        body = response.json()
        assert [n["value"] for n in body["playbook"]] == [4, 1, 0, None]
        assert body["playbook"][3]["configured"] is False
        assert body["unclassified_touches"] == 3
        assert stub.weeks == [date(2026, 10, 14)]

    async def test_it_is_not_taken_for_a_run_id(self, api: AsyncClient) -> None:
        """Declared before ``/{run_id}``, so ``weekly-report`` never parses as a UUID."""
        stub = _StubReport()
        app.dependency_overrides[get_weekly_report_service] = lambda: stub

        response = await api.get("/runs/weekly-report")

        assert response.status_code == 200
        assert stub.weeks == [None]
