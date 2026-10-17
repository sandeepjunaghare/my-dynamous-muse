"""Idempotent provisioning of the seven custom properties that make a record reviewable.

Review happens in HubSpot — there is no frontend and no second login — so a record that cannot
show where it came from cannot be reviewed. None of T3's five existed in portal 244766495 when
this was written, confirmed live on 2026-09-27. T9 adds the sixth, ``lpe_review_status`` (OQ2-A):
no "pending review" stage existed anywhere in the portal, and ``hs_lead_status`` is the founders'.
The seventh, ``lpe_verification`` (2026-10-16), says which check found the business, now that an
unverified one can reach review.

Provisioning is **read-then-create**, never a PATCH: a human may have edited a label, and
overwriting that silently would be the portal editing us back. A property whose ``type`` no longer
matches ours is logged as a warning and left alone; changing a property's type is a human's call.

Property internal names are **permanent** in HubSpot — there is no rename. The ``lpe_`` prefix
marks our fields as unmistakably ours in a portal holding 3,118 contacts of mixed provenance, and
keeps a future HubSpot-native ``source_url`` from colliding with this one.
"""

from app.core.logging import get_logger
from app.promotion.client import HubSpotClient
from app.promotion.schemas import (
    ObjectType,
    PropertyDefinition,
    PropertyGroupDefinition,
    PropertyOption,
    ReviewStatus,
    Verification,
)

logger = get_logger(__name__)

LPE_GROUP = PropertyGroupDefinition(name="lpe", label="Local Prospect Engine")

_GROUP_OBJECT_TYPES = (ObjectType.companies, ObjectType.contacts)


def _definition(
    name: str, label: str, property_type: str, field_type: str, description: str
) -> PropertyDefinition:
    return PropertyDefinition(
        name=name,
        label=label,
        type=property_type,
        field_type=field_type,
        group_name=LPE_GROUP.name,
        description=description,
    )


SOURCE_URL = _definition(
    "lpe_source_url",
    "Source URL",
    "string",
    "text",
    "Where this record's data was retrieved from. Part of the provenance a reviewer trusts.",
)

# The fieldType is `date`, not `datetime` — there is no `datetime` fieldType in HubSpot.
SOURCED_AT = _definition(
    "lpe_sourced_at",
    "Sourced at",
    "datetime",
    "date",
    "When this record's data was retrieved.",
)

# Deliberately a string, not an enumeration. An enum reads better in the HubSpot UI, but it would
# make vertical #3 a property migration — exactly the coupling "vertical is data, not code" exists
# to prevent, and a direct hit on M9.
VERTICAL = _definition(
    "lpe_vertical",
    "Vertical",
    "string",
    "text",
    "The vertical manifest this record was sourced under.",
)

PRIORITY_SCORE = _definition(
    "lpe_priority_score",
    "Priority score",
    "number",
    "number",
    "Intensity x Automatable qualification score.",
)

ROUTE_CLUSTER = _definition(
    "lpe_route_cluster",
    "Route cluster",
    "string",
    "text",
    "The DFW geographic cluster this business was routed into.",
)

_REVIEW_STATUS_LABELS = {
    ReviewStatus.pending_review: "Pending review",
    ReviewStatus.accepted: "Accepted",
    ReviewStatus.rejected: "Rejected",
}

# An enumeration, unlike the vertical: its three values are a closed workflow, not data, and a
# select is what lets the founder filter and bulk-edit the company list view (D14). The option
# values are ReviewStatus's exactly, because `lpe promotion sync` reads them back. Options a human
# adds later are not drift: drift compares `type` only, and nothing here ever PATCHes options.
REVIEW_STATUS = PropertyDefinition(
    name="lpe_review_status",
    label="Review status",
    type="enumeration",
    field_type="select",
    group_name=LPE_GROUP.name,
    description=(
        "Set by the founder at review. Accepted prospects enter the cadence on the next "
        "`lpe promotion sync`; rejected ones are left alone."
    ),
    options=[
        PropertyOption(label=_REVIEW_STATUS_LABELS[status], value=status.value, display_order=index)
        for index, status in enumerate(ReviewStatus)
    ],
)

_VERIFICATION_LABELS = {
    Verification.places: "Google Places",
    Verification.website: "Own-domain website",
    Verification.none: "Not verified",
}

# An enumeration for the same reason as the review status: a closed set of our own checks, which
# the founder filters on at review.
VERIFICATION = PropertyDefinition(
    name="lpe_verification",
    label="Verification",
    type="enumeration",
    field_type="select",
    group_name=LPE_GROUP.name,
    description=(
        'Which engine check found this business. "Not verified" means nothing contradicted the '
        "address, not that anything confirmed it: check it before accepting."
    ),
    options=[
        PropertyOption(label=_VERIFICATION_LABELS[kind], value=kind.value, display_order=index)
        for index, kind in enumerate(Verification)
    ],
)

# Scoring, routing, verification and review describe a business, so they land on COMPANY only.
# Provenance describes a record, and E18 is a *contact*-quality finding, so the three provenance
# fields go on both.
PROVISIONING_PLAN: tuple[tuple[ObjectType, PropertyDefinition], ...] = (
    (ObjectType.companies, SOURCE_URL),
    (ObjectType.companies, SOURCED_AT),
    (ObjectType.companies, VERTICAL),
    (ObjectType.companies, PRIORITY_SCORE),
    (ObjectType.companies, ROUTE_CLUSTER),
    (ObjectType.companies, REVIEW_STATUS),
    (ObjectType.companies, VERIFICATION),
    (ObjectType.contacts, SOURCE_URL),
    (ObjectType.contacts, SOURCED_AT),
    (ObjectType.contacts, VERTICAL),
)


async def ensure_property_group(client: HubSpotClient) -> None:
    """Create the ``lpe`` group on each object type that needs it.

    Runs before any property: ``groupName`` is required on a property create and HubSpot rejects
    one naming a group that does not exist.
    """
    for object_type in _GROUP_OBJECT_TYPES:
        if await client.property_group_exists(object_type, LPE_GROUP.name):
            logger.info(
                "promotion.hubspot.group_present",
                object_type=object_type.value,
                name=LPE_GROUP.name,
            )
            continue

        created = await client.create_property_group(object_type, LPE_GROUP)
        if created:
            logger.info(
                "promotion.hubspot.group_created",
                object_type=object_type.value,
                name=LPE_GROUP.name,
            )
        else:
            logger.info(
                "promotion.hubspot.group_present",
                object_type=object_type.value,
                name=LPE_GROUP.name,
            )


async def ensure_properties(client: HubSpotClient) -> None:
    """Make the seven properties exist on the right object types, creating only what is missing.

    Safe to run on every deploy: a second pass issues zero writes and logs
    ``promotion.hubspot.property_present`` for each of the ten placements.

    Event names carry exactly one underscore in their last segment — the
    ``domain.component.action_state`` convention, enforced by ``tests/core/test_logging.py``.
    """
    await ensure_property_group(client)

    for object_type, definition in PROVISIONING_PLAN:
        existing = await client.get_property(object_type, definition.name)

        if existing is not None:
            if existing.type is not None and existing.type != definition.type:
                logger.warning(
                    "promotion.hubspot.property_drifted",
                    object_type=object_type.value,
                    name=definition.name,
                    expected=definition.type,
                    found=existing.type,
                )
            logger.info(
                "promotion.hubspot.property_present",
                object_type=object_type.value,
                name=definition.name,
            )
            continue

        created = await client.create_property(object_type, definition)
        if created:
            logger.info(
                "promotion.hubspot.property_created",
                object_type=object_type.value,
                name=definition.name,
            )
        else:
            # A 409: something created it between our read and our write.
            logger.info(
                "promotion.hubspot.property_present",
                object_type=object_type.value,
                name=definition.name,
            )
