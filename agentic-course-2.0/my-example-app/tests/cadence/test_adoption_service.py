"""T13 end to end: discover, rehearse and apply a roster, against the fake portal and a database.

The fake portal raises on any request it does not model, so "adoption writes no prospect field"
is proved by the absence of a route for one, not asserted by hope.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from structlog.testing import capture_logs

from app.cadence.adoption import Adopter, Roster, RosterEntry
from app.cadence.exceptions import AlreadyEnrolledError, SyncRunningError
from app.cadence.machine import CadencePosition, Touch, due_at_enrolment
from app.cadence.repository import CADENCE_SYNC_LOCK_ID, CadenceRepository
from app.cadence.schemas import Activity, ActivityKind, RosterAction
from app.cadence.service import CadenceService
from app.cadence.sync import OutcomeReader
from tests.cadence.conftest import CONTACT, START, FakeClock, FakeHubSpot
from tests.conftest import requires_db

pytestmark = requires_db

SINCE = START - timedelta(days=20)
OTHER = "400112244"
COMPANY_A = "7000000001"
COMPANY_B = "7000000002"


def _roster(*entries: tuple[str, RosterAction] | RosterEntry) -> Roster:
    return Roster(
        prospect=[
            entry
            if isinstance(entry, RosterEntry)
            else RosterEntry(contact=entry[0], action=entry[1])
            for entry in entries
        ]
    )


def _adopt(contact: str = CONTACT) -> Roster:
    return _roster((contact, RosterAction.adopt))


async def _row(session: AsyncSession, contact: str = CONTACT) -> tuple[str, int, str]:
    state = await CadenceRepository(session).get_by_contact(contact)
    assert state is not None
    return state.status, state.cycle, state.touch


async def _hold_sync_lock[T](database_url: str, operation: Callable[[], Awaitable[T]]) -> T:
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


def _deadlock_for[**P, R](
    contact_id: str, real: Callable[P, Awaitable[R]]
) -> Callable[P, Awaitable[R]]:
    """Wrap a repository write so it raises a database error for one contact only."""

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if kwargs.get("contact_id") == contact_id:
            raise OperationalError("INSERT cadence_state", {}, Exception("deadlock detected"))
        return await real(*args, **kwargs)

    return wrapper


class TestNoFieldWrites:
    async def test_a_contact_with_nothing_citable_adopts(
        self, adopter: Adopter, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        """The ticket's test: only an id and a creation date. Nothing to cite, nothing gated."""
        hubspot.add_contact(CONTACT, SINCE)

        with capture_logs() as captured:
            report = await adopter.apply(_adopt())

        assert [state.hubspot_contact_id for state in report.adopted] == [CONTACT]
        assert await _row(db_session) == ("live", 1, "call")
        assert hubspot.created_subjects() == ["Cadence 1/3 · call"]
        for request in hubspot.portal.requests:
            path = request.url.path
            assert request.method in ("GET", "POST"), f"{request.method} {path}"
            if request.method == "POST":
                assert path.endswith(("/batch/read", "/tasks")), f"a write to {path}"
        candidates = await db_session.execute(text("select count(*) from candidate"))
        assert candidates.scalar_one() == 0
        events = [entry["event"] for entry in captured]
        assert "cadence.sync.touch_done" not in events, "history is not M5"


class TestReconstruction:
    async def test_two_logged_calls_resume_at_the_email(
        self, adopter: Adopter, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.log(ActivityKind.call, at=SINCE + timedelta(days=2))
        hubspot.log(ActivityKind.call, at=SINCE + timedelta(days=2, minutes=5))

        report = await adopter.apply(_adopt())

        assert await _row(db_session) == ("live", 1, "email")
        assert hubspot.created_subjects() == ["Cadence 1/3 · email draft (send by hand)"]
        (plan,) = report.plans
        assert [step.position.touch for step in plan.steps] == [Touch.call, Touch.voicemail]

    async def test_the_first_sync_after_adoption_closes_nothing_adoption_credited(
        self,
        adopter: Adopter,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
    ) -> None:
        """The anchor regression: history ends on a note, and that note is adoption's alone."""
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.log(ActivityKind.note, at=SINCE + timedelta(days=3))
        await adopter.apply(_adopt())
        clock.advance(hours=1)

        report = await service.sync()

        assert (report.touches_closed, report.tasks_created) == (0, 0)

    async def test_a_note_logged_after_adoption_is_the_machines_to_count(
        self,
        adopter: Adopter,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
        db_session: AsyncSession,
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        for day in (2, 3):
            hubspot.log(ActivityKind.note, at=SINCE + timedelta(days=day))
        await adopter.apply(_adopt())
        assert await _row(db_session) == ("live", 1, "email")

        hubspot.log(ActivityKind.note, at=clock.advance(hours=2))
        with capture_logs() as captured:
            report = await service.sync()

        assert report.touches_closed == 1
        assert [entry["event"] for entry in captured].count("cadence.sync.touch_done") == 1
        assert await _row(db_session) == ("live", 2, "call")

    async def test_a_roster_start_overrides_the_evidence(
        self, adopter: Adopter, hubspot: FakeHubSpot, clock: FakeClock, db_session: AsyncSession
    ) -> None:
        """Evidence logged only on the company is invisible here; the roster says where it is."""
        hubspot.add_contact(CONTACT, SINCE)
        entry = RosterEntry(contact=CONTACT, action=RosterAction.adopt, start="2-call")

        report = await adopter.apply(_roster(entry))

        assert await _row(db_session) == ("live", 2, "call")
        (state,) = report.adopted
        assert state.due_at == due_at_enrolment(clock())
        stored = await CadenceRepository(db_session).get_by_contact(CONTACT)
        assert stored is not None
        assert (stored.anchor_at, stored.anchor_ref) == (clock(), None)


class TestRefusals:
    async def test_a_park_entry_is_finished_with_no_task_and_cannot_be_enrolled(
        self,
        adopter: Adopter,
        service: CadenceService,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.log(ActivityKind.note, at=SINCE + timedelta(days=1))

        report = await adopter.apply(_roster((CONTACT, RosterAction.park)))

        assert [state.hubspot_contact_id for state in report.parked] == [CONTACT]
        assert await _row(db_session) == ("parked", 1, "voicemail")
        assert hubspot.task_creates_attempted() == 0
        with pytest.raises(AlreadyEnrolledError):
            await service.enrol(CONTACT)

    async def test_nine_logged_touches_park_an_adopt_entry(
        self, adopter: Adopter, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        for n in range(9):
            hubspot.log(ActivityKind.note, at=SINCE + timedelta(days=1, minutes=n))

        report = await adopter.apply(_adopt())

        assert report.plans[0].finished
        assert await _row(db_session) == ("parked", 3, "email")
        assert hubspot.task_creates_attempted() == 0


class TestRerun:
    async def test_applying_the_same_roster_twice_adopts_nothing_new(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.add_contact(OTHER, SINCE)
        roster = _roster((CONTACT, RosterAction.adopt), (OTHER, RosterAction.park))
        await adopter.apply(roster)
        creates = hubspot.task_creates_attempted()

        again = await adopter.apply(roster)

        assert again.already_enrolled == [CONTACT, OTHER]
        assert (again.adopted, again.parked, again.failures) == ([], [], [])
        assert hubspot.task_creates_attempted() == creates

    async def test_an_interrupted_create_is_found_by_key_on_the_rerun(
        self, adopter: Adopter, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.lose_create_responses = 1

        first = await adopter.apply(_adopt())

        (failure,) = first.failures
        assert (failure.hubspot_contact_id, failure.code) == (CONTACT, "hubspot_response_error")
        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None
        made = hubspot.last_task_id()

        second = await adopter.apply(_adopt())

        (state,) = second.adopted
        assert state.hubspot_task_id == made
        assert hubspot.task_creates_attempted() == 1, "found by key, not created twice"

    async def test_one_bad_entry_does_not_stop_the_others(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(OTHER, SINCE)

        report = await adopter.apply(
            _roster(("400999999", RosterAction.adopt), (OTHER, RosterAction.adopt))
        )

        (failure,) = report.failures
        assert (failure.hubspot_contact_id, failure.code) == ("400999999", "contact_not_found")
        assert [state.hubspot_contact_id for state in report.adopted] == [OTHER]

    async def test_a_contact_without_a_creation_date_is_reported(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(CONTACT, None)

        report = await adopter.apply(_adopt())

        (failure,) = report.failures
        assert failure.code == "contact_created_at_missing"
        assert hubspot.task_creates_attempted() == 0

    async def test_a_database_error_rolls_back_one_entry_and_the_next_still_adopts(
        self,
        adopter: Adopter,
        hubspot: FakeHubSpot,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.add_contact(OTHER, SINCE)
        monkeypatch.setattr(
            CadenceRepository, "create", _deadlock_for(CONTACT, CadenceRepository.create)
        )

        report = await adopter.apply(
            _roster((CONTACT, RosterAction.adopt), (OTHER, RosterAction.adopt))
        )

        (failure,) = report.failures
        assert (failure.hubspot_contact_id, failure.code) == (CONTACT, "database_error")
        assert [state.hubspot_contact_id for state in report.adopted] == [OTHER]
        assert [plan.hubspot_contact_id for plan in report.plans] == [OTHER]
        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None

    async def test_an_entry_enrolled_by_another_run_mid_apply_shows_no_plan(
        self,
        adopter: Adopter,
        service: CadenceService,
        hubspot: FakeHubSpot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """pr-14 M2: a plan is reported only once it was applied, not when it was drawn up."""
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.add_contact(OTHER, SINCE)
        real_activities = OutcomeReader.activities

        async def enrolled_meanwhile(
            reader: OutcomeReader, contact_id: str, since: datetime | None = None
        ) -> list[Activity]:
            history = await real_activities(reader, contact_id, since)
            if contact_id == CONTACT:
                await service.enrol(CONTACT)
            return history

        monkeypatch.setattr(OutcomeReader, "activities", enrolled_meanwhile)

        report = await adopter.apply(
            _roster((CONTACT, RosterAction.adopt), (OTHER, RosterAction.adopt))
        )

        assert report.already_enrolled == [CONTACT]
        assert [plan.hubspot_contact_id for plan in report.plans] == [OTHER]
        assert [state.hubspot_contact_id for state in report.adopted] == [OTHER]


class TestLock:
    async def test_apply_refuses_while_a_sync_runs_but_a_rehearsal_does_not(
        self, adopter: Adopter, hubspot: FakeHubSpot, migrated_database: str
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)

        with pytest.raises(SyncRunningError):
            await _hold_sync_lock(migrated_database, lambda: adopter.apply(_adopt()))
        rehearsed = await _hold_sync_lock(migrated_database, lambda: adopter.rehearse(_adopt()))

        assert [plan.start for plan in rehearsed.plans] == [CadencePosition.first()]
        assert hubspot.task_creates_attempted() == 0


class TestDiscover:
    async def test_discover_lists_each_hand_worked_contact_once(
        self,
        adopter: Adopter,
        service: CadenceService,
        hubspot: FakeHubSpot,
        clock: FakeClock,
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE, first="Jorge", last="Rodriguez")
        hand = [hubspot.add_hand_task(f"Follow up {n}", contact=CONTACT) for n in range(3)]
        ticked = hubspot.add_hand_task(
            "Called", contact=CONTACT, completed_at=SINCE + timedelta(days=1)
        )
        kodiak = hubspot.add_hand_task("Call Kodiak", company=COMPANY_A)
        hubspot.add_contact(OTHER, SINCE)
        await service.enrol(OTHER)
        hubspot.add_hand_task("Old follow-up", contact=OTHER)

        report = await adopter.discover()

        assert report.dry_run
        assert [plan.hubspot_contact_id for plan in report.plans] == [CONTACT]
        (plan,) = report.plans
        assert plan.display_name == "Jorge Rodriguez"
        assert [task.task_id for task in plan.hand_tasks] == [*hand, ticked]
        assert [task.completed for task in plan.hand_tasks] == [False, False, False, True]
        assert plan.start == CadencePosition.first()
        assert [(task.task_id, task.company_ids) for task in report.company_only] == [
            (kodiak, [COMPANY_A])
        ]
        assert report.already_enrolled == [OTHER], "our own keyed task is not a hand task"
        assert hubspot.task_creates_attempted() == 1, "only the enrolment above"

    async def test_a_company_only_task_names_the_contacts_its_company_already_has(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        """APS-style: the contact named after the company carries one task, the company another.
        The second is a duplicate to close, not a reason to add a contact."""
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.link_company(CONTACT, COMPANY_A)
        hubspot.add_hand_task("Follow up with APS", contact=CONTACT)
        duplicate = hubspot.add_hand_task("Follow up with APS FireCo", company=COMPANY_A)
        hubspot.add_contact(OTHER, SINCE)
        hubspot.link_company(OTHER, COMPANY_B)
        quiet = hubspot.add_hand_task("Follow up with Eagle", company=COMPANY_B)
        orphan = hubspot.add_hand_task("Call Kodiak", company="7000000003")

        report = await adopter.discover()

        assert [plan.hubspot_contact_id for plan in report.plans] == [CONTACT]
        assert [(task.task_id, task.company_contacts) for task in report.company_only] == [
            (duplicate, [CONTACT]),
            (quiet, [OTHER]),
            (orphan, []),
        ]

    async def test_a_truncated_search_is_warned_about(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.search_total_extra = 1

        report = await adopter.discover()

        assert any("truncated" in warning for warning in report.warnings)

    async def test_a_rehearsal_writes_nothing(
        self, adopter: Adopter, hubspot: FakeHubSpot, db_session: AsyncSession
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)

        report = await adopter.rehearse(_adopt())

        assert report.dry_run
        assert len(report.plans) == 1
        assert await CadenceRepository(db_session).get_by_contact(CONTACT) is None
        assert hubspot.task_creates_attempted() == 0


class TestHandTasks:
    async def test_hand_tasks_are_listed_and_never_written(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hand = hubspot.add_hand_task("Follow up with Jorge", contact=CONTACT)

        report = await adopter.apply(_adopt())

        assert [task.task_id for task in report.plans[0].hand_tasks] == [hand]
        assert hubspot.task_status(hand) == "NOT_STARTED"
        assert hubspot.portal.count("PATCH") == 0


class TestCompany:
    async def test_two_companies_and_no_choice_is_a_warning_and_no_company_on_the_task(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.link_company(CONTACT, COMPANY_A)
        hubspot.link_company(CONTACT, COMPANY_B)

        report = await adopter.apply(_adopt())

        assert any("2 companies" in warning for warning in report.plans[0].warnings)
        assert hubspot.created_company_ids() == [[]]

    async def test_the_roster_company_is_the_one_associated(
        self, adopter: Adopter, hubspot: FakeHubSpot
    ) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.link_company(CONTACT, COMPANY_A)
        hubspot.link_company(CONTACT, COMPANY_B)
        entry = RosterEntry(contact=CONTACT, action=RosterAction.adopt, company=COMPANY_B)

        report = await adopter.apply(_roster(entry))

        assert report.plans[0].warnings == []
        assert hubspot.created_company_ids() == [[COMPANY_B]]

    async def test_the_only_company_is_used(self, adopter: Adopter, hubspot: FakeHubSpot) -> None:
        hubspot.add_contact(CONTACT, SINCE)
        hubspot.link_company(CONTACT, COMPANY_A)

        await adopter.apply(_adopt())

        assert hubspot.created_company_ids() == [[COMPANY_A]]
