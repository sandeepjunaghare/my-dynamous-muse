"""The cadence end to end: enrol, sync, the three-cycle walk, park — against a fake portal.

Every HubSpot call goes through :class:`FakeHubSpot`, which is built on T3's ``MockPortal`` and
raises on any request it does not model. The clock is moved by hand, so twelve days of cadence run
in milliseconds.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.exceptions import AlreadyEnrolledError
from app.cadence.machine import CadencePosition, CadenceStatus, Touch, local_date
from app.cadence.repository import CadenceRepository
from app.cadence.schemas import ActivityKind
from app.cadence.service import CadenceService
from app.promotion.client import HubSpotClient
from app.promotion.exceptions import HubSpotResponseError
from tests.cadence.conftest import COMPANY, CONTACT, FakeClock, FakeHubSpot
from tests.conftest import requires_db

pytestmark = requires_db

EXPECTED_SUBJECTS = [
    f"Cadence {cycle}/3 · {touch}"
    for cycle in (1, 2, 3)
    for touch in ("call", "voicemail", "email draft (send by hand)")
]


async def _state(session: AsyncSession, contact: str = CONTACT) -> tuple[str, int, str, str | None]:
    state = await CadenceRepository(session).get_by_contact(contact)
    assert state is not None
    return state.status, state.cycle, state.touch, state.hubspot_task_id


class TestEnrol:
    async def test_enrolment_creates_the_first_call_task_and_the_schedule(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        enrolled = await service.enrol(CONTACT, company_id=COMPANY, owner_id="77001")

        assert enrolled.status is CadenceStatus.live
        assert (enrolled.cycle, enrolled.touch) == (1, Touch.call)
        assert enrolled.hubspot_task_id == hubspot.last_task_id()
        assert enrolled.due_at is not None
        assert local_date(enrolled.due_at) == local_date(clock())
        assert hubspot.created_subjects() == ["Cadence 1/3 · call"]
        body = hubspot.created[0]
        assert body["associations"] == [
            {
                "to": {"id": CONTACT},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}],
            },
            {
                "to": {"id": COMPANY},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 192}],
            },
        ]
        properties = body["properties"]
        assert isinstance(properties, dict)
        assert properties["hubspot_owner_id"] == "77001"

    async def test_a_contact_cannot_be_enrolled_twice(
        self, service: CadenceService, hubspot: FakeHubSpot
    ) -> None:
        """Re-enrolling would reset the cycle. Refused before anything reaches HubSpot."""
        await service.enrol(CONTACT)

        with pytest.raises(AlreadyEnrolledError):
            await service.enrol(CONTACT)

        assert len(hubspot.created) == 1

    async def test_the_adoption_seam_starts_where_history_says(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        """T13 passes a reconstructed position; the machine resumes there instead of resetting."""
        due = datetime(2026, 10, 7, 4, 59, tzinfo=UTC)
        enrolled = await service.enrol(
            CONTACT,
            start=CadencePosition(cycle=2, touch=Touch.email),
            anchor_at=clock(),
            due_at=due,
        )

        assert (enrolled.cycle, enrolled.touch, enrolled.due_at) == (2, Touch.email, due)
        assert hubspot.created_types() == ["EMAIL"]

    async def test_a_failed_task_create_writes_no_row(
        self, service: CadenceService, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        """A row with no task would be a prospect nobody is asked to touch."""
        hubspot.fail_creates = 1

        with pytest.raises(HubSpotResponseError):
            await service.enrol(CONTACT)

        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None


class TestIdempotency:
    async def test_a_sync_with_nothing_new_creates_nothing(
        self, service: CadenceService, hubspot: FakeHubSpot
    ) -> None:
        await service.enrol(CONTACT)

        first = await service.sync()
        second = await service.sync()

        for report in (first, second):
            assert (report.checked, report.tasks_created, report.unchanged) == (1, 0, 1)
        assert len(hubspot.created) == 1

    async def test_a_second_sync_the_same_day_schedules_nothing_twice(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        clock.advance(hours=1)
        hubspot.complete_task(hubspot.last_task_id())

        first = await service.sync()
        clock.advance(hours=2)
        second = await service.sync()

        assert first.tasks_created == 1
        assert second.tasks_created == 0
        assert hubspot.created_subjects() == ["Cadence 1/3 · call", "Cadence 1/3 · voicemail"]

    async def test_an_empty_cadence_does_not_need_hubspot(self, db_session: AsyncSession) -> None:
        """No live prospects → no portal call, so a sync with no token configured still runs."""

        def no_portal() -> HubSpotClient:
            raise AssertionError("HubSpot was reached with nothing to sync")

        report = await CadenceService(db_session, hubspot=no_portal).sync()

        assert report.checked == 0


class TestTheThreeCycleWalk:
    async def test_completing_every_task_walks_nine_touches_and_parks(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)

        for _ in range(9):
            clock.advance(hours=1)
            hubspot.complete_task(hubspot.last_task_id())
            await service.sync()
            clock.advance(days=4)

        assert hubspot.created_subjects() == EXPECTED_SUBJECTS
        assert hubspot.created_types() == ["CALL", "CALL", "EMAIL"] * 3
        status, cycle, touch, task_id = await _state(db_session)
        assert (status, cycle, touch, task_id) == ("parked", 3, "email", None)

        after_park = await service.sync()
        assert after_park.checked == 0
        assert len(hubspot.created) == 9

    async def test_the_walk_also_runs_on_logged_activity_alone(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        """The OR half of D5 for every touch: nobody ticks a single task, and it still parks."""
        await service.enrol(CONTACT)
        kinds = [ActivityKind.call, ActivityKind.note, ActivityKind.email] * 3

        superseded: list[str] = []
        for kind in kinds:
            clock.advance(hours=1)
            hubspot.log(kind)
            report = await service.sync()
            superseded.extend(report.open_tasks_superseded)
            clock.advance(days=4)

        assert len(hubspot.created) == 9
        assert (await _state(db_session))[0] == "parked"
        assert len(superseded) == 9, "every open task a logged activity closed is reported"

    async def test_the_next_cycle_waits_four_days_after_the_email(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        for _ in range(3):
            clock.advance(minutes=30)
            hubspot.complete_task(hubspot.last_task_id())
            await service.sync()

        state = await CadenceRepository(db_session).get_by_contact(CONTACT)
        assert state is not None
        assert (state.cycle, state.touch) == (2, "call")
        assert state.due_at is not None
        assert (local_date(state.due_at) - local_date(clock())).days == 4


class TestAlreadyDoneByHand:
    async def test_logged_work_advances_rather_than_re_firing(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        """Call, voicemail and email all done and logged before the sync: no task for any of
        them is created — only cycle two's call, four days out."""
        await service.enrol(CONTACT)
        first_task = hubspot.last_task_id()
        hubspot.log(ActivityKind.call, at=clock.advance(minutes=10))
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=2))
        hubspot.log(ActivityKind.email, at=clock.advance(minutes=30))

        report = await service.sync()

        assert report.touches_closed == 3
        assert report.tasks_created == 1
        assert hubspot.created_subjects() == ["Cadence 1/3 · call", "Cadence 2/3 · call"]
        assert report.open_tasks_superseded == [first_task]
        assert (await _state(db_session))[1:3] == (2, "call")

    async def test_a_ticked_task_and_its_logged_call_close_one_touch_not_two(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.log(ActivityKind.call, at=clock.advance(minutes=10))
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))

        report = await service.sync()

        assert report.touches_closed == 1
        assert hubspot.created_subjects()[-1] == "Cadence 1/3 · voicemail"
        assert report.open_tasks_superseded == []

    async def test_activity_from_before_enrolment_does_not_count(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        hubspot.log(ActivityKind.call, at=clock.advance(days=-3))
        clock.advance(days=3)
        await service.enrol(CONTACT)

        report = await service.sync()

        assert report.touches_closed == 0


class TestOverdue:
    async def test_an_untouched_call_is_surfaced_as_overdue(
        self, service: CadenceService, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT, company_id=COMPANY)
        clock.advance(days=2)

        report = await service.sync()

        assert [touch.hubspot_contact_id for touch in report.overdue] == [CONTACT]
        overdue = report.overdue[0]
        assert (overdue.cycle, overdue.touch, overdue.days_overdue) == (1, Touch.call, 2)
        assert overdue.hubspot_company_id == COMPANY

    async def test_a_touch_due_later_today_is_not_overdue(
        self, service: CadenceService, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        clock.advance(hours=8)  # 17:00 Dallas; due 23:59

        assert (await service.sync()).overdue == []
        assert await service.overdue() == []

    async def test_the_overdue_view_needs_no_hubspot(
        self, service: CadenceService, db_session: AsyncSession, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        clock.advance(days=1)

        def no_portal() -> HubSpotClient:
            raise AssertionError("the overdue view must read only our own schedule")

        overdue = await CadenceService(db_session, hubspot=no_portal, clock=clock).overdue()

        assert [touch.days_overdue for touch in overdue] == [1]


class TestFailures:
    async def test_a_failed_task_create_leaves_the_schedule_alone_and_is_retried(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        first_task = hubspot.last_task_id()
        hubspot.complete_task(first_task, at=clock.advance(minutes=5))
        hubspot.fail_creates = 1

        failed = await service.sync()

        assert [f.hubspot_contact_id for f in failed.failures] == [CONTACT]
        assert failed.tasks_created == 0
        assert await _state(db_session) == ("live", 1, "call", first_task)

        retried = await service.sync()

        assert retried.failures == []
        assert retried.tasks_created == 1
        assert (await _state(db_session))[1:3] == (1, "voicemail")

    async def test_one_prospect_failing_does_not_stop_the_others(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol("400000001")
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))
        await service.enrol("400000002")
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))
        hubspot.fail_creates = 1

        report = await service.sync()

        assert (report.checked, len(report.failures), report.tasks_created) == (2, 1, 1)

    async def test_a_deleted_task_is_reported_not_recreated(
        self, service: CadenceService, hubspot: FakeHubSpot
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.delete_task(hubspot.last_task_id())

        report = await service.sync()

        assert report.missing_tasks == [CONTACT]
        assert report.tasks_created == 0
        assert len(hubspot.created) == 1

    async def test_a_deleted_task_can_still_be_closed_by_logged_activity(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.delete_task(hubspot.last_task_id())
        hubspot.log(ActivityKind.call, at=clock.advance(minutes=3))

        report = await service.sync()

        assert report.missing_tasks == []
        assert report.tasks_created == 1


class TestNothingSends:
    async def test_the_only_writes_a_full_walk_makes_are_task_creates(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        """Spike 4 and the no-autonomous-calling rule, observed on the wire: across a whole
        cadence the portal sees task creates and reads — no email, call or note is ever written."""
        await service.enrol(CONTACT)
        for _ in range(9):
            hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(hours=1))
            await service.sync()

        writes = {
            request.url.path
            for request in hubspot.portal.requests
            if request.method in {"POST", "PATCH", "PUT", "DELETE"}
            and not request.url.path.endswith("/batch/read")
        }
        assert writes == {"/crm/objects/2026-09/tasks"}
