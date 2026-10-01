"""seed the freight and fire manifests, both DRAFT

M9 says vertical #2 costs under a founder-day and no rebuild. Seeding two verticals is how that
gets *proved* rather than asserted: the same five stages read both rows, and nothing in `app/`
knows what either word means.

**Neither row is activated, and that is the point.** Fire declares Google Places, whose terms of
use are an open question that explicitly blocks marking it active; freight is activated by a person
running `lpe manifest activate`, because a migration that could write an ACTIVE row would be a
second door around the gate this ticket exists to be.

The bodies are built through the Pydantic schema and dumped with `model_dump(mode="json")` rather
than hand-written as JSON literals: a hand-written blob drifts from the schema silently, whereas
this fails loudly the day the schema changes. Ids and timestamps are fixed literals so the
migration is deterministic in every environment.

Revision ID: 0003_seed_freight_and_fire
Revises: 0002_vertical_manifest
Create Date: 2026-09-27

"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.manifests.schemas import (
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestSource,
    ManifestStatus,
    QualifyingSignal,
    RuleKind,
    RuleOperator,
    ScoreAxis,
    SourceKind,
    Vocabulary,
)
from app.shared.provenance import ProvenancedValue, RetrievalMethod

revision: str = "0003_seed_freight_and_fire"
down_revision: str | None = "0002_vertical_manifest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FREIGHT_ID = UUID("0193f7a1-0000-7000-8000-000000000001")
FIRE_ID = UUID("0193f7a1-0000-7000-8000-000000000002")
RESEARCHED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

SAFER_URL = "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx"
QCMOBILE_URL = "https://mobile.fmcsa.dot.gov/QCDevsite/docs/getStarted"
TX_FIRE_MARSHAL_URL = "https://www.tdi.texas.gov/fire/fmlicense.html"
PLACES_URL = "https://developers.google.com/maps/documentation/places/web-service/overview"


def _cited[T](value: T, source_url: str) -> ProvenancedValue[T]:
    """Wrap a hand-researched value in its citation.

    `manual_research`, not `web_lookup`: a person read the documentation and wrote the row. T12's
    authoring agent will cite `llm_inference` for the same fields.
    """
    return ProvenancedValue(
        value=value,
        source_url=source_url,
        retrieved_at=RESEARCHED_AT,
        retrieval_method=RetrievalMethod.manual_research,
    )


def _freight_body() -> ManifestBody:
    """Freight & 3PL — vertical #1 (D2/D7), sourced from FMCSA."""
    return ManifestBody(
        sources=(
            _cited(
                ManifestSource(
                    name="fmcsa",
                    kind=SourceKind.bulk_file,
                    description=(
                        "FMCSA. Geography comes from the bulk Company Census File filtered to DFW "
                        "counties and active broker authority; QCMobile is then a per-record "
                        "lookup keyed on USDOT/MC for authority status, fleet size and BOC-3 "
                        "filings. QCMobile cannot search by geography, which is why the census "
                        "file is the entry point (E10)."
                    ),
                    base_url=SAFER_URL,
                ),
                QCMOBILE_URL,
            ),
        ),
        disqualifier_rules=(
            _cited(
                DisqualifierRule(
                    id="asset_based_carrier",
                    kind=RuleKind.predicate,
                    description=(
                        "Asset-based carriers are not the ICP — the target is non-asset "
                        "brokerages with an office function."
                    ),
                    field="carrier_operation",
                    operator=RuleOperator.equals,
                    value="asset_based",
                ),
                SAFER_URL,
            ),
            _cited(
                DisqualifierRule(
                    id="double_brokering_risk",
                    kind=RuleKind.judgment,
                    description=(
                        "Authority history and filings suggesting double-brokering. Needs "
                        "judgment across several weak signals, so it is a classify_rollup node "
                        "call, not a field comparison."
                    ),
                ),
                SAFER_URL,
            ),
        ),
        qualifying_signals=(
            _cited(
                QualifyingSignal(
                    id="tms_and_api",
                    question="which TMS, and does it have an API",
                    feeds=ScoreAxis.automatable,
                ),
                SAFER_URL,
            ),
        ),
        icp_band=_cited(
            IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True),
            SAFER_URL,
        ),
        vocabulary=_cited(
            Vocabulary(
                terms=(
                    "loads",
                    "lanes",
                    "carrier packets",
                    "COIs",
                    "check calls",
                    "detention",
                )
            ),
            SAFER_URL,
        ),
    )


def _fire_body() -> ManifestBody:
    """Fire protection — the portability test (M9). Two sources, one of them paid."""
    return ManifestBody(
        sources=(
            _cited(
                ManifestSource(
                    name="tx_fire_marshal",
                    kind=SourceKind.registry_api,
                    description=(
                        "Texas State Fire Marshal licensed-contractor search — the authoritative "
                        "registry of fire protection licensees operating in Texas."
                    ),
                    base_url=TX_FIRE_MARSHAL_URL,
                ),
                TX_FIRE_MARSHAL_URL,
            ),
            _cited(
                ManifestSource(
                    name="places",
                    kind=SourceKind.web_lookup,
                    description=(
                        "Google Places — address and phone verification, plus the review-name "
                        "signal that often surfaces an owner. Paid per request, so it runs after "
                        "the free disqualifier filters, never before."
                    ),
                    base_url=PLACES_URL,
                ),
                PLACES_URL,
            ),
        ),
        disqualifier_rules=(
            _cited(
                DisqualifierRule(
                    id="national_rollup_no_local_owner",
                    kind=RuleKind.judgment,
                    description=(
                        "National rollups with no local owner cannot buy a $999 assessment "
                        "locally (E7: Impact Fire, Summit Fire, Century Fire, Control Systems). "
                        "The call needs judgment about ownership structure, so it belongs to the "
                        "classify_rollup node rather than to a skip list."
                    ),
                ),
                TX_FIRE_MARSHAL_URL,
            ),
        ),
        qualifying_signals=(
            _cited(
                QualifyingSignal(
                    id="owner_accessible",
                    question="is the owner or principal reachable directly at this location",
                    feeds=ScoreAxis.intensity,
                ),
                PLACES_URL,
            ),
        ),
        icp_band=_cited(
            IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True),
            TX_FIRE_MARSHAL_URL,
        ),
        vocabulary=_cited(
            Vocabulary(terms=("inspections", "permits", "AHJ", "renewals")),
            TX_FIRE_MARSHAL_URL,
        ),
    )


def upgrade() -> None:
    """Insert the two DRAFT manifests."""
    manifest_table = sa.table(
        "vertical_manifest",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("vertical", sa.String),
        sa.column("version", sa.Integer),
        sa.column("status", sa.String),
        sa.column("body", postgresql.JSONB),
    )
    op.bulk_insert(
        manifest_table,
        [
            {
                "id": FREIGHT_ID,
                "vertical": "freight",
                "version": 1,
                "status": ManifestStatus.draft.value,
                "body": _freight_body().model_dump(mode="json"),
            },
            {
                "id": FIRE_ID,
                "vertical": "fire",
                "version": 1,
                "status": ManifestStatus.draft.value,
                "body": _fire_body().model_dump(mode="json"),
            },
        ],
    )


def downgrade() -> None:
    """Delete exactly these two rows, by (vertical, version)."""
    op.execute(
        sa.text(
            "delete from vertical_manifest "
            "where (vertical, version) in (('freight', 1), ('fire', 1))"
        )
    )
