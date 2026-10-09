"""The HTTP surface: the sync trigger, the overdue view, and the route that deliberately is not."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CadencePosition
from app.cadence.repository import CadenceRepository
from app.cadence.routes import get_cadence_service
from app.cadence.service import CadenceService
from app.main import app
from tests.cadence.conftest import CONTACT, START, FakeClock, FakeHubSpot
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def routed(client_with_db: AsyncClient, service: CadenceService) -> AsyncClient:
    """The app, with the cadence service bound to the fake portal and the hand-moved clock.

    ``client_with_db`` clears every dependency override on teardown, this one included.
    """
    app.dependency_overrides[get_cadence_service] = lambda: service
    return client_with_db


class TestSync:
    async def test_post_sync_advances_and_reports(
        self,
        routed: AsyncClient,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))

        response = await routed.post("/cadence/sync")

        assert response.status_code == 200
        body = response.json()
        assert (body["checked"], body["touches_closed"], body["tasks_created"]) == (1, 1, 1)
        assert body["overdue"] == []

    async def test_overdue_touches_come_back_in_the_sync_report(
        self, routed: AsyncClient, service: CadenceService, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        clock.advance(days=3)

        body = (await routed.post("/cadence/sync")).json()

        late = [(o["hubspot_contact_id"], o["touch"], o["days_overdue"]) for o in body["overdue"]]
        assert late == [(CONTACT, "call", 3)]


class TestOverdue:
    async def test_get_overdue_lists_late_touches(
        self, routed: AsyncClient, service: CadenceService, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        clock.advance(days=1)

        response = await routed.get("/cadence/overdue")

        assert response.status_code == 200
        assert [o["hubspot_contact_id"] for o in response.json()] == [CONTACT]


class TestBoundaries:
    async def test_there_is_no_enrol_route(self, routed: AsyncClient) -> None:
        """Enrolling without T13's reconstruction would reset a cycle. No door for it."""
        for path in ("/cadence", "/cadence/enrol", "/cadence/enroll"):
            assert (await routed.post(path)).status_code in {404, 405}

    async def test_sync_without_a_token_is_a_structured_error_not_a_traceback(
        self,
        client_with_db: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
        await CadenceRepository(db_session).create(
            contact_id=CONTACT,
            company_id=None,
            owner_id=None,
            position=CadencePosition.first(),
            task_id="88000001",
            due_at=START,
            anchor_at=START,
            enrolled_at=START,
        )

        response = await client_with_db.post("/cadence/sync")

        assert response.status_code == 500
        assert response.json()["code"] == "configuration_error"
