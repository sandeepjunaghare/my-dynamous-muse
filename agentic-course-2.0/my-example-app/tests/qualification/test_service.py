"""Disqualification and re-source suppression, against a real Postgres.

``db_session`` joins an outer transaction with ``create_savepoint``, so the service's ``commit()``
is real and still rolled back after each test.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.schemas import RuleKind, RuleOperator, SourceKind
from app.qualification.schemas import AdmittedJudgment, AdmittedRule, RecordSet
from app.qualification.service import QualificationService
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.service import SourcingService
from tests.conftest import requires_db
from tests.qualification.builders import (
    DOUBLE_BROKERING_RULE,
    FREIGHT_V3_PREDICATES,
    ROLLUP_RULE,
    a_body_with,
    a_run_with,
    census_row,
    judgment,
    predicate,
)
from tests.sourcing.builders import a_brief

pytestmark = requires_db

NOT_ALLOWED = predicate("not_allowed", "allowToOperate", RuleOperator.is_true, source="qcmobile")
BODY = a_body_with(
    *FREIGHT_V3_PREDICATES,
    NOT_ALLOWED,
    judgment(ROLLUP_RULE),
    judgment(DOUBLE_BROKERING_RULE, "authority history suggests double-brokering"),
    sources=(("fmcsa", SourceKind.bulk_file), ("qcmobile", SourceKind.registry_api)),
)
QCMOBILE_LOOKUP = "https://mobile.fmcsa.dot.gov/qc/services/carriers/555"


def _fired(*rule_ids: str) -> AdmittedJudgment:
    return AdmittedJudgment(
        fired=tuple(
            AdmittedRule(
                rule_id=rule_id,
                reason=ProvenancedValue(
                    value=f"{rule_id}: owned by a national platform",
                    source_url="https://example.test/about",
                    retrieved_at=datetime(2026, 10, 10, tzinfo=UTC),
                    retrieval_method=RetrievalMethod.llm_inference,
                ),
            )
            for rule_id in rule_ids
        ),
        signals=(),
        priority=None,
        omitted=(),
    )


class TestPredicates:
    async def test_the_first_fired_rule_is_recorded_with_its_citation(
        self, db_session: AsyncSession
    ) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "555")
        service = QualificationService(db_session)
        records: RecordSet = {"fmcsa": census_row(), "qcmobile": {"allowToOperate": "Y"}}

        fired = await service.disqualify_by_predicates(
            candidate, run, manifest, records, source_urls={"qcmobile": QCMOBILE_LOOKUP}
        )

        assert fired is not None and fired.rule_id == "not_allowed"
        (row,) = await service.list_for_candidate(candidate.id)
        assert (row.rule_id, row.rule_kind, row.registry_id) == (
            "not_allowed",
            RuleKind.predicate,
            "555",
        )
        assert row.source_url == QCMOBILE_LOOKUP
        assert row.retrieval_method is RetrievalMethod.registry_api
        assert row.evidence == "allowToOperate='Y'"

    async def test_without_a_record_url_the_source_base_url_is_cited(
        self, db_session: AsyncSession
    ) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "556")
        service = QualificationService(db_session)
        await service.disqualify_by_predicates(
            candidate, run, manifest, {"fmcsa": census_row(power_units="40")}
        )
        (row,) = await service.list_for_candidate(candidate.id)
        assert row.rule_id == "asset_based_carrier"
        assert row.source_url == "https://fmcsa.example.gov/"
        assert row.retrieval_method is RetrievalMethod.bulk_file

    async def test_a_survivor_is_not_recorded(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "557")
        service = QualificationService(db_session)
        records: RecordSet = {"fmcsa": census_row(), "qcmobile": {"allowToOperate": "N"}}
        assert await service.disqualify_by_predicates(candidate, run, manifest, records) is None
        assert await service.list_for_candidate(candidate.id) == []

    async def test_an_unjudgeable_record_is_never_disqualified(
        self, db_session: AsyncSession
    ) -> None:
        """No QCMobile record and a blank power-units field: unsure, so it stays."""
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "558")
        service = QualificationService(db_session)
        records = {"fmcsa": census_row(power_units="")}
        assert await service.disqualify_by_predicates(candidate, run, manifest, records) is None
        assert await service.disqualified_candidate_ids(run.id) == frozenset()

    async def test_recording_twice_keeps_one_row(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "559")
        service = QualificationService(db_session)
        records = {"fmcsa": census_row(carship="C")}
        await service.disqualify_by_predicates(candidate, run, manifest, records)
        await service.disqualify_by_predicates(candidate, run, manifest, records)
        assert len(await service.list_for_candidate(candidate.id)) == 1


class TestJudgment:
    async def test_every_fired_judgment_rule_is_recorded(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "600")
        service = QualificationService(db_session)
        rows = await service.record_judgment(
            candidate, run, manifest, _fired(ROLLUP_RULE, DOUBLE_BROKERING_RULE)
        )
        assert sorted(row.rule_id for row in rows) == [DOUBLE_BROKERING_RULE, ROLLUP_RULE]
        assert {row.rule_kind for row in rows} == {RuleKind.judgment}
        assert {row.retrieval_method for row in rows} == {RetrievalMethod.llm_inference}

    async def test_nothing_fired_records_nothing(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "601")
        service = QualificationService(db_session)
        assert await service.record_judgment(candidate, run, manifest, _fired()) == []
        assert await service.disqualified_candidate_ids(run.id) == frozenset()

    async def test_a_retried_judgment_keeps_the_first_row(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "602")
        service = QualificationService(db_session)
        first = await service.record_judgment(candidate, run, manifest, _fired(ROLLUP_RULE))
        again = await service.record_judgment(candidate, run, manifest, _fired(ROLLUP_RULE))
        assert [row.id for row in again] == [row.id for row in first]


class TestSuppressionAcrossRuns:
    async def test_a_rejected_business_is_suppressed_in_the_next_run(
        self, db_session: AsyncSession
    ) -> None:
        """The thing a HubSpot-only model cannot do: run 2 knows what run 1 rejected.

        Run 2 writes *new* candidate rows for the same registry ids, which is why suppression is
        keyed on (vertical, registry id) rather than on the candidate.
        """
        manifest, first_run, (rejected, kept) = await a_run_with(db_session, BODY, "700", "701")
        service = QualificationService(db_session)
        await service.record_judgment(rejected, first_run, manifest, _fired(ROLLUP_RULE))

        sourcing = SourcingService(db_session)
        second_run = await sourcing.start_run(a_brief(manifest.vertical))
        assert second_run.id != first_run.id

        suppressed = await service.suppressed_registry_ids(manifest.vertical, ["700", "701", "702"])
        assert suppressed == frozenset({"700"})
        assert await service.is_suppressed(manifest.vertical, "700")
        assert not await service.is_suppressed(manifest.vertical, kept.registry_id)

    async def test_suppression_is_per_vertical(self, db_session: AsyncSession) -> None:
        manifest, run, (candidate,) = await a_run_with(db_session, BODY, "800")
        other, _, _ = await a_run_with(db_session, BODY)
        service = QualificationService(db_session)
        await service.record_judgment(candidate, run, manifest, _fired(ROLLUP_RULE))
        assert await service.suppressed_registry_ids(other.vertical) == frozenset()
        assert await service.suppressed_registry_ids(manifest.vertical) == frozenset({"800"})

    async def test_an_empty_batch_suppresses_nothing(self, db_session: AsyncSession) -> None:
        manifest, _, _ = await a_run_with(db_session, BODY)
        service = QualificationService(db_session)
        assert await service.suppressed_registry_ids(manifest.vertical, []) == frozenset()
