"""Builders for the sourcing suites — a brief, a cited field, a candidate, an active manifest."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.manifests.repository import ManifestRepository
from app.manifests.schemas import IcpBand
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import CandidateFields, PostalAddress, SourcingBrief
from tests.manifests.builders import a_body, a_vertical

RETRIEVED_AT = datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
CENSUS_URL = "https://ai.fmcsa.dot.gov/SMS/Tools/Downloads.aspx"
QCMOBILE_URL = "https://mobile.fmcsa.dot.gov/qc/services/carriers/1234567"


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
