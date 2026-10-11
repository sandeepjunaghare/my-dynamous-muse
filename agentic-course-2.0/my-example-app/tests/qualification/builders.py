"""Builders for the qualification suites — manifests with chosen rules, and raw source records.

Rule ids and field names are census-shaped (freight v3's real rules) so a reader recognises them,
but nothing in ``app/`` knows any of them: they reach the evaluator only through manifest rows.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.repository import ManifestRepository
from app.manifests.schemas import (
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestResponse,
    ManifestSource,
    ManifestStatus,
    QualifyingSignal,
    RuleKind,
    RuleOperator,
    ScoreAxis,
    SourceKind,
    Vocabulary,
)
from app.sourcing.schemas import CandidateResponse, SourcingRunResponse
from app.sourcing.service import SourcingService
from tests.manifests.builders import a_vertical, cited
from tests.sourcing.builders import REGISTRY, a_brief, a_candidate

DFW_COUNTIES = ("085", "113", "121", "139", "221", "231", "251", "257", "367", "397", "439")
"""The 11 counties of OMB's CBSA 19100, as FIPS codes — freight v3's ``outside_dfw_metro`` set."""

ROLLUP_RULE = "national_rollup_no_local_owner"
DOUBLE_BROKERING_RULE = "double_brokering_risk"


def predicate(
    rule_id: str,
    field: str,
    operator: RuleOperator,
    value: str | tuple[str, ...] | int | bool | None = None,
    *,
    source: str | None = None,
) -> DisqualifierRule:
    return DisqualifierRule(
        id=rule_id,
        kind=RuleKind.predicate,
        description=f"{field} {operator.value} {value!r}",
        field=field,
        operator=operator,
        value=value,
        source=source,
    )


def judgment(rule_id: str = ROLLUP_RULE, description: str | None = None) -> DisqualifierRule:
    return DisqualifierRule(
        id=rule_id,
        kind=RuleKind.judgment,
        description=description
        or "A national rollup with no local owner cannot buy a $999 assessment locally.",
    )


def signal(signal_id: str, feeds: ScoreAxis) -> QualifyingSignal:
    return QualifyingSignal(id=signal_id, question=f"what about {signal_id}", feeds=feeds)


FREIGHT_V3_PREDICATES = (
    predicate("outside_dfw_metro", "phy_cnty", RuleOperator.not_in_set, DFW_COUNTIES),
    predicate("no_broker_entity_type", "carship", RuleOperator.not_contains, "B"),
    predicate("asset_based_carrier", "power_units", RuleOperator.greater_than, 10),
)


def a_body_with(
    *rules: DisqualifierRule,
    sources: tuple[tuple[str, SourceKind], ...] = (("fmcsa", SourceKind.bulk_file),),
    signals: tuple[QualifyingSignal, ...] = (),
) -> ManifestBody:
    """A valid manifest body holding exactly these rules, sources and signals."""
    return ManifestBody(
        sources=tuple(
            cited(
                ManifestSource(
                    name=name,
                    kind=kind,
                    description=f"the {name} source",
                    base_url=f"https://{name}.example.gov/",
                )
            )
            for name, kind in sources
        ),
        disqualifier_rules=tuple(cited(rule) for rule in rules),
        qualifying_signals=tuple(cited(item) for item in signals),
        icp_band=cited(IcpBand(headcount_min=1, headcount_max=50, requires_office_function=True)),
        vocabulary=cited(Vocabulary(terms=("loads",))),
    )


def census_row(**fields: str | None) -> dict[str, str | None]:
    """A census-shaped row: an in-DFW broker with five power units, unless overridden."""
    row: dict[str, str | None] = {
        "dot_number": "1000001",
        "phy_cnty": "113",
        "carship": "C;B",
        "power_units": "5",
    }
    row.update(fields)
    return row


def a_manifest(body: ManifestBody) -> ManifestResponse:
    """An in-memory DRAFT row for code that takes a ``ManifestResponse`` and needs no database."""
    return ManifestResponse(
        id=uuid4(),
        vertical=a_vertical(),
        version=1,
        status=ManifestStatus.draft,
        body=body,
        created_at=datetime(2026, 10, 10, tzinfo=UTC),
        activated_at=None,
        activated_by=None,
    )


async def an_active_manifest_with(session: AsyncSession, body: ManifestBody) -> ManifestResponse:
    """Create and activate a manifest holding ``body`` under a throwaway vertical; no commit.

    Through the repository, like ``tests/sourcing/builders.an_active_manifest``, so the setup stays
    inside the test's rolled-back transaction and bypasses the dry-run gate on purpose.
    """
    repository = ManifestRepository(session)
    manifest = await repository.create_draft(a_vertical(), body)
    await repository.mark_active(manifest, body, "test")
    return ManifestResponse.model_validate(manifest)


async def a_run_with(
    session: AsyncSession, body: ManifestBody, *registry_ids: str
) -> tuple[ManifestResponse, SourcingRunResponse, list[CandidateResponse]]:
    """An active manifest, a running run under it, and one recorded candidate per registry id."""
    manifest = await an_active_manifest_with(session, body)
    service = SourcingService(session)
    run = await service.start_run(a_brief(manifest.vertical))
    candidates = await service.record_candidates(
        run.id, [a_candidate(registry_id) for registry_id in registry_ids], stage=REGISTRY
    )
    return manifest, run, candidates
