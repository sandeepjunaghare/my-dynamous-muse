"""The run lifecycle and its refusals, against a real Postgres.

``db_session`` joins an outer transaction with ``create_savepoint``, so the service's
``commit()`` is real and still rolled back at the end of each test.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import BillableKind, RunCost
from app.manifests.exceptions import ActiveManifestNotFoundError
from app.manifests.repository import ManifestRepository
from app.shared.provenance import is_promotable
from app.sourcing.exceptions import SourcingRunNotFoundError, SourcingRunNotRunningError
from app.sourcing.schemas import CandidateFields, RunOutcome, RunStatus, SourcingRunResponse
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.manifests.builders import a_body, a_vertical
from tests.sourcing.builders import (
    PLACES_URL,
    REGISTRY,
    VERIFY,
    a_brief,
    a_candidate,
    an_active_manifest,
    sourced,
    verified,
)

pytestmark = requires_db


async def _started(session: AsyncSession) -> SourcingRunResponse:
    _, vertical = await an_active_manifest(session)
    return await SourcingService(session).start_run(a_brief(vertical))


class TestStartRun:
    async def test_a_vertical_with_only_a_draft_cannot_start(
        self, db_session: AsyncSession
    ) -> None:
        """A run must not source against a proposal nobody accepted."""
        vertical = a_vertical()
        await ManifestRepository(db_session).create_draft(vertical, a_body())

        with pytest.raises(ActiveManifestNotFoundError):
            await SourcingService(db_session).start_run(a_brief(vertical))

    async def test_a_vertical_with_no_manifest_cannot_start(self, db_session: AsyncSession) -> None:
        with pytest.raises(ActiveManifestNotFoundError):
            await SourcingService(db_session).start_run(a_brief())

    async def test_the_run_records_the_active_manifest_version(
        self, db_session: AsyncSession
    ) -> None:
        manifest_id, vertical = await an_active_manifest(db_session)

        run = await SourcingService(db_session).start_run(a_brief(vertical))

        assert run.manifest_id == manifest_id
        assert run.status is RunStatus.running
        assert run.finished_at is None


class TestRecordCandidates:
    async def test_an_unknown_run_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await SourcingService(db_session).record_candidates(
                uuid4(), [a_candidate()], stage=REGISTRY
            )

    async def test_a_finished_run_is_refused(self, db_session: AsyncSession) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)
        await service.finish_run(run.id, RunOutcome.completed, counts={}, cost=RunCost())

        with pytest.raises(SourcingRunNotRunningError) as refused:
            await service.record_candidates(run.id, [a_candidate()], stage=REGISTRY)
        assert refused.value.status == RunStatus.completed.value

    async def test_recording_twice_is_idempotent(self, db_session: AsyncSession) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)
        batch = [a_candidate("200"), a_candidate("100")]

        first = await service.record_candidates(run.id, batch, stage=REGISTRY)
        second = await service.record_candidates(run.id, batch, stage=REGISTRY)

        assert [c.id for c in first] == [c.id for c in second]
        assert [c.fields for c in first] == [c.fields for c in second]
        listed = await service.list_candidates(run.id)
        assert [c.registry_id for c in listed] == ["100", "200"]

    async def test_storable_but_not_promotable_end_to_end(self, db_session: AsyncSession) -> None:
        """The ticket's rule through the service: recorded, listed, and refused by the gate."""
        service = SourcingService(db_session)
        run = await _started(db_session)

        await service.record_candidates(run.id, [a_candidate(with_phone=False)], stage=REGISTRY)
        db_session.expunge_all()
        [candidate] = await service.list_candidates(run.id)

        assert candidate.fields.phone is None
        assert is_promotable(candidate.fields.phone) is False
        assert is_promotable(candidate.fields.legal_name) is True
        assert "phone" in candidate.fields.unprovenanced_fields()

    async def test_the_writing_stage_reaches_the_merge(self, db_session: AsyncSession) -> None:
        """Field ownership holds through the service: a registry retry keeps the verified phone."""
        service = SourcingService(db_session)
        run = await _started(db_session)
        await service.record_candidates(run.id, [a_candidate()], stage=REGISTRY)
        await service.record_candidates(
            run.id,
            [CandidateFields(registry_id=sourced("1234567"), phone=verified("+1-817-555-0199"))],
            stage=VERIFY,
        )

        [retried] = await service.record_candidates(run.id, [a_candidate()], stage=REGISTRY)

        assert retried.fields.phone is not None
        assert retried.fields.phone.source_url == PLACES_URL

    async def test_listing_an_unknown_run_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await SourcingService(db_session).list_candidates(uuid4())


class TestFinishRun:
    async def test_counts_cost_and_outcome_are_persisted(self, db_session: AsyncSession) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)
        cost = RunCost()
        cost.record(BillableKind.places_lookup, count=500, usd=Decimal("8.50"))

        await service.finish_run(
            run.id,
            RunOutcome.degraded,
            counts={"sourced": 600, "qualified": 120, "verified": 80},
            cost=cost,
            detail="places circuit breaker tripped at 500 calls",
        )
        db_session.expunge_all()
        finished = await service.get_run(run.id)

        assert finished.status is RunStatus.degraded
        assert finished.finished_at is not None
        assert finished.finished_at >= finished.started_at
        assert finished.counts == {"sourced": 600, "qualified": 120, "verified": 80}
        assert finished.cost.calls[BillableKind.places_lookup] == 500
        assert finished.cost.total_usd() == Decimal("8.50")
        assert finished.status_detail == "places circuit breaker tripped at 500 calls"

    async def test_a_run_with_no_candidates_finishes_cleanly(
        self, db_session: AsyncSession
    ) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)

        finished = await service.finish_run(
            run.id, RunOutcome.completed, counts={"sourced": 0}, cost=RunCost()
        )

        assert finished.status is RunStatus.completed
        assert finished.cost.total_usd() == Decimal("0")

    async def test_finishing_twice_is_refused(self, db_session: AsyncSession) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)
        await service.finish_run(run.id, RunOutcome.failed, counts={}, cost=RunCost())

        with pytest.raises(SourcingRunNotRunningError):
            await service.finish_run(run.id, RunOutcome.completed, counts={}, cost=RunCost())

    async def test_a_negative_count_writes_nothing(self, db_session: AsyncSession) -> None:
        service = SourcingService(db_session)
        run = await _started(db_session)

        with pytest.raises(ValidationError):
            await service.finish_run(
                run.id, RunOutcome.completed, counts={"sourced": -1}, cost=RunCost()
            )

        still = await service.get_run(run.id)
        assert still.status is RunStatus.running

    async def test_an_unknown_run_is_refused(self, db_session: AsyncSession) -> None:
        with pytest.raises(SourcingRunNotFoundError):
            await SourcingService(db_session).finish_run(
                uuid4(), RunOutcome.completed, counts={}, cost=RunCost()
            )
