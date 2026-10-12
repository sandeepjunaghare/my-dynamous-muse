"""The ``promotion`` ledger against a real Postgres: every constraint, and the repository's API.

The ledger is what makes a re-run promote nothing twice, so its guarantees are tested where they
live — in the database — not in a fake.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.promotion.models import Promotion
from app.promotion.repository import PromotionRepository
from app.promotion.schemas import PromotionStatus
from app.sourcing.schemas import CandidateResponse
from tests.conftest import requires_db
from tests.qualification.builders import a_body_with, a_run_with

pytestmark = requires_db

COMPANY_KEY = "https://data.transportation.gov/resource/az4n-8mr2.json?dot_number=1000001"
ENROLLED_AT = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


async def _candidates(
    session: AsyncSession, *registry_ids: str
) -> tuple[str, list[CandidateResponse]]:
    """A running run under a fresh vertical, with one candidate per registry id."""
    manifest, _, candidates = await a_run_with(session, a_body_with(), *registry_ids)
    return manifest.vertical, candidates


def _row(
    vertical: str, candidate: CandidateResponse, **overrides: str | bool | datetime | UUID | None
) -> Promotion:
    """A ledger row in ``pending_create`` for ``candidate``, with any column overridden."""
    row = Promotion(
        vertical=vertical,
        registry_id=candidate.registry_id,
        run_id=candidate.run_id,
        candidate_id=candidate.id,
        status=PromotionStatus.pending_create.value,
        company_key=COMPANY_KEY,
    )
    for name, value in overrides.items():
        setattr(row, name, value)
    return row


async def _claim(
    repository: PromotionRepository,
    vertical: str,
    candidate: CandidateResponse,
    *,
    phone: str | None = None,
    domain: str | None = None,
) -> Promotion | None:
    return await repository.claim(
        candidate,
        vertical=vertical,
        company_key=f"{COMPANY_KEY}&c={candidate.registry_id}",
        dedupe_phone=phone,
        dedupe_domain=domain,
        name_ambiguous=False,
    )


class TestConstraints:
    async def test_a_status_outside_the_set_is_refused(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, status="promoted"))

        with pytest.raises(IntegrityError, match="ck_promotion_status"):
            await db_session.flush()

    async def test_a_blank_company_key_is_refused(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, company_key="  "))

        with pytest.raises(IntegrityError, match="ck_promotion_company_key"):
            await db_session.flush()

    @pytest.mark.parametrize(
        "status", ["pending_review", "enrolling", "enrolled", "rejected", "withdrawn"]
    )
    async def test_past_pending_create_both_hubspot_ids_are_required(
        self, db_session: AsyncSession, status: str
    ) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(
            _row(
                vertical,
                candidate,
                status=status,
                hubspot_company_id="7001",
                enrolled_at=ENROLLED_AT if status == "enrolled" else None,
            )
        )

        with pytest.raises(IntegrityError, match="ck_promotion_ids_match_status"):
            await db_session.flush()

    async def test_pending_create_may_hold_a_company_without_its_contact(
        self, db_session: AsyncSession
    ) -> None:
        """The interrupted-contact state recovery resumes from."""
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, hubspot_company_id="7001"))

        await db_session.flush()

    async def test_a_duplicate_must_name_what_it_duplicates(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, status="duplicate"))

        with pytest.raises(IntegrityError, match="ck_promotion_duplicate_has_match"):
            await db_session.flush()

    async def test_only_a_duplicate_carries_a_match(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, matched_company_id="7001"))

        with pytest.raises(IntegrityError, match="ck_promotion_duplicate_has_match"):
            await db_session.flush()

    async def test_enrolled_requires_enrolled_at(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(
            _row(
                vertical,
                candidate,
                status="enrolled",
                hubspot_company_id="7001",
                hubspot_contact_id="4001",
            )
        )

        with pytest.raises(IntegrityError, match="ck_promotion_enrolled_at"):
            await db_session.flush()

    async def test_enrolled_at_without_enrolled_is_refused(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, candidate, enrolled_at=ENROLLED_AT))

        with pytest.raises(IntegrityError, match="ck_promotion_enrolled_at"):
            await db_session.flush()

    async def test_one_business_has_one_row_per_vertical_across_runs(
        self, db_session: AsyncSession
    ) -> None:
        """The idempotency key: next week's run cannot open a second row for the same business."""
        vertical, (first,) = await _candidates(db_session, "1000001")
        _, (second,) = await _candidates(db_session, "1000001")
        db_session.add(_row(vertical, first))
        await db_session.flush()
        db_session.add(_row(vertical, second))

        with pytest.raises(IntegrityError, match="uq_promotion_vertical_registry_id"):
            await db_session.flush()

    async def test_a_hubspot_company_is_on_one_row_only(self, db_session: AsyncSession) -> None:
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        db_session.add(_row(vertical, one, hubspot_company_id="7001"))
        db_session.add(_row(vertical, two, hubspot_company_id="7001"))

        with pytest.raises(IntegrityError, match="uq_promotion_hubspot_company_id"):
            await db_session.flush()

    async def test_a_candidate_from_another_run_is_refused(self, db_session: AsyncSession) -> None:
        """#20: a row naming a candidate must name that candidate's own run."""
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        _, (other,) = await _candidates(db_session, "1000002")
        db_session.add(_row(vertical, candidate, run_id=other.run_id))

        with pytest.raises(IntegrityError, match="fk_promotion_run_candidate"):
            await db_session.flush()


class TestClaim:
    async def test_a_claim_opens_a_pending_create_row(self, db_session: AsyncSession) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)

        row = await repository.claim(
            candidate,
            vertical=vertical,
            company_key=COMPANY_KEY,
            dedupe_phone="8175550100",
            dedupe_domain="acme.com",
            name_ambiguous=True,
        )

        assert row is not None
        assert row.status == PromotionStatus.pending_create.value
        assert (row.vertical, row.registry_id) == (vertical, "1000001")
        assert (row.run_id, row.candidate_id) == (candidate.run_id, candidate.id)
        assert (row.dedupe_phone, row.dedupe_domain) == ("8175550100", "acme.com")
        assert row.name_ambiguous is True
        assert row.hubspot_company_id is None

    async def test_a_second_claim_of_the_same_business_gets_nothing(
        self, db_session: AsyncSession
    ) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)

        assert await _claim(repository, vertical, candidate) is not None
        assert await _claim(repository, vertical, candidate) is None

    async def test_a_later_run_of_the_same_business_gets_nothing(
        self, db_session: AsyncSession
    ) -> None:
        """Whatever the first row's status — the business has been promoted or attempted."""
        vertical, (first,) = await _candidates(db_session, "1000001")
        _, (later,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        assert await _claim(repository, vertical, first) is not None

        assert await _claim(repository, vertical, later) is None
        assert (await repository.get_by_registry_id(vertical, "1000001")) is not None

    async def test_the_same_registry_id_in_another_vertical_is_its_own_business(
        self, db_session: AsyncSession
    ) -> None:
        """Paired with the refusal above: the key is (vertical, registry id), not registry id."""
        vertical, (first,) = await _candidates(db_session, "1000001")
        other_vertical, (second,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)

        assert await _claim(repository, vertical, first) is not None
        assert await _claim(repository, other_vertical, second) is not None


class TestDedupeKey:
    async def test_a_phone_match_is_found(self, db_session: AsyncSession) -> None:
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        repository = PromotionRepository(db_session)
        first = await _claim(repository, vertical, one, phone="8175550100")
        second = await _claim(repository, vertical, two, phone="8175550100")
        assert first is not None and second is not None

        found = await repository.find_by_dedupe_key(
            vertical, phone="8175550100", domain=None, excluding_id=second.id
        )

        assert found is not None and found.id == first.id

    async def test_a_domain_match_is_found(self, db_session: AsyncSession) -> None:
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        repository = PromotionRepository(db_session)
        first = await _claim(repository, vertical, one, domain="acme.com")
        second = await _claim(repository, vertical, two, phone="2145550199", domain="acme.com")
        assert first is not None and second is not None

        found = await repository.find_by_dedupe_key(
            vertical, phone="2145550199", domain="acme.com", excluding_id=second.id
        )

        assert found is not None and found.id == first.id

    async def test_a_row_does_not_match_itself(self, db_session: AsyncSession) -> None:
        vertical, (one,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        row = await _claim(repository, vertical, one, phone="8175550100")
        assert row is not None

        assert (
            await repository.find_by_dedupe_key(
                vertical, phone="8175550100", domain=None, excluding_id=row.id
            )
            is None
        )

    async def test_a_duplicate_row_is_never_the_match(self, db_session: AsyncSession) -> None:
        """A duplicate created nothing, so pointing at it would point at nothing in HubSpot."""
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        db_session.add(
            _row(vertical, one, status="duplicate", matched_company_id="7001", dedupe_phone="1")
        )
        await db_session.flush()
        repository = PromotionRepository(db_session)
        second = await _claim(repository, vertical, two, phone="1")
        assert second is not None

        assert (
            await repository.find_by_dedupe_key(
                vertical, phone="1", domain=None, excluding_id=second.id
            )
            is None
        )

    async def test_another_vertical_is_never_the_match(self, db_session: AsyncSession) -> None:
        vertical, (one,) = await _candidates(db_session, "1000001")
        other_vertical, (two,) = await _candidates(db_session, "1000002")
        repository = PromotionRepository(db_session)
        await _claim(repository, other_vertical, two, phone="8175550100")
        row = await _claim(repository, vertical, one, phone="8175550100")
        assert row is not None

        assert (
            await repository.find_by_dedupe_key(
                vertical, phone="8175550100", domain=None, excluding_id=row.id
            )
            is None
        )

    async def test_no_key_matches_nothing(self, db_session: AsyncSession) -> None:
        """Two rows with no phone and no domain are not the same business by absence."""
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        repository = PromotionRepository(db_session)
        await _claim(repository, vertical, one)
        row = await _claim(repository, vertical, two)
        assert row is not None

        assert (
            await repository.find_by_dedupe_key(
                vertical, phone=None, domain=None, excluding_id=row.id
            )
            is None
        )


class TestTransition:
    async def test_a_transition_moves_the_row_and_sets_its_values(
        self, db_session: AsyncSession
    ) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        row = await _claim(repository, vertical, candidate)
        assert row is not None

        moved = await repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_review,
            hubspot_company_id="7001",
            hubspot_contact_id="4001",
        )

        assert moved is not None
        assert moved.status == PromotionStatus.pending_review.value
        assert (moved.hubspot_company_id, moved.hubspot_contact_id) == ("7001", "4001")

    async def test_a_value_change_without_a_status_change_is_a_transition_too(
        self, db_session: AsyncSession
    ) -> None:
        """The company id is recorded the moment the create returns, still in pending_create."""
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        row = await _claim(repository, vertical, candidate)
        assert row is not None

        moved = await repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_create,
            hubspot_company_id="7001",
        )

        assert moved is not None and moved.hubspot_company_id == "7001"

    async def test_a_transition_from_the_wrong_status_changes_nothing(
        self, db_session: AsyncSession
    ) -> None:
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        row = await _claim(repository, vertical, candidate)
        assert row is not None

        lost = await repository.transition(
            row.id,
            from_status=PromotionStatus.pending_review,
            to_status=PromotionStatus.rejected,
            decided_at=ENROLLED_AT,
        )

        assert lost is None
        again = await repository.get_by_registry_id(vertical, "1000001")
        assert again is not None
        assert again.status == PromotionStatus.pending_create.value
        assert again.decided_at is None

    async def test_a_transition_loses_to_a_concurrent_one(self, db_session: AsyncSession) -> None:
        """Another process moved the row first. `synchronize_session=False` keeps this session's
        copy stale, as a second process's would be — otherwise the test passes on broken code."""
        vertical, (candidate,) = await _candidates(db_session, "1000001")
        repository = PromotionRepository(db_session)
        row = await _claim(repository, vertical, candidate)
        assert row is not None
        await db_session.execute(
            update(Promotion)
            .where(Promotion.id == row.id)
            .values(status="duplicate", matched_company_id="7999")
            .execution_options(synchronize_session=False)
        )

        lost = await repository.transition(
            row.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_create,
            hubspot_company_id="7001",
        )

        assert lost is None
        stored = (
            await db_session.execute(
                select(Promotion.status, Promotion.hubspot_company_id).where(Promotion.id == row.id)
            )
        ).one()
        assert tuple(stored) == ("duplicate", None)

    async def test_an_unknown_row_is_a_lost_transition(self, db_session: AsyncSession) -> None:
        repository = PromotionRepository(db_session)

        assert (
            await repository.transition(
                uuid4(),
                from_status=PromotionStatus.pending_create,
                to_status=PromotionStatus.withdrawn,
            )
            is None
        )


class TestReads:
    async def test_rows_are_listed_by_status_and_vertical(self, db_session: AsyncSession) -> None:
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        other_vertical, (three,) = await _candidates(db_session, "1000003")
        repository = PromotionRepository(db_session)
        first = await _claim(repository, vertical, one)
        await _claim(repository, vertical, two)
        await _claim(repository, other_vertical, three)
        assert first is not None
        await repository.transition(
            first.id,
            from_status=PromotionStatus.pending_create,
            to_status=PromotionStatus.pending_review,
            hubspot_company_id="7001",
            hubspot_contact_id="4001",
        )

        pending = await repository.list_by_status(
            [PromotionStatus.pending_create], vertical=vertical
        )
        reviewing = await repository.list_by_status([PromotionStatus.pending_review])

        assert [row.registry_id for row in pending] == ["1000002"]
        assert [row.registry_id for row in reviewing] == ["1000001"]

    async def test_a_runs_rows_are_listed_in_registry_order(self, db_session: AsyncSession) -> None:
        vertical, (one, two) = await _candidates(db_session, "1000001", "1000002")
        repository = PromotionRepository(db_session)
        await _claim(repository, vertical, two)
        await _claim(repository, vertical, one)

        rows = await repository.list_for_run(one.run_id)

        assert [row.registry_id for row in rows] == ["1000001", "1000002"]

    async def test_an_unpromoted_business_reads_as_none(self, db_session: AsyncSession) -> None:
        repository = PromotionRepository(db_session)

        assert await repository.get_by_registry_id("freight", "nope") is None
