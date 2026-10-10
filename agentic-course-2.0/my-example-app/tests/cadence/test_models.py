"""The database's own guarantees on ``cadence_state`` — true however a row gets written."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CadencePosition, Touch
from app.cadence.models import CadenceState
from app.cadence.repository import CadenceRepository
from tests.conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)


def _row(**overrides: object) -> CadenceState:
    values: dict[str, object] = {
        "hubspot_contact_id": "400112233",
        "status": "live",
        "cycle": 1,
        "touch": "call",
        "hubspot_task_id": "88000001",
        "due_at": NOW,
        "anchor_at": NOW,
        "enrolled_at": NOW,
    }
    values.update(overrides)
    return CadenceState(**values)


async def _insert(session: AsyncSession, row: CadenceState) -> None:
    session.add(row)
    await session.flush()


class TestConstraints:
    async def test_a_well_formed_live_row_is_accepted(self, db_session: AsyncSession) -> None:
        await _insert(db_session, _row())

    async def test_a_parked_row_needs_no_task(self, db_session: AsyncSession) -> None:
        await _insert(
            db_session,
            _row(status="parked", touch="email", cycle=3, hubspot_task_id=None, due_at=None),
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"hubspot_task_id": None},
            {"hubspot_task_id": None, "pending_task_key": None},
            {"due_at": None},
            {"status": "Live"},
            {"status": "waiting"},
            {"touch": "door"},
            {"cycle": 0},
            {"cycle": 4},
        ],
        ids=[
            "live-without-task",
            "live-without-task-or-pending",
            "live-without-due-date",
            "status-case",
            "status-unknown",
            "touch-unknown",
            "cycle-zero",
            "cycle-four",
        ],
    )
    async def test_an_impossible_row_is_refused(
        self, db_session: AsyncSession, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(IntegrityError):
            await _insert(db_session, _row(**overrides))

    async def test_a_live_row_may_carry_a_pending_task_instead_of_a_task(
        self, db_session: AsyncSession
    ) -> None:
        """H1: the advance is committed before the create, as an intent the next sync resolves."""
        await _insert(
            db_session,
            _row(hubspot_task_id=None, pending_task_key="lpe-cadence:400112233:1-voicemail"),
        )

    async def test_a_parked_row_cannot_carry_a_pending_task(self, db_session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            await _insert(
                db_session,
                _row(
                    status="parked",
                    hubspot_task_id=None,
                    due_at=None,
                    pending_task_key="lpe-cadence:400112233:3-email",
                ),
            )

    async def test_one_cadence_per_contact(self, db_session: AsyncSession) -> None:
        """Re-enrolling would reset the cycle; the database refuses it whatever the service does."""
        await _insert(db_session, _row())

        with pytest.raises(IntegrityError):
            await _insert(db_session, _row(hubspot_task_id="88000002"))


class TestRepositoryCreates:
    """T13: an enrolment anchored on an activity keeps its ref, and a refusal parks with no task."""

    async def test_a_live_row_keeps_the_anchor_ref_it_was_created_with(
        self, db_session: AsyncSession
    ) -> None:
        repository = CadenceRepository(db_session)
        await repository.create(
            contact_id="400112233",
            company_id=None,
            owner_id=None,
            position=CadencePosition.first(),
            task_id="88000001",
            due_at=NOW,
            anchor_at=NOW,
            anchor_ref="notes:401433689810",
            enrolled_at=NOW,
        )

        state = await repository.get_by_contact("400112233")
        assert state is not None
        assert (state.anchor_at, state.anchor_ref) == (NOW, "notes:401433689810")

    async def test_a_parked_row_is_created_with_no_task(self, db_session: AsyncSession) -> None:
        repository = CadenceRepository(db_session)
        await repository.create_parked(
            contact_id="400112233",
            company_id="7009876543",
            owner_id=None,
            position=CadencePosition(cycle=1, touch=Touch.voicemail),
            anchor_at=NOW,
            anchor_ref="notes:401433689810",
            enrolled_at=NOW,
            parked_at=NOW,
        )

        state = await repository.get_by_contact("400112233")
        assert state is not None
        assert (state.status, state.cycle, state.touch) == ("parked", 1, "voicemail")
        assert (state.hubspot_task_id, state.pending_task_key, state.due_at) == (None, None, None)
        assert (state.anchor_ref, state.parked_at) == ("notes:401433689810", NOW)


class TestScheduleOnly:
    def test_the_columns_are_exactly_the_schedule(self) -> None:
        """D4: outcomes live in HubSpot. An allowlist, not a blacklist of suspicious names — a
        column called ``spoke_to`` or ``answered`` would pass a blacklist and still be the shadow
        CRM starting. Adding a column means adding it here, on purpose."""
        columns = {column.key for column in inspect(CadenceState).columns}

        assert columns == {
            "id",
            "hubspot_contact_id",
            "hubspot_company_id",
            "hubspot_owner_id",
            "status",
            "cycle",
            "touch",
            "hubspot_task_id",
            "pending_task_key",
            "due_at",
            "anchor_at",
            "anchor_ref",
            "enrolled_at",
            "updated_at",
            "parked_at",
        }
