"""``sync_reviews``: the founder's decision in HubSpot → the cadence, recorded in the ledger.

The enrolment is T11's ``CadenceService.enrol``, run for real against the same fake portal, so the
hand-off is tested end to end: one task, on the right contact and company, and a cadence row.
"""

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.models import CadenceState
from app.cadence.repository import CadenceRepository
from app.cadence.service import CadenceService
from app.promotion.client import HubSpotClient
from app.promotion.models import Promotion
from app.promotion.repository import PromotionRepository
from app.promotion.schemas import (
    PromotedProspect,
    PromotionStatus,
    ReviewOutcome,
    ReviewSyncReport,
)
from app.promotion.service import PromotionService
from tests.cadence.conftest import FakeClock, raced_by_another_run
from tests.conftest import requires_db
from tests.promotion.builders import a_promotable
from tests.promotion.crm import FakeCrm
from tests.promotion.scenario import a_finished_run

pytestmark = requires_db

PHONES = ("+1-817-555-0101", "+1-817-555-0102", "+1-817-555-0103")


async def _promoted(
    session: AsyncSession, promotion: PromotionService, count: int = 1
) -> list[PromotedProspect]:
    ids = [f"100000{n}" for n in range(1, count + 1)]
    scenario = await a_finished_run(
        session, [a_promotable(rid, phone=phone) for rid, phone in zip(ids, PHONES, strict=False)]
    )
    report = await promotion.promote_run(scenario.run.id)
    assert report.counts["created"] == count
    return list(report.created)


def _ids(prospect: PromotedProspect) -> tuple[str, str]:
    assert prospect.company_id is not None and prospect.contact_id is not None
    return prospect.company_id, prospect.contact_id


async def _status(session: AsyncSession, registry_id: str) -> str:
    row = await session.scalar(
        select(Promotion)
        .where(Promotion.registry_id == registry_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    return row.status


def _nonzero(report: ReviewSyncReport) -> dict[str, int]:
    return {name: count for name, count in report.counts.items() if count}


def _losing_rejections[**P](
    real: Callable[P, Awaitable[Promotion | None]],
) -> Callable[P, Awaitable[Promotion | None]]:
    """Wrap ``PromotionRepository.transition`` so a move to ``rejected`` loses its race."""

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Promotion | None:
        if kwargs.get("to_status") is PromotionStatus.rejected:
            return None
        return await real(*args, **kwargs)

    return wrapper


class TestAccepted:
    async def test_an_accepted_prospect_is_enrolled_once(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        company_id, contact_id = _ids(prospect)
        crm.set_review_status(company_id, "accepted")

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "enrolled": 1}
        assert crm.created_subjects() == ["Cadence 1/3 · call"]
        assert crm.open_tasks(contact_id) == ["Cadence 1/3 · call"]
        assert crm.created_company_ids() == [[company_id]]
        (decision,) = report.decisions
        assert (decision.outcome, decision.contact_id) == (ReviewOutcome.enrolled, contact_id)
        assert decision.first_due_at is not None
        state = await db_session.scalar(
            select(CadenceState).where(CadenceState.hubspot_contact_id == contact_id)
        )
        assert state is not None and state.status == "live"
        assert await _status(db_session, prospect.registry_id) == "enrolled"

    async def test_a_second_sync_enrols_nothing_and_a_new_acceptance_still_enrols(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        first, second = await _promoted(db_session, promotion, count=2)
        crm.set_review_status(_ids(first)[0], "accepted")
        await promotion.sync_reviews()

        again = await promotion.sync_reviews()
        assert _nonzero(again) == {"read": 1, "waiting": 1}
        assert len(crm.created) == 1

        crm.set_review_status(_ids(second)[0], "accepted")
        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1
        assert len(crm.created) == 2

    async def test_a_failed_enrolment_stays_enrolling_and_the_next_sync_finishes_it(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        crm.set_review_status(_ids(prospect)[0], "accepted")
        crm.fail_creates = 1

        failed = await promotion.sync_reviews()
        assert _nonzero(failed) == {"read": 1, "failed": 1}
        assert await _status(db_session, prospect.registry_id) == "enrolling"

        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1
        assert len(crm.tasks) == 1
        assert await _status(db_session, prospect.registry_id) == "enrolled"

    async def test_a_lost_task_response_is_reused_not_made_twice(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        crm.set_review_status(_ids(prospect)[0], "accepted")
        crm.lose_create_responses = 1

        await promotion.sync_reviews()
        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1
        assert len(crm.tasks) == 1

    async def test_a_contact_already_in_the_cadence_counts_as_enrolled(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        crm_client: HubSpotClient,
        crm_clock: FakeClock,
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        company_id, contact_id = _ids(prospect)
        cadence = CadenceService(db_session, hubspot=lambda: crm_client, clock=crm_clock)
        await cadence.enrol(contact_id, company_id=company_id)
        crm.set_review_status(company_id, "accepted")

        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1
        assert len(crm.created) == 1
        assert await _status(db_session, prospect.registry_id) == "enrolled"

    async def test_a_lost_enrolment_race_leaves_the_next_row_readable(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``enrol`` rolls back when it loses, expiring every loaded row (PR #33 M4)."""
        first, second = await _promoted(db_session, promotion, count=2)
        crm.set_review_status(_ids(first)[0], "accepted")
        crm.set_review_status(_ids(second)[0], "accepted")
        monkeypatch.setattr(
            CadenceRepository, "create", raced_by_another_run(CadenceRepository.create)
        )

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 2, "enrolled": 2}
        assert all(decision.orphan_task_id for decision in report.decisions)
        for prospect in (first, second):
            assert await _status(db_session, prospect.registry_id) == "enrolled"


class TestARetriedEnrolment:
    """A row left ``enrolling`` follows the founder's current value, not the one it was claimed on
    (PR #33 M2)."""

    async def test_a_founder_who_then_rejects_gets_no_task(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        company_id = _ids(prospect)[0]
        crm.set_review_status(company_id, "accepted")
        crm.fail_creates = 1
        await promotion.sync_reviews()
        assert await _status(db_session, prospect.registry_id) == "enrolling"
        crm.set_review_status(company_id, "rejected")

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "rejected": 1}
        assert crm.tasks == {}
        assert await _status(db_session, prospect.registry_id) == "rejected"

    async def test_a_decision_set_back_to_pending_waits_and_a_new_acceptance_enrols(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        company_id = _ids(prospect)[0]
        crm.set_review_status(company_id, "accepted")
        crm.fail_creates = 1
        await promotion.sync_reviews()
        crm.set_review_status(company_id, "pending_review")

        waiting = await promotion.sync_reviews()
        assert _nonzero(waiting) == {"read": 1, "waiting": 1}
        assert await _status(db_session, prospect.registry_id) == "pending_review"
        assert crm.tasks == {}

        crm.set_review_status(company_id, "accepted")
        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1
        assert len(crm.tasks) == 1


class TestOtherDecisions:
    async def test_rejected_is_recorded_and_hubspot_is_left_alone(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        crm.set_review_status(_ids(prospect)[0], "rejected")
        writes = crm.writes()

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "rejected": 1}
        assert crm.writes() == writes
        assert await _status(db_session, prospect.registry_id) == "rejected"

    async def test_a_rejection_another_sync_recorded_first_is_not_reported_twice(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The compare-and-set decides who reports a decision (PR #33 round-2 L3)."""
        (prospect,) = await _promoted(db_session, promotion)
        crm.set_review_status(_ids(prospect)[0], "rejected")
        monkeypatch.setattr(
            PromotionRepository, "transition", _losing_rejections(PromotionRepository.transition)
        )

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1}
        assert report.decisions == ()

    async def test_pending_review_is_waiting(
        self, db_session: AsyncSession, promotion: PromotionService
    ) -> None:
        await _promoted(db_session, promotion)

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "waiting": 1}
        assert report.unrecognised == ()

    async def test_a_deleted_company_is_withdrawn(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        crm.delete_company(_ids(prospect)[0])

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "withdrawn": 1}
        assert await _status(db_session, prospect.registry_id) == "withdrawn"

    async def test_a_company_the_read_lost_is_left_for_the_next_sync(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        """Only an explicit not-found withdraws; any other absence is a failed read (PR #33 M3)."""
        (prospect,) = await _promoted(db_session, promotion)
        company_id = _ids(prospect)[0]
        crm.set_review_status(company_id, "accepted")
        crm.dropped_from_reads.add(company_id)

        lost = await promotion.sync_reviews()
        assert _nonzero(lost) == {"read": 1, "failed": 1}
        assert lost.failures[0].registry_id == prospect.registry_id
        assert await _status(db_session, prospect.registry_id) == "pending_review"

        crm.dropped_from_reads.clear()
        report = await promotion.sync_reviews()

        assert report.counts["enrolled"] == 1

    @pytest.mark.parametrize("value", ["maybe_later", "ACCEPTED"])
    async def test_an_unrecognised_value_waits_and_is_reported(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm, value: str
    ) -> None:
        (prospect,) = await _promoted(db_session, promotion)
        crm.set_review_status(_ids(prospect)[0], value)

        report = await promotion.sync_reviews()

        assert _nonzero(report) == {"read": 1, "waiting": 1}
        assert report.unrecognised == (value,)
        assert crm.created == []


class TestDryRun:
    async def test_a_dry_run_decides_the_same_and_changes_nothing(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        accepted, rejected, deleted = await _promoted(db_session, promotion, count=3)
        crm.set_review_status(_ids(accepted)[0], "accepted")
        crm.set_review_status(_ids(rejected)[0], "rejected")
        crm.delete_company(_ids(deleted)[0])

        dry = await promotion.sync_reviews(dry_run=True)

        assert crm.created == []
        for prospect in (accepted, rejected, deleted):
            assert await _status(db_session, prospect.registry_id) == "pending_review"
        real = await promotion.sync_reviews()
        assert dry.counts == real.counts
        assert [d.outcome for d in dry.decisions] == [d.outcome for d in real.decisions]

    async def test_a_dry_run_follows_retried_enrolments_as_the_real_sync_does(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        """Rows left ``enrolling`` take the same branches dry and real (PR #33 round-2 L4)."""
        rejected, cleared = await _promoted(db_session, promotion, count=2)
        for prospect in (rejected, cleared):
            crm.set_review_status(_ids(prospect)[0], "accepted")
        crm.fail_creates = 2
        await promotion.sync_reviews()
        crm.set_review_status(_ids(rejected)[0], "rejected")
        crm.set_review_status(_ids(cleared)[0], None)

        dry = await promotion.sync_reviews(dry_run=True)

        for prospect in (rejected, cleared):
            assert await _status(db_session, prospect.registry_id) == "enrolling"
        real = await promotion.sync_reviews()
        assert _nonzero(real) == {"read": 2, "rejected": 1, "waiting": 1}
        assert dry.counts == real.counts
        assert [d.outcome for d in dry.decisions] == [d.outcome for d in real.decisions]
        assert crm.tasks == {}
