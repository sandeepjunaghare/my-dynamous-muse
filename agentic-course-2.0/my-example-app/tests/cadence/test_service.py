"""The cadence end to end: enrol, sync, the three-cycle walk, park — against a fake portal.

Every HubSpot call goes through :class:`FakeHubSpot`, which is built on T3's ``MockPortal`` and
raises on any request it does not model. The clock is moved by hand, so twelve days of cadence run
in milliseconds.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NoReturn

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from structlog.testing import capture_logs

from app.cadence.exceptions import (
    AlreadyEnrolledError,
    AlreadyParkedError,
    NotEnrolledError,
    SyncRunningError,
)
from app.cadence.machine import CadencePosition, CadenceStatus, Touch, local_date
from app.cadence.models import CadenceState
from app.cadence.repository import CADENCE_SYNC_LOCK_ID, CadenceRepository
from app.cadence.schemas import ActivityKind, PendingTask, SignalSource, TaskSnapshot
from app.cadence.service import CadenceService
from app.cadence.sync import OutcomeReader
from app.core.config import get_settings
from app.promotion.client import HubSpotClient
from app.promotion.exceptions import HubSpotResponseError
from app.promotion.schemas import HubSpotObject, TaskCreate
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

    async def test_an_anchor_on_an_activity_is_not_credited_again(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        """T13's regression: adoption anchors on the note that closed the last touch. Stored
        without its ref, that note would sort after the anchor and close the next touch too."""
        logged_at = clock()
        note_id = hubspot.log(ActivityKind.note, at=logged_at)
        clock.advance(hours=1)
        await service.enrol(
            CONTACT,
            start=CadencePosition(cycle=1, touch=Touch.voicemail),
            anchor_at=logged_at,
            anchor_ref=f"notes:{note_id}",
        )

        report = await service.sync()

        assert (report.touches_closed, report.tasks_created) == (0, 0)

    async def test_an_interrupted_enrolment_reuses_the_task_it_made(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
    ) -> None:
        """pr-9 L1: HubSpot made the task and lost the answer. The retry finds it by its key
        instead of putting a second one on the founder's list."""
        hubspot.lose_create_responses = 1
        with pytest.raises(HubSpotResponseError):
            await service.enrol(CONTACT)
        made = hubspot.last_task_id()

        with capture_logs() as captured:
            enrolled = await service.enrol(CONTACT)

        assert enrolled.hubspot_task_id == made
        assert hubspot.task_creates_attempted() == 1
        assert any(entry["event"] == "cadence.service.task_reused" for entry in captured)

    async def test_a_concurrent_enrolment_is_already_enrolled_not_an_integrity_error(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr-9 L1: two enrolments race past the check; the loser hears the domain error."""
        await service.enrol(CONTACT)
        first_task = hubspot.last_task_id()
        repository = CadenceRepository(db_session)
        monkeypatch.setattr(service, "_repository", repository)
        real_get = repository.get_by_contact
        calls = {"n": 0}

        async def missed_once(contact_id: str) -> CadenceState | None:
            calls["n"] += 1
            return None if calls["n"] == 1 else await real_get(contact_id)

        monkeypatch.setattr(repository, "get_by_contact", missed_once)

        with pytest.raises(AlreadyEnrolledError) as raised:
            await service.enrol(CONTACT)

        assert await _state(db_session) == ("live", 1, "call", first_task)
        assert hubspot.task_creates_attempted() == 1, "the loser found the task by key"
        assert raised.value.orphan_task_id is None, "a reused task is the winner's, not an orphan"

    async def test_a_losing_enrolment_names_the_task_it_left_open(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr-14 L1: both racers created a task; the loser's is orphaned, and says so."""
        await service.enrol(CONTACT)
        first_task = hubspot.last_task_id()
        repository = CadenceRepository(db_session)
        monkeypatch.setattr(service, "_repository", repository)
        real_get = repository.get_by_contact
        calls = {"n": 0}

        async def missed_once(contact_id: str) -> CadenceState | None:
            calls["n"] += 1
            return None if calls["n"] == 1 else await real_get(contact_id)

        async def not_yet_visible(reader: OutcomeReader, contact_id: str, key: str) -> None:
            return None

        monkeypatch.setattr(repository, "get_by_contact", missed_once)
        monkeypatch.setattr(OutcomeReader, "find_task_by_key", not_yet_visible)

        with capture_logs() as logs, pytest.raises(AlreadyEnrolledError) as raised:
            await service.enrol(CONTACT)

        orphan = hubspot.last_task_id()
        assert orphan != first_task
        assert f"task {orphan}" in raised.value.message
        assert raised.value.orphan_task_id == orphan
        (event,) = [log for log in logs if log["event"] == "cadence.service.task_orphaned"]
        assert (event["contact_id"], event["task_id"]) == (CONTACT, orphan)
        assert await _state(db_session) == ("live", 1, "call", first_task)

    async def test_another_constraint_violation_is_not_called_already_enrolled(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#15: only ``uq_cadence_state_contact`` means "enrolled meanwhile"; others re-raise."""
        monkeypatch.setattr(CadenceRepository, "create", _violates("ck_cadence_state_cycle"))

        with pytest.raises(IntegrityError, match="ck_cadence_state_cycle"):
            await service.enrol(CONTACT)

        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None
        assert hubspot.task_creates_attempted() == 1, "the task was made; a re-run finds it by key"

    async def test_another_constraint_violation_on_parking_is_not_called_already_enrolled(
        self,
        service: CadenceService,
        clock: FakeClock,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            CadenceRepository, "create_parked", _violates("ck_cadence_state_status")
        )

        with pytest.raises(IntegrityError, match="ck_cadence_state_status"):
            await service.adopt_parked(
                CONTACT,
                company_id=None,
                position=CadencePosition.first(),
                anchor_at=clock(),
                anchor_ref=None,
            )

        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None

    async def test_an_adopted_refusal_is_parked_and_never_enrolled(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
    ) -> None:
        parked = await service.adopt_parked(
            CONTACT,
            company_id=COMPANY,
            position=CadencePosition.first(),
            anchor_at=clock(),
            anchor_ref=None,
        )

        assert (parked.status, parked.hubspot_task_id, parked.due_at) == (
            CadenceStatus.parked,
            None,
            None,
        )
        assert hubspot.portal.requests == [], "parking a refusal never reaches HubSpot"
        with pytest.raises(AlreadyEnrolledError):
            await service.enrol(CONTACT)
        with pytest.raises(AlreadyEnrolledError):
            await service.adopt_parked(
                CONTACT,
                company_id=None,
                position=CadencePosition.first(),
                anchor_at=clock(),
                anchor_ref=None,
            )
        assert (await service.sync()).checked == 0


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
        # The intent was committed before the create: the row says "a voicemail task should
        # exist — check before making one", not "still on the call".
        assert await _state(db_session) == ("live", 1, "voicemail", None)
        assert first_task in hubspot.tasks

        retried = await service.sync()

        assert retried.failures == []
        assert retried.tasks_created == 1
        assert retried.pending_tasks_adopted == []
        assert (await _state(db_session))[1:3] == (1, "voicemail")
        assert hubspot.open_tasks() == ["Cadence 1/3 · voicemail"]

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


def _crash_after_create(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient, times: int = 1
) -> None:
    """The task is created in HubSpot, then the database write behind it fails."""
    real = hubspot_client.create_task
    remaining = [times]

    async def create_then_crash(
        task: TaskCreate, *, contact_id: str | None = None, company_id: str | None = None
    ) -> HubSpotObject:
        created = await real(task, contact_id=contact_id, company_id=company_id)
        if remaining[0]:
            remaining[0] -= 1
            raise OperationalError("UPDATE cadence_state", {}, Exception("connection dropped"))
        return created

    monkeypatch.setattr(hubspot_client, "create_task", create_then_crash)


def _failing_for[**P](
    contact_id: str, real: Callable[P, Awaitable[None]]
) -> Callable[P, Awaitable[None]]:
    """Wrap a repository write so it raises a database error for one contact only."""

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
        if any(getattr(arg, "hubspot_contact_id", None) == contact_id for arg in args):
            raise OperationalError("UPDATE cadence_state", {}, Exception("deadlock detected"))
        await real(*args, **kwargs)

    return wrapper


def _violates(constraint: str) -> Callable[..., Awaitable[NoReturn]]:
    """A repository write that fails on ``constraint`` — any constraint but the contact's."""

    async def write(*args: object, **kwargs: object) -> NoReturn:
        raise IntegrityError(
            "INSERT INTO cadence_state",
            {},
            Exception(
                f'new row for relation "cadence_state" violates check constraint "{constraint}"'
            ),
        )

    return write


class TestAmbiguousCreates:
    """H1: a create HubSpot executed but never confirmed must not become a second task."""

    async def test_the_task_carries_its_idempotency_key(
        self, service: CadenceService, hubspot: FakeHubSpot
    ) -> None:
        await service.enrol(CONTACT)

        assert hubspot.created_bodies()[0].endswith(f"Ref: lpe-cadence:{CONTACT}:1-call")

    async def test_a_task_created_but_answered_with_a_500_is_adopted_not_duplicated(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.lose_create_responses = 1

        failed = await service.sync()

        assert [f.hubspot_contact_id for f in failed.failures] == [CONTACT]
        assert hubspot.task_creates_attempted() == 2, "the POST is never retried blindly"
        orphan = hubspot.last_task_id()

        retried = await service.sync()

        assert retried.failures == []
        assert retried.tasks_created == 0
        assert retried.pending_tasks_adopted == [orphan]
        assert hubspot.task_creates_attempted() == 2
        assert hubspot.open_tasks() == ["Cadence 1/3 · voicemail"]
        assert await _state(db_session) == ("live", 1, "voicemail", orphan)

    async def test_a_crash_between_the_create_and_the_commit_does_not_duplicate(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        hubspot_client: HubSpotClient,
        clock: FakeClock,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        _crash_after_create(monkeypatch, hubspot_client)

        crashed = await service.sync()

        assert [f.code for f in crashed.failures] == ["database_error"]
        orphan = hubspot.last_task_id()

        retried = await service.sync()

        assert retried.pending_tasks_adopted == [orphan]
        assert hubspot.open_tasks() == ["Cadence 1/3 · voicemail"]
        assert (await _state(db_session))[3] == orphan

    async def test_an_adopted_task_already_ticked_advances_in_the_same_sync(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        """The founder worked the orphan before the next sync: no nag, no one-sync lag."""
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.lose_create_responses = 1
        await service.sync()
        orphan = hubspot.last_task_id()
        hubspot.complete_task(orphan, at=clock.advance(minutes=30))

        report = await service.sync()

        assert report.pending_tasks_adopted == [orphan]
        assert report.touches_closed == 1
        assert hubspot.open_tasks() == ["Cadence 1/3 · email draft (send by hand)"]


class TestTickThenLogNextTouch:
    """H2 through the real service: the orderings people actually log in."""

    async def test_tick_then_log_the_voicemail_and_email_creates_no_task_for_either(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=10))
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=5))
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=25))

        report = await service.sync()

        assert report.touches_closed == 3
        assert hubspot.created_subjects() == ["Cadence 1/3 · call", "Cadence 2/3 · call"]


class TestFutureDatedActivity:
    async def test_a_booked_meeting_does_not_close_the_call(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.log(ActivityKind.meeting, at=clock.now.replace(day=13))

        report = await service.sync()

        assert report.touches_closed == 0
        assert report.tasks_created == 0


class TestConcurrentSyncs:
    """M1: a second sync while one is running skips cleanly instead of double-creating."""

    async def test_a_sync_skips_while_another_holds_the_lock(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        migrated_database: str,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as other:
                await other.execute(
                    text("select pg_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                )
                skipped = await service.sync()
                await other.execute(
                    text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                )
        finally:
            await engine.dispose()

        assert skipped.skipped is True
        assert (skipped.checked, skipped.tasks_created) == (0, 0)
        assert hubspot.task_creates_attempted() == 1

        ran = await service.sync()

        assert ran.skipped is False
        assert ran.tasks_created == 1

    async def test_the_lock_is_released_after_a_sync(
        self, service: CadenceService, migrated_database: str
    ) -> None:
        await service.sync()

        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as other:
                taken = await other.execute(
                    text("select pg_try_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                )
                assert taken.scalar_one() is True
                await other.execute(
                    text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                )
        finally:
            await engine.dispose()


class TestNonHubSpotFailures:
    """M4: a validation or database error on one prospect is that prospect's failure."""

    async def test_a_malformed_body_for_one_prospect_does_not_stop_the_others(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol("400000001")
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))
        await service.enrol("400000002")
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))
        hubspot.malformed_associations_for = {"400000001"}

        report = await service.sync()

        assert report.checked == 2
        assert [(f.hubspot_contact_id, f.code) for f in report.failures] == [
            ("400000001", "invalid_hubspot_response")
        ]
        assert report.tasks_created == 1

    async def test_a_database_error_rolls_back_that_prospect_and_carries_on(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await service.enrol("400000001")
        first_task = hubspot.last_task_id()
        hubspot.complete_task(first_task, at=clock.advance(minutes=1))
        await service.enrol("400000002")
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=1))
        monkeypatch.setattr(
            CadenceRepository, "advance", _failing_for("400000001", CadenceRepository.advance)
        )

        report = await service.sync()

        assert [(f.hubspot_contact_id, f.code) for f in report.failures] == [
            ("400000001", "database_error")
        ]
        assert report.tasks_created == 1
        assert await _state(db_session, "400000001") == ("live", 1, "call", first_task)
        assert (await _state(db_session, "400000002"))[1:3] == (1, "voicemail")


class TestAssociationShape:
    async def test_an_unrecognised_associations_body_is_reported_not_read_as_silence(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.log(ActivityKind.call, at=clock.advance(minutes=5))
        hubspot.unrecognised_associations_for = {CONTACT}

        report = await service.sync()

        assert [f.code for f in report.failures] == ["hubspot_response_shape_unrecognised"]
        assert report.tasks_created == 0


class TestDefaultOwner:
    async def test_enrol_without_an_owner_assigns_the_default(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HUBSPOT_DEFAULT_OWNER_ID", "77002")
        get_settings.cache_clear()

        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        await service.sync()

        assert hubspot.created_owners() == ["77002", "77002"]

    async def test_an_explicit_owner_wins_over_the_default(
        self, service: CadenceService, hubspot: FakeHubSpot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HUBSPOT_DEFAULT_OWNER_ID", "77002")
        get_settings.cache_clear()

        await service.enrol(CONTACT, owner_id="77001")

        properties = hubspot.created[0]["properties"]
        assert isinstance(properties, dict)
        assert properties["hubspot_owner_id"] == "77001"

    async def test_no_default_keeps_tasks_unassigned_and_warns(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HUBSPOT_DEFAULT_OWNER_ID", "")
        get_settings.cache_clear()

        await service.enrol(CONTACT)
        report = await service.sync()

        properties = hubspot.created[0]["properties"]
        assert isinstance(properties, dict)
        assert "hubspot_owner_id" not in properties
        assert any("HUBSPOT_DEFAULT_OWNER_ID" in warning for warning in report.warnings)


async def _hold_sync_lock[T](database_url: str, operation: Callable[[], Awaitable[T]]) -> T:
    """Run ``operation`` while another connection holds the cadence-sync lock."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as other:
            await other.execute(text("select pg_advisory_lock(:id)"), {"id": CADENCE_SYNC_LOCK_ID})
            try:
                return await operation()
            finally:
                await other.execute(
                    text("select pg_advisory_unlock(:id)"), {"id": CADENCE_SYNC_LOCK_ID}
                )
    finally:
        await engine.dispose()


class TestDryRun:
    """``sync --dry-run``: the same reads and the same decision, and nothing applied."""

    async def test_a_dry_run_creates_nothing_and_writes_nothing(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        call_task = hubspot.last_task_id()
        hubspot.complete_task(call_task, at=clock.advance(minutes=5))

        report = await service.sync(dry_run=True)

        assert report.dry_run is True
        assert hubspot.task_creates_attempted() == 1, "only the enrolment's create"
        assert await _state(db_session) == ("live", 1, "call", call_task)
        assert (report.touches_closed, report.tasks_created, report.parked) == (0, 0, 0)
        [plan] = report.plans
        assert plan.hubspot_contact_id == CONTACT
        assert [(s.position.cycle, s.position.touch) for s in plan.steps] == [(1, Touch.call)]
        assert plan.steps[0].signal.source == SignalSource.task_completed
        assert plan.next_position == CadencePosition(cycle=1, touch=Touch.voicemail)

    async def test_a_dry_run_predicts_what_the_real_sync_then_does(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=5))
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=5))
        clock.advance(minutes=5)

        [plan] = (await service.sync(dry_run=True)).plans
        real = await service.sync()

        assert real.touches_closed == len(plan.steps) == 3
        assert plan.parks is False
        assert plan.next_position == CadencePosition(cycle=2, touch=Touch.call)
        _, cycle, touch, _ = await _state(db_session)
        assert (cycle, touch) == (2, "call")

    async def test_a_dry_run_names_the_note_that_closed_a_touch(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        await service.enrol(CONTACT)
        note = hubspot.log(ActivityKind.note, at=clock.advance(minutes=5))

        [plan] = (await service.sync(dry_run=True)).plans

        [step] = plan.steps
        assert step.signal.source == SignalSource.activity
        assert step.signal.anchor_ref == f"notes:{note}"

    async def test_an_unchanged_prospect_is_planned_as_nothing_new(
        self, service: CadenceService
    ) -> None:
        await service.enrol(CONTACT)

        [plan] = (await service.sync(dry_run=True)).plans

        assert plan.steps == []
        assert plan.changed is False

    async def test_a_dry_run_does_not_wait_for_the_sync_lock(
        self, service: CadenceService, migrated_database: str
    ) -> None:
        await service.enrol(CONTACT)

        report = await _hold_sync_lock(migrated_database, lambda: service.sync(dry_run=True))

        assert report.skipped is False
        assert report.checked == 1

    async def test_a_pending_create_that_happened_is_reported_found_not_adopted(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.lose_create_responses = 1
        await service.sync()
        orphan = hubspot.last_task_id()

        [plan] = (await service.sync(dry_run=True)).plans

        assert plan.pending_task == PendingTask.found
        assert plan.pending_task_id == orphan
        assert hubspot.task_creates_attempted() == 2
        assert await _state(db_session) == ("live", 1, "voicemail", None), "still pending"

    async def test_a_pending_create_that_never_happened_is_reported_to_create(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.fail_creates = 1
        await service.sync()

        [plan] = (await service.sync(dry_run=True)).plans

        assert plan.pending_task == PendingTask.would_create
        assert hubspot.task_creates_attempted() == 2, "the failed create, and no more"


class TestPark:
    """``park``: a human finishes a prospect's cadence for good."""

    async def test_park_finishes_the_cadence_and_leaves_the_task_alone(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        call_task = hubspot.last_task_id()
        requests_before = len(hubspot.portal.requests)

        result = await service.park(CONTACT)

        assert (result.cycle, result.touch, result.open_task_id) == (1, Touch.call, call_task)
        assert await _state(db_session) == ("parked", 1, "call", None)
        assert len(hubspot.portal.requests) == requests_before, "park never reaches HubSpot"
        assert hubspot.open_tasks() == ["Cadence 1/3 · call"]
        after = await service.sync()
        assert (after.skipped, after.checked) == (False, 0), "the lock was released"

    async def test_park_needs_no_hubspot(self, db_session: AsyncSession) -> None:
        def no_portal() -> HubSpotClient:
            raise AssertionError("park reached HubSpot")

        await CadenceRepository(db_session).create(
            contact_id=CONTACT,
            company_id=None,
            owner_id=None,
            position=CadencePosition.first(),
            task_id="88000001",
            due_at=datetime(2026, 10, 6, 4, 59, tzinfo=UTC),
            anchor_at=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
            enrolled_at=datetime(2026, 10, 5, 14, 0, tzinfo=UTC),
        )

        result = await CadenceService(db_session, hubspot=no_portal).park(CONTACT)

        assert result.open_task_id == "88000001"

    async def test_an_unknown_contact_cannot_be_parked(self, service: CadenceService) -> None:
        with pytest.raises(NotEnrolledError):
            await service.park("400999999")

        assert (await service.sync()).skipped is False, "a refused park releases the lock"

    async def test_a_parked_contact_cannot_be_parked_again(self, service: CadenceService) -> None:
        await service.enrol(CONTACT)
        await service.park(CONTACT)

        with pytest.raises(AlreadyParkedError):
            await service.park(CONTACT)

        assert (await service.sync()).skipped is False, "a refused park releases the lock"

    async def test_park_refuses_while_a_sync_runs(
        self,
        service: CadenceService,
        db_session: AsyncSession,
        migrated_database: str,
    ) -> None:
        """A sync that already loaded the row could otherwise advance a parked prospect."""
        await service.enrol(CONTACT)

        with pytest.raises(SyncRunningError):
            await _hold_sync_lock(migrated_database, lambda: service.park(CONTACT))

        status, *_ = await _state(db_session)
        assert status == "live"

    async def test_park_clears_an_interrupted_create_and_says_so(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.lose_create_responses = 1
        await service.sync()

        result = await service.park(CONTACT)

        assert result.pending_task_key == f"lpe-cadence:{CONTACT}:1-voicemail"
        assert result.open_task_id is None
        state = await CadenceRepository(db_session).get_by_contact(CONTACT)
        assert state is not None
        assert (state.status, state.pending_task_key) == (CadenceStatus.parked.value, None)


class TestDryRunAgainstAMovingSchedule:
    """Review findings on PR #12: a dry run plans from one moment, and says only true things."""

    async def test_a_sync_advancing_mid_dry_run_is_not_reported_as_a_deleted_task(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M1: rows and the task batch must come from the same moment.

        Between the batch read and the per-row work, a real sync advances the prospect to a task
        the batch never saw. Planning from the re-read row would call that task deleted.
        """
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        real_tasks = OutcomeReader.tasks

        async def tasks_then_a_sync_lands(
            reader: OutcomeReader, task_ids: list[str]
        ) -> dict[str, TaskSnapshot]:
            batch = await real_tasks(reader, task_ids)
            await db_session.execute(
                update(CadenceState)
                .where(CadenceState.hubspot_contact_id == CONTACT)
                .values(touch="voicemail", hubspot_task_id="88999999")
                .execution_options(synchronize_session=False)
            )
            return batch

        monkeypatch.setattr(OutcomeReader, "tasks", tasks_then_a_sync_lands)

        [plan] = (await service.sync(dry_run=True)).plans

        assert plan.task_missing is False
        assert [(s.position.cycle, s.position.touch) for s in plan.steps] == [(1, Touch.call)]

    async def test_a_pending_row_closed_by_a_note_plans_what_the_sync_then_does(
        self,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        """L1 and L5: the riskiest path of the decide/apply split.

        The voicemail's create failed, so the row is pending with no task in HubSpot, and a note
        then closes the voicemail. The dry run plans with no task; the real sync creates the task
        first. Both must close the same touches, schedule the same next task, and the dry run must
        say the task it creates would be left open, superseded.
        """
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        hubspot.fail_creates = 1
        await service.sync()
        hubspot.log(ActivityKind.note, at=clock.advance(minutes=5))
        clock.advance(minutes=5)

        [plan] = (await service.sync(dry_run=True)).plans
        real = await service.sync()

        assert plan.pending_task == PendingTask.would_create
        assert plan.pending_task_superseded is True
        assert [(s.position.touch, s.signal.source) for s in plan.steps] == [
            (Touch.voicemail, SignalSource.activity)
        ]
        assert real.touches_closed == len(plan.steps)
        assert real.tasks_created == 2, "the interrupted voicemail task, then the email task"
        assert len(real.open_tasks_superseded) == 1
        assert plan.next_position == CadencePosition(cycle=1, touch=Touch.email)
        state = await CadenceRepository(db_session).get_by_contact(CONTACT)
        assert state is not None
        assert (state.touch, state.due_at) == ("email", plan.next_due_at)

    async def test_a_dry_run_logs_no_touch_done_and_no_overdue_warning(
        self, service: CadenceService, hubspot: FakeHubSpot, clock: FakeClock
    ) -> None:
        """L2 and L6: T10 sums ``touch_done`` for M5, so a rehearsal must never emit it."""
        await service.enrol(CONTACT)
        hubspot.complete_task(hubspot.last_task_id(), at=clock.advance(minutes=5))
        clock.advance(days=2)

        with capture_logs() as captured:
            report = await service.sync(dry_run=True)

        events = {entry["event"] for entry in captured}
        assert report.plans[0].changed
        assert report.overdue, "the overdue view is still reported"
        assert "cadence.sync.touch_done" not in events
        assert "cadence.sync.touch_overdue" not in events


class TestParkReadsTheDatabase:
    async def test_park_sees_a_park_its_session_has_not_loaded(
        self, service: CadenceService, db_session: AsyncSession
    ) -> None:
        """L4: the row is checked as the database has it, not as this session cached it."""
        await service.enrol(CONTACT)
        cached = await CadenceRepository(db_session).get_by_contact(CONTACT)
        assert cached is not None and cached.status == "live"
        await db_session.execute(
            update(CadenceState)
            .where(CadenceState.hubspot_contact_id == CONTACT)
            .values(status="parked", hubspot_task_id=None, due_at=None, parked_at=datetime.now(UTC))
            # Change the row, not the session's copy: another process parked it.
            .execution_options(synchronize_session=False)
        )
        assert cached.status == "live", "the session still holds the stale copy"

        with pytest.raises(AlreadyParkedError):
            await service.park(CONTACT)
