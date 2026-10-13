"""``sync_reviews``: the founder's decision in HubSpot → the cadence, recorded in the ledger.

The enrolment is T11's ``CadenceService.enrol``, run for real against the same fake portal, so the
hand-off is tested end to end: one task, on the right contact and company, and a cadence row.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.models import CadenceState
from app.cadence.service import CadenceService
from app.promotion.client import HubSpotClient
from app.promotion.models import Promotion
from app.promotion.schemas import PromotedProspect, ReviewOutcome, ReviewSyncReport
from app.promotion.service import PromotionService
from tests.cadence.conftest import FakeClock
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
