"""Google Places (New) Text Search: ``verify_business``'s check of a registry record.

**D13, the three rules this module exists to keep.**

1. *A check, never a source.* Places is asked about a business the registry already named, by the
   registry's own name and address. Nothing here enumerates businesses.
2. *Use and discard.* A :class:`PlaceResult` lives in memory for one candidate's check. What is
   kept is the place ID (storable indefinitely) and the census address that was checked — ours,
   not Places content. What the judgment node may see travels in memory, as
   ``PlacesEvidence``, and dies with the run.
3. *Bound only where declared.* Terms of use are accepted per declared source at ``activate``, so
   Places is called only for a manifest that declares it (:func:`declared_places_source`).

**Not a ``LookupSource``.** A lookup's answer is stored in ``source_records``, and storing a Places
answer is exactly what D13 forbids. So this module is never in an adapter table, and Places stays
``unbound`` in ``search_registry``.

**Pro fields only.** Requesting any Enterprise field bills the whole request at the Enterprise rate
("billed at the highest SKU rate for the fields requested"), and those fields are content D13 would
forbid storing anyway. ``tests/test_structure.py`` holds the mask to the Pro allow-list.

**A bad key is HTTP 400**, not 401 or 403 (read live 2026-10-11: ``error.status``
``INVALID_ARGUMENT``, ``details[].reason`` ``API_KEY_INVALID``), so it is told apart by its
reason. A 403 (the API not enabled, or billing off; its shape unverified) is treated the same way.
"""

import asyncio
import re
import time
from collections.abc import Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.manifests.schemas import ManifestBody
from app.sourcing.exceptions import SourceAuthError, SourceRequestError
from app.sourcing.schemas import CandidateFields, has_entity_designator
from app.sourcing.sources import http
from app.sourcing.sources.base import prefix_matcher

logger = get_logger(__name__)

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

PLACES_FIELD_MASK = (
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.businessStatus",
    "places.types",
)
"""Text Search **Pro** fields only (``places.id`` alone is Essentials, IDs Only).

Never requested: ``websiteUri``, ``nationalPhoneNumber``, ``internationalPhoneNumber``,
``reviews``, ``rating``, ``userRatingCount``, ``regularOpeningHours`` and the rest of the
Enterprise tier — each would bill every call at the Enterprise rate, and is content D13 forbids
storing in any case."""

PAGE_SIZE = 5
"""Results asked for. ``pageSize``, not the deprecated ``maxResultCount``; no paging."""

PLACES_SOURCE_PREFIXES = (
    "places.googleapis.com",
    "developers.google.com/maps/documentation/places",
)
"""A declared source binds as Places when its normalised ``base_url`` is the API or its docs page
(the fire seed cites the docs' overview; freight v9 the API URL)."""

CLOSED_PERMANENTLY = "CLOSED_PERMANENTLY"
"""A match with this status is no business to route a door to: no check is stored (A4)."""

_MIN_INTERVAL_SECONDS = 0.1
"""No per-key QPS is published, so stay at a self-imposed ten a second."""

_AUTH_STATUSES = frozenset({400, 401, 403})
_BAD_REQUEST = 400
_API_KEY_INVALID = "API_KEY_INVALID"

_NAME_STOP_WORDS = frozenset({"THE", "AND", "OF"})
"""Words too common to tell two businesses apart. Nothing vertical: "FREIGHT" or "LOGISTICS" here
would be vertical-specific code, and the address match carries the weight anyway."""

_MIN_TOKEN_LENGTH = 3
_LETTERS = re.compile(r"[^\W\d_]+")
_LEADING_NUMBER = re.compile(r"^\s*(\d+)")
_ZIP5 = re.compile(r"^\s*(\d{5})")


class PlaceResult(BaseModel):
    """One Text Search result, **in memory only**: never a ``CandidateFields`` member, never logged.

    Only the place ID may outlive the check (D13). Everything else is read to decide the match and
    to build the run's in-memory evidence, then dropped.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    display_name: str | None = None
    formatted_address: str | None = None
    business_status: str | None = None
    types: tuple[str, ...] = ()


class _DisplayName(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str | None = None


class _Place(BaseModel):
    """The wire shape of one ``places[]`` entry, camelCase as Google sends it."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    display_name: _DisplayName | None = Field(default=None, alias="displayName")
    formatted_address: str | None = Field(default=None, alias="formattedAddress")
    business_status: str | None = Field(default=None, alias="businessStatus")
    types: tuple[str, ...] = ()


class _SearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    places: tuple[_Place, ...] = ()


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = None


class _ErrorBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str | None = None
    details: tuple[_ErrorDetail, ...] = ()


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: _ErrorBody | None = None


def place_url(place_id: str) -> str:
    """Where a ``business_check`` is cited: the place itself. It names the place, carries no key,
    and is a Google Maps URL — which ``business_check`` is the one field allowed to cite."""
    return f"https://places.googleapis.com/v1/places/{place_id}"


def declared_places_source(body: ManifestBody) -> str | None:
    """The name of the first declared source that is Google Places, or ``None`` (A3).

    First declared wins, so a manifest declaring Places twice is still deterministic.
    """
    matchers = tuple(prefix_matcher(prefix) for prefix in PLACES_SOURCE_PREFIXES)
    for cited in body.sources:
        if any(matches(cited.value) for matches in matchers):
            return cited.value.name
    return None


def places_unavailable(settings: Settings) -> str | None:
    """Why Places cannot run in this environment, or ``None``. A blank key counts as unset (D18)."""
    key = settings.google_places_api_key
    if key is None or not key.get_secret_value().strip():
        return "GOOGLE_PLACES_API_KEY is not set"
    return None


class PlacesClient:
    """One Text Search per call, paced, with the key in a header and never in a URL or a log."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        source: str,
        backoff_seconds: float = http.DEFAULT_BACKOFF_SECONDS,
        throttle: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key.strip()
        self._source = source
        self._backoff_seconds = backoff_seconds
        self._throttle = throttle
        self._last_call_at: float | None = None

    async def _pace(self) -> None:
        if not self._throttle:
            return
        if self._last_call_at is not None:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < _MIN_INTERVAL_SECONDS:
                await asyncio.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_at = time.monotonic()

    async def search_text(self, query: str) -> tuple[PlaceResult, ...]:
        """The results for ``query``, in Google's order; ``()`` when there are none.

        Raises :class:`SourceAuthError` when the key is refused (no later call will do better) and
        :class:`SourceRequestError` for any other refusal or an unreadable body.
        """
        await self._pace()
        response = await http.post(
            self._client,
            PLACES_SEARCH_URL,
            source=self._source,
            json={"textQuery": query, "pageSize": PAGE_SIZE},
            headers={
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": ",".join(PLACES_FIELD_MASK),
            },
            allow_status=_AUTH_STATUSES,
            backoff_seconds=self._backoff_seconds,
        )
        body = http.json_body(response, source=self._source)
        if response.status_code in _AUTH_STATUSES:
            raise self._refusal(response.status_code, body)

        try:
            parsed = _SearchResponse.model_validate(body)
        except ValidationError as exc:
            raise SourceRequestError(
                f"{self._source} answered a body that is not a Text Search response",
                source=self._source,
                status=response.status_code,
            ) from exc
        results = tuple(
            PlaceResult(
                id=place.id,
                display_name=place.display_name.text if place.display_name else None,
                formatted_address=place.formatted_address,
                business_status=place.business_status,
                types=place.types,
            )
            for place in parsed.places
        )
        logger.info("sourcing.places.lookup_succeeded", source=self._source, results=len(results))
        return results

    def _refusal(self, status: int, body: object) -> SourceRequestError:
        """A refused key as :class:`SourceAuthError`; any other 4xx as a request error."""
        try:
            envelope = _ErrorEnvelope.model_validate(body)
        except ValidationError:
            envelope = _ErrorEnvelope()
        error = envelope.error
        reasons = {detail.reason for detail in error.details} if error else set[str | None]()
        if status != _BAD_REQUEST or _API_KEY_INVALID in reasons:
            return SourceAuthError(
                f"{self._source} refused the API key ({status}) — check GOOGLE_PLACES_API_KEY, "
                "that Places API (New) is enabled, and that billing is on",
                source=self._source,
                status=status,
            )
        return SourceRequestError(
            f"{self._source} refused the request ({status}"
            + (f", {error.status}" if error and error.status else "")
            + ")",
            source=self._source,
            status=status,
        )


def search_query(fields: CandidateFields) -> str | None:
    """``"{dba or legal name}, {street}, {city}, {state} {zip}"``, or ``None`` when uncheckable.

    The DBA first, because that is what a storefront usually shows. Built from our own cited
    registry data only.
    """
    name = fields.dba_name or fields.legal_name
    if name is None or fields.address is None:
        return None
    address = fields.address.value
    return f"{name.value}, {address.street}, {address.city}, {address.state} {address.postal_code}"


def _name_tokens(name: str) -> frozenset[str]:
    """Distinguishing words: upper-cased letters only, no designators, no stop words, 3+ long."""
    return frozenset(
        token
        for token in _LETTERS.findall(name.upper())
        if len(token) >= _MIN_TOKEN_LENGTH
        and token not in _NAME_STOP_WORDS
        and not has_entity_designator(token)
    )


def _contains_number(text: str, number: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text) is not None


def match_place(fields: CandidateFields, results: Sequence[PlaceResult]) -> PlaceResult | None:
    """The first result that is this business, in Google's order, or ``None`` (A4). No model.

    A result matches when its address holds the census street number (when the census street
    starts with one) **and** the 5-digit ZIP, **and** its name shares a distinguishing word with the
    legal or DBA name. A PO-box-only street has no number to hold, so it is matched on ZIP and name;
    a shared office building is told apart by the name.
    """
    if fields.address is None:
        return None
    address = fields.address.value
    zip_match = _ZIP5.match(address.postal_code)
    if zip_match is None:
        return None
    zip5 = zip_match.group(1)
    number_match = _LEADING_NUMBER.match(address.street)
    number = number_match.group(1) if number_match else None

    names = [cited.value for cited in (fields.legal_name, fields.dba_name) if cited is not None]
    ours = frozenset[str]().union(*(_name_tokens(name) for name in names))
    if not ours:
        return None

    for result in results:
        formatted = result.formatted_address or ""
        if not _contains_number(formatted, zip5):
            continue
        if number is not None and not _contains_number(formatted, number):
            continue
        if ours & _name_tokens(result.display_name or ""):
            return result
    return None
