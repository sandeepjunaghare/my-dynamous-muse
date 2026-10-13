"""A promotable candidate → the HubSpot company and contact it becomes. Pure; no I/O, no clock.

**Every mapped property carries exactly the citation of the field it came from.** The address is
cited once, as one unit, and split into HubSpot's four properties, each re-cited with that one
citation; the owner's name is split the same way (``names.py``). A deterministic split of a cited
string is still that string's claim. Values are built as new ``ProvenancedValue``s, never with
``model_copy(update=...)``, which skips the validators (PR #13 M1).

**Optional fields are omitted when absent, never sent as ``None``** — the write-gate refuses
``None``, and an absent ``website`` is honest, not a failure. ``business_check`` is never mapped:
it is the one field allowed to cite Google Maps (D13), and its place ID is not a HubSpot property.

**``lpe_source_url`` and ``lpe_sourced_at`` are derived, not chosen** (OQ6): the company's from
the ``registry_id`` citation, the contact's from the ``owner`` citation, so the provenance a
reviewer reads cannot disagree with the fields it describes. The company's ``lpe_source_url`` is
also the ledger's recovery key: freight's census citation is one URL per USDOT number.

Not mapped, by decision: the contact gets **no email and no phone** (the census phone is the
business's, and its email is the business's stated address — writing either on the person would
claim it is theirs); ``dba_name`` (HubSpot has no standard DBA property); ``headcount_band``
(a follow-up, its HubSpot shape undesigned).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.core.logging import get_logger
from app.promotion.client import ProvenancedProperty
from app.promotion.names import NameSplit, split_owner_name
from app.promotion.schemas import ObjectType, RecordMeta, ReviewStatus
from app.shared.provenance import ProvenancedValue
from app.sourcing.schemas import CandidateFields

logger = get_logger(__name__)

_OBJECT_TYPE_IDS = {ObjectType.companies: "0-2", ObjectType.contacts: "0-1"}


@dataclass(frozen=True)
class CompanyWrite:
    """What a company create sends: gated prospect fields, ungated metadata, the recovery key."""

    properties: Mapping[str, ProvenancedProperty]
    meta: RecordMeta
    key: str
    """The company's ``lpe_source_url`` — how an interrupted create is found again."""
    domain: str | None
    """The website's host, for dedupe; ``None`` without a website."""


@dataclass(frozen=True)
class ContactWrite:
    """What a contact create sends, and how the owner's name was split."""

    properties: Mapping[str, ProvenancedProperty]
    meta: RecordMeta
    name: NameSplit
    source_url: str
    """The owner citation's URL — the contact's ``lpe_source_url``, which recovery matches on."""


def _recited[T, C](value: T, citation: ProvenancedValue[C]) -> ProvenancedValue[T]:
    """``value``, carrying ``citation``'s provenance unchanged."""
    return ProvenancedValue(
        value=value,
        source_url=citation.source_url,
        retrieved_at=citation.retrieved_at,
        retrieval_method=citation.retrieval_method,
    )


def website_domain(website: str) -> str | None:
    """A website's host as a HubSpot ``domain``: lowercased, ``www.`` dropped; ``None`` if none."""
    host = (urlsplit(website.strip()).hostname or "").rstrip(".")
    if host.startswith("www."):
        host = host[len("www.") :]
    return host or None


def company_write(
    fields: CandidateFields,
    *,
    vertical: str,
    route_label: str | None,
    owner_id: str | None,
) -> CompanyWrite:
    """The company for an eligible candidate, at ``lpe_review_status = pending_review``.

    Raises ``ValueError`` if a required field is absent: eligibility runs first, so that is a bug.
    """
    if fields.legal_name is None or fields.address is None or fields.phone is None:
        raise ValueError(f"candidate {fields.registry_id.value} is missing a required field")
    registry = fields.registry_id
    address = fields.address
    properties: dict[str, ProvenancedProperty] = {
        "name": _recited(fields.legal_name.value, fields.legal_name),
        "address": _recited(address.value.street, address),
        "city": _recited(address.value.city, address),
        "state": _recited(address.value.state, address),
        "zip": _recited(address.value.postal_code, address),
        "phone": _recited(fields.phone.value, fields.phone),
        "lpe_source_url": _recited(registry.source_url, registry),
        "lpe_sourced_at": _recited(registry.retrieved_at, registry),
    }
    domain: str | None = None
    if fields.website is not None:
        domain = website_domain(fields.website.value)
        if domain is None:
            logger.warning(
                "promotion.mapping.domain_unparsed", registry_id=fields.registry_id.value
            )
        else:
            properties["domain"] = _recited(domain, fields.website)
    if fields.priority is not None:
        properties["lpe_priority_score"] = _recited(fields.priority.value.score, fields.priority)
    meta = RecordMeta(
        vertical=vertical,
        review_status=ReviewStatus.pending_review,
        route_cluster=route_label,
        owner_id=owner_id,
    )
    return CompanyWrite(properties=properties, meta=meta, key=registry.source_url, domain=domain)


def contact_write(fields: CandidateFields, *, vertical: str, owner_id: str | None) -> ContactWrite:
    """The owner's contact for an eligible candidate. No email, no phone (module docstring).

    Raises ``ValueError`` without an owner: eligibility runs first, so that is a bug.
    """
    if fields.owner is None:
        raise ValueError(f"candidate {fields.registry_id.value} has no owner")
    owner = fields.owner
    name = split_owner_name(owner.value.name)
    properties: dict[str, ProvenancedProperty] = {
        "lastname": _recited(name.last, owner),
        "lpe_source_url": _recited(owner.source_url, owner),
        "lpe_sourced_at": _recited(owner.retrieved_at, owner),
    }
    if name.first is not None:
        properties["firstname"] = _recited(name.first, owner)
    if owner.value.title is not None:
        properties["jobtitle"] = _recited(owner.value.title, owner)
    meta = RecordMeta(vertical=vertical, owner_id=owner_id)
    return ContactWrite(properties=properties, meta=meta, name=name, source_url=owner.source_url)


def record_url(object_type: ObjectType, object_id: str) -> str | None:
    """The record's page in the HubSpot UI, or ``None`` when ``HUBSPOT_PORTAL_ID`` is unset."""
    settings = get_settings()
    portal = (settings.hubspot_portal_id or "").strip()
    if not portal:
        return None
    type_id = _OBJECT_TYPE_IDS[object_type]
    return f"https://{settings.hubspot_ui_domain}/contacts/{portal}/record/{type_id}/{object_id}"
