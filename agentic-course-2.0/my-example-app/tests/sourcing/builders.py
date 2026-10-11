"""Builders for the sourcing suites — a brief, a cited field, a candidate, an active manifest."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.repository import ManifestRepository
from app.manifests.schemas import (
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestSource,
    RuleKind,
    RuleOperator,
    SourceKind,
)
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import CandidateFields, PlaceCheck, PostalAddress, SourcingBrief
from app.sourcing.stages import PipelineStage
from tests.manifests.builders import a_body, a_vertical, cited

RETRIEVED_AT = datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
CENSUS_URL = "https://ai.fmcsa.dot.gov/SMS/Tools/Downloads.aspx"
QCMOBILE_URL = "https://mobile.fmcsa.dot.gov/qc/services/carriers/1234567"
PLACES_URL = "https://places.googleapis.com/v1/places/ChIJ-acme-logistics"
"""Where a business check is cited. Under D13 nothing else may cite Google Places."""
SEARCH_RESULT_URL = "https://www.acmefreight.test/contact"
"""A web-search hit, the D13 source for a website."""

REGISTRY = PipelineStage.search_registry
"""The stage that owns identity fields; what every plain sourcing write in these suites is."""

VERIFY = PipelineStage.verify_business
"""The stage that owns the business check and the website (D13)."""


def sourced[T](
    value: T,
    source_url: str = CENSUS_URL,
    method: RetrievalMethod = RetrievalMethod.bulk_file,
) -> ProvenancedValue[T]:
    """Cite a value the way a sourcing stage would."""
    return ProvenancedValue(
        value=value,
        source_url=source_url,
        retrieved_at=RETRIEVED_AT,
        retrieval_method=method,
    )


def looked_up[T](value: T) -> ProvenancedValue[T]:
    """Cite a value the way ``verify_business`` would from a web search (D13: never from Places)."""
    return sourced(value, SEARCH_RESULT_URL, RetrievalMethod.web_lookup)


def place_checked(place_id: str = "ChIJ-acme-logistics") -> ProvenancedValue[PlaceCheck]:
    """The one thing ``verify_business`` may keep from Google Places: the place ID (D13)."""
    return sourced(PlaceCheck(place_id=place_id), PLACES_URL, RetrievalMethod.web_lookup)


def a_brief(vertical: str | None = None) -> SourcingBrief:
    """A valid brief. Pass the vertical of a manifest the test has activated."""
    return SourcingBrief(
        vertical=vertical or a_vertical(),
        geography="dfw",
        icp_band=IcpBand(headcount_min=20, headcount_max=200, requires_office_function=True),
    )


def a_candidate(registry_id: str = "1234567", *, with_phone: bool = True) -> CandidateFields:
    """A candidate with every identity field cited, optionally missing its phone."""
    return CandidateFields(
        registry_id=sourced(registry_id),
        legal_name=sourced(f"Acme Logistics {registry_id} LLC"),
        dba_name=sourced("Acme Freight"),
        address=sourced(
            PostalAddress(
                street="2100 Olympic Dr",
                city="Arlington",
                state="TX",
                postal_code="76011",
            )
        ),
        phone=sourced("+1-817-555-0100", QCMOBILE_URL, RetrievalMethod.registry_api)
        if with_phone
        else None,
        website=None,
    )


async def an_active_manifest(session: AsyncSession) -> tuple[UUID, str]:
    """Create and activate a manifest for a throwaway vertical; return its id and vertical.

    Written through the repository rather than the service so the setup does not commit — it stays
    inside the test's rolled-back transaction.
    """
    vertical = a_vertical()
    repository = ManifestRepository(session)
    manifest = await repository.create_draft(vertical, a_body())
    await repository.mark_active(manifest, a_body(), "test")
    return manifest.id, vertical


DFW_COUNTIES = ("085", "113", "121", "139", "231", "251", "257", "367", "397", "439", "497")

CENSUS_SOURCE_URL = "https://data.transportation.gov/d/az4n-8mr2"
QCMOBILE_SOURCE_URL = "https://mobile.fmcsa.dot.gov/qc/services/"
AUTHORITY_HISTORY_URL = "https://data.transportation.gov/d/u4i8-4m26"
MOTUS_REVOCATIONS_URL = "https://data.transportation.gov/d/wb4f-neki"
LEGACY_REVOCATIONS_URL = "https://data.transportation.gov/d/rwr4-5nkg"
"""What freight v3 declares: the frozen pre-Motus file, which T5 deliberately won't bind."""

TX_FIRE_MARSHAL_URL = "https://www.tdi.texas.gov/fire/fmlicense.html"
PLACES_SOURCE_URL = "https://developers.google.com/maps/documentation/places/web-service/overview"


def freight_v3_rules() -> tuple[DisqualifierRule, ...]:
    """Freight v3's disqualifier rules, copied from the dev manifest (``ecdc1c8e``, 2026-10-09)."""
    predicate = RuleKind.predicate
    return (
        DisqualifierRule(
            id="inactive_registration", kind=predicate, description="inactive",
            field="status_code", operator=RuleOperator.equals, value="I",
        ),
        DisqualifierRule(
            id="outside_texas", kind=predicate, description="not TX",
            field="phy_state", operator=RuleOperator.not_equals, value="TX",
        ),
        DisqualifierRule(
            id="outside_dfw_metro", kind=predicate, description="not one of the 11 counties",
            field="phy_cnty", operator=RuleOperator.not_in_set, value=DFW_COUNTIES,
        ),
        DisqualifierRule(
            id="no_broker_entity_type", kind=predicate, description="no broker code",
            field="carship", operator=RuleOperator.not_contains, value="B",
        ),
        DisqualifierRule(
            id="not_allowed_to_operate", kind=predicate, description="QCMobile says N",
            field="allowToOperate", operator=RuleOperator.equals, value="N",
        ),
        DisqualifierRule(
            id="authority_revoked", kind=RuleKind.judgment, description="in the revocations"
        ),
        DisqualifierRule(
            id="asset_based_carrier", kind=predicate, description="a real fleet",
            field="power_units", operator=RuleOperator.greater_than, value=10,
        ),
        DisqualifierRule(
            id="self_employed_shell", kind=RuleKind.judgment, description="no back office"
        ),
    )  # fmt: skip


def _source(name: str, kind: SourceKind, base_url: str) -> ProvenancedValue[ManifestSource]:
    return cited(
        ManifestSource(name=name, kind=kind, description=f"the {name} source", base_url=base_url)
    )


def a_freight_body(*, revocations_url: str = MOTUS_REVOCATIONS_URL) -> ManifestBody:
    """Freight v3's sources and rules — with Motus revocations unless told otherwise."""
    base = a_body()
    return base.model_copy(
        update={
            "sources": (
                _source("fmcsa_company_census", SourceKind.bulk_file, CENSUS_SOURCE_URL),
                _source("fmcsa_qcmobile", SourceKind.registry_api, QCMOBILE_SOURCE_URL),
                _source("fmcsa_authority_history", SourceKind.bulk_file, AUTHORITY_HISTORY_URL),
                _source("fmcsa_revocations", SourceKind.bulk_file, revocations_url),
            ),
            "disqualifier_rules": tuple(cited(rule) for rule in freight_v3_rules()),
        }
    )


def a_fire_body() -> ManifestBody:
    """Fire's sources: the Texas Fire Marshal registry, and Places — which must never discover."""
    base = a_body()
    return base.model_copy(
        update={
            "sources": (
                _source("tx_fire_marshal", SourceKind.registry_api, TX_FIRE_MARSHAL_URL),
                _source("places", SourceKind.web_lookup, PLACES_SOURCE_URL),
            ),
        }
    )


async def an_active_manifest_with(
    session: AsyncSession, body: ManifestBody, *, vertical: str | None = None
) -> tuple[UUID, str]:
    """Like :func:`an_active_manifest`, with the body (and optionally the vertical) given."""
    vertical = vertical or a_vertical()
    repository = ManifestRepository(session)
    manifest = await repository.create_draft(vertical, body)
    await repository.mark_active(manifest, body, "test")
    return manifest.id, vertical
