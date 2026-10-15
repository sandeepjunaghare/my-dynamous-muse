"""``promote_run`` against a real ledger and a fake CRM: who is created, who is not, and recovery.

"Nothing happens" tests are paired with "and the next real thing does" (T13 lesson): a re-run that
promotes nothing is followed by a new candidate that is still promoted.
"""

from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.promotion import service as service_module
from app.promotion.client import API_VERSION, ProvenancedProperty, to_property_payload
from app.promotion.exceptions import RunNotPromotableError, UnprovenancedWriteError
from app.promotion.models import Promotion
from app.promotion.repository import PromotionRepository
from app.promotion.schemas import PromotionReport, PromotionStatus
from app.promotion.service import PromotionService
from app.sourcing.exceptions import SourcingRunNotFoundError
from app.sourcing.schemas import RunOutcome
from tests.cadence.conftest import FakeClock
from tests.conftest import requires_db
from tests.promotion.builders import a_promotable, a_score, census_row_url
from tests.promotion.conftest import DEFAULT_OWNER, PORTAL_ID, load_fixture
from tests.promotion.crm import FakeCrm
from tests.promotion.scenario import a_finished_run, disqualify, route

pytestmark = requires_db


async def _ledger(session: AsyncSession, vertical: str) -> dict[str, Promotion]:
    rows = await session.scalars(
        select(Promotion)
        .where(Promotion.vertical == vertical)
        .execution_options(populate_existing=True)
    )
    return {row.registry_id: row for row in rows}


def _nonzero(report: PromotionReport) -> dict[str, int]:
    return {name: count for name, count in report.counts.items() if count}


def _losing_duplicate_marks[**P](
    real: Callable[P, Awaitable[Promotion | None]],
) -> Callable[P, Awaitable[Promotion | None]]:
    """Wrap ``PromotionRepository.transition`` so a move to ``duplicate`` loses its race."""

    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Promotion | None:
        if kwargs.get("to_status") is PromotionStatus.duplicate:
            return None
        return await real(*args, **kwargs)

    return wrapper


class TestHappyPath:
    async def test_each_eligible_candidate_becomes_a_company_and_its_contact(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001"), a_promotable("1000002", phone="+1-214-555-0101")]
        )
        await route(db_session, scenario, "1000001", "A · 76011")

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 2, "created": 2}
        assert len(crm.companies) == 2
        first = report.created[0]
        assert first.company_id is not None and first.contact_id is not None
        assert crm.contacts_of(first.company_id) == [first.contact_id]
        assert crm.company_property(first.company_id, "lpe_review_status") == "pending_review"
        assert crm.company_property(first.company_id, "lpe_route_cluster") == "A · 76011"
        assert crm.company_property(first.company_id, "lpe_vertical") == scenario.vertical
        assert crm.company_property(first.company_id, "lpe_source_url") == census_row_url("1000001")
        assert crm.company_property(first.company_id, "hubspot_owner_id") == DEFAULT_OWNER
        assert crm.contact_property(first.contact_id, "firstname") == "DAVE"
        assert crm.contact_property(first.contact_id, "lastname") == "MUSGRAVES"
        assert first.company_url == (
            f"https://app-na2.hubspot.com/contacts/{PORTAL_ID}/record/0-2/{first.company_id}"
        )
        second = report.created[1]
        assert second.company_id is not None
        assert crm.company_property(second.company_id, "lpe_route_cluster") is None

    async def test_the_ledger_records_both_ids_at_pending_review(
        self, db_session: AsyncSession, promotion: PromotionService
    ) -> None:
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        report = await promotion.promote_run(scenario.run.id)

        row = (await _ledger(db_session, scenario.vertical))["1000001"]
        assert row.status == PromotionStatus.pending_review.value
        assert (row.hubspot_company_id, row.hubspot_contact_id) == (
            report.created[0].company_id,
            report.created[0].contact_id,
        )
        assert row.company_key == census_row_url("1000001")
        assert row.dedupe_phone == "8175550100"

    async def test_the_review_properties_are_provisioned_first(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        await promotion.promote_run(scenario.run.id)

        assert ("companies", "lpe_review_status") in crm.provisioned
        first_create = next(
            index
            for index, request in enumerate(crm.portal.requests)
            if request.method == "POST"
            and request.url.path == f"/crm/objects/{API_VERSION}/companies"
        )
        last_provision = max(
            index
            for index, request in enumerate(crm.portal.requests)
            if "/crm/properties/" in request.url.path
        )
        assert last_provision < first_create

    async def test_a_cited_score_reaches_the_company(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        fields = a_promotable("1000001").model_copy(update={"priority": a_score(4, 5)})
        scenario = await a_finished_run(db_session, [fields])

        report = await promotion.promote_run(scenario.run.id)

        company_id = report.created[0].company_id
        assert company_id is not None
        assert crm.company_property(company_id, "lpe_priority_score") == "20"

    async def test_an_ambiguous_name_is_flagged_and_counted(
        self, db_session: AsyncSession, promotion: PromotionService
    ) -> None:
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001", owner="JUAN JOSE DE LEON")]
        )

        report = await promotion.promote_run(scenario.run.id)

        assert report.counts["name_ambiguous"] == 1
        assert report.created[0].name_ambiguous
        assert (await _ledger(db_session, scenario.vertical))["1000001"].name_ambiguous


class TestIdempotency:
    async def test_a_rerun_promotes_nothing_and_a_new_candidate_still_promotes(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        first = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(first.run.id)
        writes = crm.writes()

        again = await promotion.promote_run(first.run.id)

        assert _nonzero(again) == {"considered": 1, "already_promoted": 1}
        assert crm.writes() == writes

        second = await a_finished_run(
            db_session,
            [a_promotable("1000001"), a_promotable("1000003", phone="+1-972-555-0103")],
            manifest=first.manifest,
        )
        report = await promotion.promote_run(second.run.id)

        assert _nonzero(report) == {"considered": 2, "already_promoted": 1, "created": 1}
        assert [p.registry_id for p in report.created] == ["1000003"]
        assert len(crm.companies) == 2


class TestRefusals:
    async def test_refused_candidates_send_nothing_and_leave_no_row(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        no_check = a_promotable("1000003", phone="+1-972-555-0103").model_copy(
            update={"business_check": None}
        )
        dead = a_promotable("1000004", phone="+1-972-555-0104").model_copy(
            update={"priority": a_score(2, 2)}
        )
        scenario = await a_finished_run(
            db_session,
            [
                a_promotable("1000001"),
                a_promotable("1000002", phone="+1-972-555-0102", owner=None),
                no_check,
                dead,
            ],
        )
        await disqualify(db_session, scenario, "1000001")

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {
            "considered": 4,
            "refused_disqualified": 1,
            "refused_no_principal": 1,
            "refused_missing_fields": 1,
            "refused_dead_score": 1,
        }
        assert report.missing_field_tally == {"business_check": 1}
        assert crm.writes() == 0
        assert await _ledger(db_session, scenario.vertical) == {}

    @pytest.mark.parametrize("outcome", [RunOutcome.failed, None])
    async def test_a_failed_or_running_run_is_refused(
        self, db_session: AsyncSession, promotion: PromotionService, outcome: RunOutcome | None
    ) -> None:
        scenario = await a_finished_run(db_session, [a_promotable("1000001")], outcome=outcome)

        with pytest.raises(RunNotPromotableError):
            await promotion.promote_run(scenario.run.id)

    async def test_a_degraded_run_is_promoted(
        self, db_session: AsyncSession, promotion: PromotionService
    ) -> None:
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001")], outcome=RunOutcome.degraded
        )

        assert (await promotion.promote_run(scenario.run.id)).counts["created"] == 1

    async def test_an_unknown_run_is_not_found(self, promotion: PromotionService) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await promotion.promote_run(uuid4())


class TestDedupe:
    async def test_a_domain_match_in_hubspot_is_flagged_and_left_alone(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        existing = crm.add_company(domain="acme-logistics.test", name="ACME (hand-typed)")
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001", website="https://www.acme-logistics.test/")]
        )

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 1, "duplicate_in_hubspot": 1}
        assert report.duplicates[0].matched_company_id == existing
        assert crm.writes() == 0
        row = (await _ledger(db_session, scenario.vertical))["1000001"]
        assert (row.status, row.matched_company_id) == ("duplicate", existing)

    async def test_a_phone_match_in_hubspot_is_flagged(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        existing = crm.add_company(phone="(817) 555-0100")
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        report = await promotion.promote_run(scenario.run.id)

        assert report.counts["duplicate_in_hubspot"] == 1
        assert report.duplicates[0].matched_company_url == (
            f"https://app-na2.hubspot.com/contacts/{PORTAL_ID}/record/0-2/{existing}"
        )
        assert crm.writes() == 0

    async def test_two_candidates_sharing_a_phone_create_once_though_search_lags(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        """The broker and its carrier arm: HubSpot's index cannot see the first create yet."""
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001"), a_promotable("1000002")]
        )

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 2, "created": 1, "duplicate_in_ledger": 1}
        assert report.duplicates[0].duplicate_of == "1000001"
        assert len(crm.companies) == 1
        ledger = await _ledger(db_session, scenario.vertical)
        assert ledger["1000002"].duplicate_of_id == ledger["1000001"].id


class TestNoPartialWrites:
    async def test_a_contact_create_failure_leaves_the_company_recorded(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        crm.fail_contact_creates = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 1, "failed": 1}
        row = (await _ledger(db_session, scenario.vertical))["1000001"]
        assert row.status == "pending_create"
        assert row.hubspot_company_id is not None and row.hubspot_contact_id is None

    async def test_a_contact_the_gate_refuses_creates_no_company(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 7: both payloads pass the gate before the company create (PR #33 L4)."""

        def refusing_contacts(
            properties: Mapping[str, ProvenancedProperty | None],
        ) -> dict[str, str]:
            if "lastname" in properties:
                raise UnprovenancedWriteError("lastname cannot be cited", fields=("lastname",))
            return to_property_payload(properties)

        monkeypatch.setattr(service_module, "to_property_payload", refusing_contacts)
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 1, "failed": 1}
        assert crm.writes() == 0
        assert await _ledger(db_session, scenario.vertical) == {}


class TestRaces:
    async def test_a_claim_lost_to_another_promote_counts_already_promoted(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another promote inserts the row between our read and our claim (PR #33 L5)."""
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(scenario.run.id)

        async def unseen(
            self: PromotionRepository, vertical: str, registry_id: str
        ) -> Promotion | None:
            return None

        monkeypatch.setattr(PromotionRepository, "get_by_registry_id", unseen)
        writes = crm.writes()

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 1, "already_promoted": 1}
        assert crm.writes() == writes

    async def test_a_duplicate_mark_lost_to_another_pass_is_not_counted(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The compare-and-set decides, as it does for every other move (PR #33 L6)."""
        crm.add_company(phone="(214) 555-0109")
        scenario = await a_finished_run(
            db_session, [a_promotable("1000009", phone="+1-214-555-0109")]
        )
        monkeypatch.setattr(
            PromotionRepository,
            "transition",
            _losing_duplicate_marks(PromotionRepository.transition),
        )

        report = await promotion.promote_run(scenario.run.id)

        assert _nonzero(report) == {"considered": 1}
        assert report.duplicates == ()


class TestRecovery:
    async def test_an_ambiguous_company_create_is_found_after_the_lag_not_made_twice(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        crm_clock: FakeClock,
    ) -> None:
        crm.lose_company_create_responses = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])

        first = await promotion.promote_run(scenario.run.id)
        assert _nonzero(first) == {"considered": 1, "failed": 1}
        assert len(crm.companies) == 1

        crm_clock.advance(minutes=5)
        early = await promotion.promote_run(scenario.run.id)
        assert _nonzero(early) == {"considered": 1, "already_promoted": 1, "recovery_deferred": 1}

        crm.catch_up()
        crm_clock.advance(minutes=6)
        late = await promotion.promote_run(scenario.run.id)

        assert _nonzero(late) == {"considered": 1, "already_promoted": 1, "recovered": 1}
        assert len(crm.companies) == 1
        (company_id,) = crm.companies
        assert len(crm.contacts_of(company_id)) == 1
        row = (await _ledger(db_session, scenario.vertical))["1000001"]
        assert (row.status, row.hubspot_company_id) == ("pending_review", company_id)

    async def test_an_unambiguous_company_failure_is_created_once_on_the_next_pass(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        crm_clock: FakeClock,
    ) -> None:
        crm.fail_company_creates = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(scenario.run.id)
        assert crm.companies == {}

        crm_clock.advance(minutes=11)
        report = await promotion.promote_run(scenario.run.id)

        assert report.counts["recovered"] == 1
        assert len(crm.companies) == 1

    async def test_a_second_ambiguous_create_waits_out_the_lag_again(
        self,
        db_session: AsyncSession,
        promotion: PromotionService,
        crm: FakeCrm,
        crm_clock: FakeClock,
    ) -> None:
        """The wait counts from the last create attempt, not from the claim (PR #33 M1)."""
        crm.fail_company_creates = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(scenario.run.id)
        assert crm.companies == {}

        crm_clock.advance(minutes=11)
        crm.lose_company_create_responses = 1
        second = await promotion.promote_run(scenario.run.id)
        assert second.counts["failed"] == 1
        assert len(crm.companies) == 1

        crm_clock.advance(minutes=1)
        early = await promotion.promote_run(scenario.run.id)
        assert _nonzero(early) == {"considered": 1, "already_promoted": 1, "recovery_deferred": 1}
        assert len(crm.companies) == 1

        crm.catch_up()
        crm_clock.advance(minutes=10)
        late = await promotion.promote_run(scenario.run.id)

        assert late.counts["recovered"] == 1
        assert len(crm.companies) == 1

    async def test_an_ambiguous_contact_create_is_found_by_the_association_walk(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        crm.lose_contact_create_responses = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(scenario.run.id)
        (company_id,) = crm.companies
        assert len(crm.contacts_of(company_id)) == 1

        report = await promotion.promote_run(scenario.run.id)

        assert report.counts["recovered"] == 1
        assert len(crm.contacts) == 1
        assert len(crm.contacts_of(company_id)) == 1
        row = (await _ledger(db_session, scenario.vertical))["1000001"]
        assert row.status == "pending_review"
        assert row.hubspot_contact_id == crm.contacts_of(company_id)[0]

    async def test_a_contact_failure_is_created_on_the_next_pass(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        crm.fail_contact_creates = 1
        scenario = await a_finished_run(db_session, [a_promotable("1000001")])
        await promotion.promote_run(scenario.run.id)

        report = await promotion.promote_run(scenario.run.id)

        assert report.counts["recovered"] == 1
        (company_id,) = crm.companies
        assert len(crm.contacts_of(company_id)) == 1


class TestStoppingThePass:
    async def test_an_auth_failure_stops_the_pass(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        scenario = await a_finished_run(
            db_session, [a_promotable("1000001"), a_promotable("1000002", phone="+1-972-555-0102")]
        )
        await promotion.promote_run(scenario.run.id, dry_run=True)  # reads work
        crm.portal.prepend(("POST", "/companies/search", _forbidden))

        report = await promotion.promote_run(scenario.run.id)

        assert report.stopped is not None
        assert report.counts["failed"] == 1
        assert report.counts["considered"] == 1
        assert crm.companies == {}


def _forbidden(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json=load_fixture("error_forbidden"))


class TestDryRun:
    async def test_a_dry_run_counts_what_a_real_pass_does_and_writes_nothing(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        crm.add_company(phone="(214) 555-0109")
        scenario = await a_finished_run(
            db_session,
            [
                a_promotable("1000001"),
                a_promotable("1000002"),
                a_promotable("1000009", phone="+1-214-555-0109"),
                a_promotable("1000010", phone="+1-214-555-0110", owner=None),
            ],
        )

        dry = await promotion.promote_run(scenario.run.id, dry_run=True)

        assert crm.writes() == 0
        assert crm.provisioned == {}
        assert await _ledger(db_session, scenario.vertical) == {}
        real = await promotion.promote_run(scenario.run.id)
        assert dry.counts == real.counts
        assert [p.registry_id for p in dry.created] == [p.registry_id for p in real.created]

    async def test_a_key_shared_with_a_portal_duplicate_is_sorted_as_the_real_pass_sorts_it(
        self, db_session: AsyncSession, promotion: PromotionService, crm: FakeCrm
    ) -> None:
        """A portal duplicate leaves no ledger key behind, so the next one is a portal hit too
        (PR #33 L3)."""
        crm.add_company(phone="(214) 555-0109")
        scenario = await a_finished_run(
            db_session,
            [
                a_promotable("1000009", phone="+1-214-555-0109"),
                a_promotable("1000011", phone="+1-214-555-0109"),
            ],
        )

        dry = await promotion.promote_run(scenario.run.id, dry_run=True)
        real = await promotion.promote_run(scenario.run.id)

        assert real.counts["duplicate_in_hubspot"] == 2
        assert dry.counts == real.counts
