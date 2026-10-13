"""The HubSpot gateway — the only place this service talks to the portal — and promotion.

T3 built the gateway; T9 built promotion on it (``service.py``: a finished run's qualified
candidates → Company + Contact at ``pending_review``, and the founder's decision → the cadence).
The cadence slice (T11) is the gateway's other caller. ``PromotionService`` is deliberately **not**
exported here: ``app.cadence.service`` imports this package, and promotion imports the cadence, so
an export would be a circular import. Import it as ``app.promotion.service``.

The public surface:

* :class:`~app.promotion.client.HubSpotClient` and its lifecycle trio
* :func:`~app.promotion.client.to_property_payload` — the write-gate
* :func:`~app.promotion.properties.ensure_properties` — idempotent provisioning
* :func:`~app.promotion.dedupe.find_existing_company` / ``find_existing_contact``
"""

from app.promotion.client import (
    API_VERSION,
    HubSpotClient,
    aclose_hubspot_client,
    get_hubspot_client,
    is_initialised,
    to_property_payload,
)
from app.promotion.dedupe import find_existing_company, find_existing_contact, normalize_phone
from app.promotion.exceptions import (
    HubSpotAuthError,
    HubSpotError,
    HubSpotRateLimitError,
    HubSpotResponseError,
    HubSpotTransportError,
    UnprovenancedWriteError,
)
from app.promotion.properties import ensure_properties
from app.promotion.schemas import TaskCreate, TaskPriority, TaskStatus, TaskType

__all__ = [
    "API_VERSION",
    "HubSpotAuthError",
    "HubSpotClient",
    "HubSpotError",
    "HubSpotRateLimitError",
    "HubSpotResponseError",
    "HubSpotTransportError",
    "TaskCreate",
    "TaskPriority",
    "TaskStatus",
    "TaskType",
    "UnprovenancedWriteError",
    "aclose_hubspot_client",
    "ensure_properties",
    "find_existing_company",
    "find_existing_contact",
    "get_hubspot_client",
    "is_initialised",
    "normalize_phone",
    "to_property_payload",
]
