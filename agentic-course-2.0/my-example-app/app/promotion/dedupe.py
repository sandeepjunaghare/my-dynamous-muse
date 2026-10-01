"""Find the record that already exists, so nothing creates a second one.

The portal holds 3,118 contacts and 1,040 companies of mixed provenance (E14). An engine that
creates before it checks turns a weekly run into a weekly duplicate-generation job, so every
create path runs one of these first.

**Phone matching strips the country code**, because HubSpot's search does. HubSpot standardizes
phone numbers into calculated properties and matches on area code plus local number only; its own
docs say to leave the country code out of search criteria. Every candidate a phone search returns
is then re-checked client-side with :func:`normalize_phone` — HubSpot's standardization is good,
but it is not ours, and a false dedupe silently drops a real prospect.

**What this cannot do, and T9 must not assume it does.** HubSpot's search index lags writes by "a
few moments", with no published SLA. Two candidates in the same run that resolve to the same
company will both miss here and both create. The protection against *that* is T9's ``promotion``
ledger — a record of what this run already promoted. This module's job is to be honest about the
boundary rather than imply a guarantee it cannot make.
"""

from app.core.logging import get_logger
from app.promotion.client import HubSpotClient
from app.promotion.schemas import (
    HubSpotObject,
    ObjectType,
    SearchFilter,
    SearchFilterGroup,
    SearchRequest,
)

logger = get_logger(__name__)

_COMPANY_PROPERTIES = ["domain", "name", "phone"]
_CONTACT_PROPERTIES = ["email", "firstname", "lastname", "phone", "mobilephone"]

# Both confirmed present on CONTACT in portal 244766495 on 2026-09-27.
_COMPANY_PHONE_PROPERTIES = ("phone",)
_CONTACT_PHONE_PROPERTIES = ("phone", "mobilephone")

_US_NATIONAL_DIGITS = 10


def normalize_phone(raw: str) -> str:
    """Reduce a phone number to its 10 national digits, or ``""`` when there are not 10.

    Handles the formats the portal actually contains — ``(214) 555-0147``, ``+1 214-555-0147``,
    ``214.555.0147``, ``2145550147``, and numbers with an extension tacked on the end.

    The empty string is meaningful: it says *this number is not usable as a match key*. A caller
    that searched on it would be asking HubSpot to match on nothing, and a short or garbled number
    that matched everything would be far worse than one that matched nothing.
    """
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) > _US_NATIONAL_DIGITS and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < _US_NATIONAL_DIGITS:
        return ""
    return digits[:_US_NATIONAL_DIGITS]


def _single_filter_search(property_name: str, value: str, properties: list[str]) -> SearchRequest:
    """One filter, one group. The caps are generous; OR-semantics across groups are easy to
    get subtly wrong, and two sequential searches read more plainly than one clever body."""
    return SearchRequest(
        filter_groups=[
            SearchFilterGroup(filters=[SearchFilter(property_name=property_name, value=value)])
        ],
        properties=properties,
    )


def _phone_matches(
    record: HubSpotObject, normalized: str, phone_properties: tuple[str, ...]
) -> bool:
    """Re-check a search hit against our own normalization before calling it a duplicate."""
    for name in phone_properties:
        raw = record.properties.get(name)
        if raw is not None and normalize_phone(raw) == normalized:
            return True
    return False


async def find_existing_company(
    client: HubSpotClient,
    *,
    domain: str | None = None,
    phone: str | None = None,
) -> HubSpotObject | None:
    """Return the company already in HubSpot for this domain or phone, or ``None``.

    Domain first: it is the reliable key. Phone is the fallback for the many local businesses whose
    web presence is a Facebook page.
    """
    if domain:
        response = await client.search(
            ObjectType.companies, _single_filter_search("domain", domain, _COMPANY_PROPERTIES)
        )
        if response.results:
            match = response.results[0]
            logger.info(
                "promotion.hubspot.duplicate_found",
                object_type=ObjectType.companies.value,
                matched_on="domain",
                object_id=match.id,
            )
            return match

    normalized = normalize_phone(phone) if phone else ""
    if normalized:
        response = await client.search(
            ObjectType.companies, _single_filter_search("phone", normalized, _COMPANY_PROPERTIES)
        )
        for candidate in response.results:
            if _phone_matches(candidate, normalized, _COMPANY_PHONE_PROPERTIES):
                logger.info(
                    "promotion.hubspot.duplicate_found",
                    object_type=ObjectType.companies.value,
                    matched_on="phone",
                    object_id=candidate.id,
                )
                return candidate

    logger.info("promotion.hubspot.duplicate_absent", object_type=ObjectType.companies.value)
    return None


async def find_existing_contact(
    client: HubSpotClient,
    *,
    email: str | None = None,
    phone: str | None = None,
) -> HubSpotObject | None:
    """Return the contact already in HubSpot for this email or phone, or ``None``.

    Email first, then ``phone`` and ``mobilephone`` as separate searches — filters inside one group
    are ANDed, so asking for both in a single group would find only records carrying the same
    number twice.
    """
    if email:
        response = await client.search(
            ObjectType.contacts, _single_filter_search("email", email, _CONTACT_PROPERTIES)
        )
        if response.results:
            match = response.results[0]
            logger.info(
                "promotion.hubspot.duplicate_found",
                object_type=ObjectType.contacts.value,
                matched_on="email",
                object_id=match.id,
            )
            return match

    normalized = normalize_phone(phone) if phone else ""
    if normalized:
        for phone_property in _CONTACT_PHONE_PROPERTIES:
            response = await client.search(
                ObjectType.contacts,
                _single_filter_search(phone_property, normalized, _CONTACT_PROPERTIES),
            )
            for candidate in response.results:
                if _phone_matches(candidate, normalized, _CONTACT_PHONE_PROPERTIES):
                    logger.info(
                        "promotion.hubspot.duplicate_found",
                        object_type=ObjectType.contacts.value,
                        matched_on=phone_property,
                        object_id=candidate.id,
                    )
                    return candidate

    logger.info("promotion.hubspot.duplicate_absent", object_type=ObjectType.contacts.value)
    return None
