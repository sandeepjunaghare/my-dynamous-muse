"""Builders for the promotion suites: a candidate that clears every check, and its variants."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import CandidateFields, CandidateResponse, Principal, PriorityScore
from tests.sourcing.builders import a_candidate, place_checked, sourced

OWNER_NAME = "DAVE MUSGRAVES"
WEBSITE_PAGE = "https://www.acme-logistics.test/"
JUDGMENT_PAGE = "https://www.acme-logistics.test/about"


def census_row_url(registry_id: str) -> str:
    """The per-carrier census row a registry id is cited to — the company's recovery key."""
    return f"https://data.transportation.gov/resource/az4n-8mr2.json?dot_number={registry_id}"


def an_owner(name: str = OWNER_NAME, title: str | None = None) -> ProvenancedValue[Principal]:
    """A census-cited owner, the way the registry adapter fills it."""
    return sourced(Principal(name=name, title=title))


def a_score(intensity: int, automatable: int) -> ProvenancedValue[PriorityScore]:
    """A priority score cited to a page the judgment node read."""
    return sourced(
        PriorityScore(intensity=intensity, automatable=automatable),
        JUDGMENT_PAGE,
        RetrievalMethod.llm_inference,
    )


def a_promotable(
    registry_id: str = "1000001",
    *,
    phone: str = "+1-817-555-0100",
    website: str | None = None,
    owner: str | None = OWNER_NAME,
) -> CandidateFields:
    """A candidate carrying every required field: identity, a verified address and an owner.

    ``registry_id`` is cited to its own census row, as the census adapter does, so the company's
    ``lpe_source_url`` is distinct per candidate. ``owner=None`` builds the no-principal case.
    """
    base = a_candidate(registry_id)
    return base.model_copy(
        update={
            "registry_id": sourced(registry_id, census_row_url(registry_id)),
            "phone": sourced(phone),
            "business_check": place_checked(f"ChIJ-{registry_id}"),
            "owner": an_owner(owner) if owner is not None else None,
            "website": sourced(website, WEBSITE_PAGE, RetrievalMethod.web_lookup)
            if website is not None
            else None,
        }
    )


def as_response(fields: CandidateFields, *, run_id: UUID | None = None) -> CandidateResponse:
    """An in-memory candidate row, for code that needs no database."""
    moment = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    return CandidateResponse(
        id=uuid4(),
        run_id=run_id or uuid4(),
        registry_id=fields.registry_id.value,
        fields=fields,
        created_at=moment,
        updated_at=moment,
    )
