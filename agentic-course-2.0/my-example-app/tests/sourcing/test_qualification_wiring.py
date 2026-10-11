"""T7 Phase 6: the free predicates run at the end of ``search_registry``; suppression at selection.

End to end through the runner, the mock FMCSA and a real database: what QCMobile says decides,
nothing is dropped for evidence it lacks, a rejected business is never batched again, and a rule
read from the wrong source refuses the run before the backlog is touched.

Lives here, not in ``tests/tools/`` or ``tests/qualification/``, for the ``session_factory`` and
``MockFmcsa`` fixtures in ``tests/sourcing/conftest.py``.
"""

from typing import cast

import pytest
from sqlalchemy import select

from app.manifests.schemas import DisqualifierRule, ManifestBody, RuleKind, RuleOperator
from app.qualification.exceptions import RuleNotEvaluableError
from app.qualification.service import QualificationService
from app.sourcing.models import SourcingRun
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import CandidateResponse, RunStatus, SourcingRunResponse
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.manifests.builders import cited
from tests.sourcing.builders import a_brief, a_freight_body, an_active_manifest_with
from tests.sourcing.conftest import TEST_WEBKEY, MockFmcsa, SessionFactory, load_fixture
from tests.sourcing.test_pipeline import Boom, registry_stage

pytestmark = requires_db

TARGET = "usdot:1000001"
"""First in best-first order, so a batch of three always takes it."""

TARGET_DOT = TARGET.removeprefix("usdot:")


@pytest.fixture(autouse=True)
def _webkey(monkeypatch: pytest.MonkeyPatch) -> None:
    """A webKey for every test, never whatever ``.env`` holds (as in ``test_pipeline.py``)."""
    monkeypatch.setenv("FMCSA_WEBKEY", TEST_WEBKEY)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "")


async def _candidates(factory: SessionFactory, run: SourcingRunResponse) -> list[CandidateResponse]:
    async with factory() as session:
        return await SourcingService(session).list_candidates(run.id)


async def _runs(factory: SessionFactory, vertical: str) -> list[SourcingRunResponse]:
    async with factory() as session:
        rows = await session.scalars(
            select(SourcingRun)
            .where(SourcingRun.vertical == vertical)
            .order_by(SourcingRun.started_at)
        )
        return [SourcingRunResponse.model_validate(row) for row in rows.all()]


def _broker_rule(*, source: str | None = "fmcsa_qcmobile") -> DisqualifierRule:
    return DisqualifierRule(
        id="broker_authority_not_active",
        kind=RuleKind.predicate,
        description="no active broker authority",
        field="brokerAuthorityStatus",
        operator=RuleOperator.not_equals,
        value="A",
        source=source,
    )


def _body(rule: DisqualifierRule) -> ManifestBody:
    base = a_freight_body()
    return base.model_copy(update={"disqualifier_rules": (*base.disqualifier_rules, cited(rule))})


async def _manifest(factory: SessionFactory, body: ManifestBody) -> str:
    async with factory() as session:
        _, vertical = await an_active_manifest_with(session, body)
        await session.commit()
    return vertical


def _carrier(**changes: object) -> tuple[int, object]:
    body = cast(dict[str, dict[str, dict[str, object]]], load_fixture("qcmobile_carrier"))
    body["content"]["carrier"]["dotNumber"] = int(TARGET_DOT)
    body["content"]["carrier"].update(changes)
    return 200, body


async def _suppressed(factory: SessionFactory, vertical: str) -> frozenset[str]:
    async with factory() as session:
        return await QualificationService(session).suppressed_registry_ids(vertical)


class TestTheFreePredicatesDecide:
    async def test_inactive_broker_authority_disqualifies_cited_to_the_carrier_record(
        self, session_factory: SessionFactory
    ) -> None:
        vertical = await _manifest(session_factory, _body(_broker_rule()))
        mock = MockFmcsa()
        mock.carriers[TARGET_DOT] = _carrier(brokerAuthorityStatus="I")

        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )

        assert run.status is RunStatus.completed
        assert run.counts["qualification_disqualified"] == 1
        assert await _suppressed(session_factory, vertical) == {TARGET}
        (candidate,) = [
            c for c in await _candidates(session_factory, run) if c.registry_id == TARGET
        ]
        async with session_factory() as session:
            (row,) = await QualificationService(session).list_for_candidate(candidate.id)
        assert row.rule_id == "broker_authority_not_active"
        assert row.evidence == "brokerAuthorityStatus='I'"
        assert row.source_url == f"https://mobile.fmcsa.dot.gov/qc/services/carriers/{TARGET_DOT}"

    async def test_a_field_absent_from_a_found_record_fires_the_negated_rule(
        self, session_factory: SessionFactory
    ) -> None:
        """No authority on file: QCMobile returns the status null, which is not stored."""
        vertical = await _manifest(session_factory, _body(_broker_rule()))
        mock = MockFmcsa()
        mock.carriers[TARGET_DOT] = _carrier(brokerAuthorityStatus=None)

        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )

        assert run.counts["qualification_disqualified"] == 1
        (candidate,) = [
            c for c in await _candidates(session_factory, run) if c.registry_id == TARGET
        ]
        async with session_factory() as session:
            (row,) = await QualificationService(session).list_for_candidate(candidate.id)
        assert row.evidence == "brokerAuthorityStatus absent"

    async def test_no_qcmobile_record_is_never_a_drop(
        self, session_factory: SessionFactory
    ) -> None:
        """QCMobile did not know the USDOT: there is no record for the rule to read."""
        vertical = await _manifest(session_factory, _body(_broker_rule()))
        mock = MockFmcsa()
        mock.carriers[TARGET_DOT] = (200, load_fixture("qcmobile_not_found"))

        run = await SourcingPipeline(session_factory, stages=[registry_stage(mock)]).run(
            a_brief(vertical)
        )

        assert run.status is RunStatus.completed
        assert run.counts["lookup_not_found"] == 1
        assert run.counts["qualification_disqualified"] == 0
        assert await _suppressed(session_factory, vertical) == frozenset()


class TestSuppression:
    async def test_a_batch_handed_back_never_re_spends_a_slot_on_a_rejected_business(
        self, session_factory: SessionFactory
    ) -> None:
        """The ticket's two-run test. Run 1 disqualifies, then a later stage fails, so its batch
        goes back to the pool. Run 2 takes the rest of that batch — but not the rejected one."""
        vertical = await _manifest(session_factory, _body(_broker_rule()))
        first = MockFmcsa()
        first.carriers[TARGET_DOT] = _carrier(brokerAuthorityStatus="I")
        with pytest.raises(RuntimeError, match="places is down"):
            await SourcingPipeline(session_factory, stages=[registry_stage(first), Boom()]).run(
                a_brief(vertical)
            )
        (failed,) = await _runs(session_factory, vertical)
        assert failed.status is RunStatus.failed
        handed_back = {c.registry_id for c in await _candidates(session_factory, failed)}
        assert TARGET in handed_back

        second = await SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa())]).run(
            a_brief(vertical)
        )

        taken = {c.registry_id for c in await _candidates(session_factory, second)}
        assert TARGET not in taken
        assert handed_back - {TARGET} <= taken
        assert second.counts["pool_suppressed"] == 1
        assert second.counts["pool_available"] == 7


class TestAMisroutedRule:
    async def test_a_qcmobile_field_read_from_the_census_refuses_before_any_batch(
        self, session_factory: SessionFactory
    ) -> None:
        """Freight v6's shape: no ``source``, so the rule reads the census, which never holds
        ``brokerAuthorityStatus`` — absent on every record, so it would fire on every one."""
        vertical = await _manifest(session_factory, _body(_broker_rule(source=None)))

        with pytest.raises(RuleNotEvaluableError, match="set the rule's `source`"):
            await SourcingPipeline(session_factory, stages=[registry_stage(MockFmcsa())]).run(
                a_brief(vertical)
            )

        (run,) = await _runs(session_factory, vertical)
        assert run.status is RunStatus.failed
        assert await _candidates(session_factory, run) == []
        assert await _suppressed(session_factory, vertical) == frozenset()
