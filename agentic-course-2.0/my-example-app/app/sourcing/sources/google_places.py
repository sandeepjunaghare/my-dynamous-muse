"""Google Places (New) Text Search: ``verify_business``'s check of a registry record.

**D13, the three rules this module exists to keep.**

1. *A check, never a source.* Places is asked about a business the registry already named, by the
   registry's own name and address. Nothing here enumerates businesses.
2. *Use and discard.* A :class:`PlaceResult` lives in memory for one candidate's check. What is
   kept is the place ID (storable indefinitely) and the census address that was checked — ours,
   not Places content — plus, on a miss, our own kind for what sat at the address
   (:func:`classify_miss`, never a Places name or type). What the judgment node may see travels
   in memory, as ``PlacesEvidence``, and dies with the run.
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
from typing import Final, NamedTuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.manifests.schemas import ManifestBody
from app.sourcing.exceptions import SourceAuthError, SourceRequestError
from app.sourcing.schemas import CandidateFields, MissKind, name_words, shares_name
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

MAIL_DROP_TYPES: Final = frozenset({"post_office", "business_center", "shipping_service"})
"""Place types that make a census address a mail drop (website-verification OQ-2). All three are
Table A types (checked 2026-10-15). ``courier_service`` and ``coworking_space`` are left out on
purpose: a courier's depot and a coworking space are both real doors. Generic across verticals, so
code beside :data:`CLOSED_PERMANENTLY`, not manifest data."""

ADDRESS_TYPES: Final = frozenset({"street_address", "premise", "subpremise"})
"""Table B types Places returns on a result that is only an address (website-verification OQ-2):
a result whose types are all of these lists nothing at the address. ``subpremise`` included: a
suite with nothing listed is still nothing listed."""

_MIN_INTERVAL_SECONDS = 0.1
"""No per-key QPS is published, so stay at a self-imposed ten a second."""

_AUTH_STATUSES = frozenset({400, 401, 403})
_BAD_REQUEST = 400
_API_KEY_INVALID = "API_KEY_INVALID"

_LEADING_NUMBER = re.compile(r"^\s*(\d+)")
_ZIP5 = re.compile(r"^\s*(\d{5})")
_DOOR = re.compile(r"(?:\b(?:STE|SUITE|UNIT|APT|ROOM|PMB)\b\.?|#)\s*#?\s*([A-Z0-9][A-Z0-9-]*)")
"""A door designator and its number, in an upper-cased street line. Only the number is kept, so
``#120`` and ``Ste 120`` are the same door (PR #36 M1). No ``RM``: in Texas that is a Ranch to
Market road (``1234 RM 620 N``), not a room."""

_LOCATOR = re.compile(r"\b(BLDG|BUILDING|FL|FLOOR)\b\.?\s*#?\s*([A-Z0-9][A-Z0-9-]*)")
"""A building or floor and its number. Not a door: ``Bldg 3 Ste 120`` and ``Bldg 3 Ste 300`` are
neighbours (PR #36 round-2 R1), so a locator keeps its kind and never matches a door number."""

_LOCATOR_KIND = {"BUILDING": "BLDG", "FLOOR": "FL"}


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


class _Units(NamedTuple):
    """The doors and locators a street line names: ``Bldg 3 Ste 100 PMB 555`` is doors
    ``{100, 555}`` and locators ``{BLDG 3}``."""

    doors: frozenset[str] = frozenset()
    locators: frozenset[str] = frozenset()


class _Checkable(NamedTuple):
    """What :func:`match_place` compares, from our own registry data."""

    zip5: str
    number: str
    names: tuple[frozenset[str], ...]
    units: _Units = _Units()


def _checkable(fields: CandidateFields) -> _Checkable | None:
    """The census ZIP, street number and name words, or ``None`` if a match is impossible.

    One test for both :func:`search_query` and :func:`match_place`, so a call is never paid for a
    candidate the matcher could not match (PR #32 M1): a street with no leading number (a PO box is
    no door to knock on), no 5-digit ZIP, or no name with a distinguishing word.
    """
    if fields.address is None:
        return None
    address = fields.address.value
    zip_match = _ZIP5.match(address.postal_code)
    number_match = _LEADING_NUMBER.match(address.street)
    if zip_match is None or number_match is None:
        return None
    cited = (fields.legal_name, fields.dba_name)
    names = tuple(tokens for tokens in (name_words(name.value) for name in cited if name) if tokens)
    if not names:
        return None
    return _Checkable(
        zip5=zip_match.group(1),
        number=number_match.group(1),
        names=names,
        units=_units(address.street),
    )


def _units(street: str) -> _Units:
    """The doors and locators a street line names. Locators are read and removed first, so the
    ``#`` in ``Bldg #3`` is not taken for a door."""
    upper = street.upper()
    locators = frozenset(
        f"{_LOCATOR_KIND.get(kind, kind)} {number}" for kind, number in _LOCATOR.findall(upper)
    )
    return _Units(doors=frozenset(_DOOR.findall(_LOCATOR.sub(" ", upper))), locators=locators)


def search_query(fields: CandidateFields) -> str | None:
    """``"{dba or legal name}, {street}, {city}, {state} {zip}"``, or ``None`` when uncheckable.

    The DBA first, because that is what a storefront usually shows, unless it has no
    distinguishing word (``KC``): then the legal name, because a query by a name
    :func:`match_place` cannot compare is a paid likely miss (PR #32 round-2 L3). Built from our
    own cited registry data only. Uncheckable means :func:`_checkable` says no match is possible.
    """
    if fields.address is None or _checkable(fields) is None:
        return None
    # `_checkable` passed, so at least one of the two names has a distinguishing word.
    name = next(
        name for name in (fields.dba_name, fields.legal_name) if name and name_words(name.value)
    )
    address = fields.address.value
    return f"{name.value}, {address.street}, {address.city}, {address.state} {address.postal_code}"


def _contains_number(text: str, number: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text) is not None


def _starts_with_number(text: str, number: str) -> bool:
    """The address's own street number, not a suite or a ZIP that happens to contain it."""
    return re.match(rf"\s*{re.escape(number)}(?!\d)", text) is not None


def _at_address(result: PlaceResult, checkable: _Checkable) -> bool:
    """Whether ``result``'s address starts with the census street number and holds its ZIP.

    No ``formatted_address`` is never at the address.
    """
    formatted = result.formatted_address or ""
    return _contains_number(formatted, checkable.zip5) and _starts_with_number(
        formatted, checkable.number
    )


def _our_unit(result: PlaceResult, checkable: _Checkable) -> bool:
    """Whether a result at the address is at *our* door, not a neighbour's suite (PR #36 M1).

    A result naming no door is the whole building (a standalone post office). One naming a door is
    ours only if the census street names that door too, so a UPS Store at Ste 120 says nothing about
    Ste 300, or about a census address with no suite. Every building or floor the result names must
    be ours as well (PR #36 round-2 R1): a store in Bldg 5 says nothing about Bldg 3, and one in
    Bldg 3 says nothing about a census address that names no building. Only the street line,
    before the first comma, is read.
    """
    theirs = _units((result.formatted_address or "").split(",", 1)[0])
    ours = checkable.units
    if not theirs.locators <= ours.locators:
        return False
    return not theirs.doors or bool(theirs.doors & ours.doors)


def match_place(fields: CandidateFields, results: Sequence[PlaceResult]) -> PlaceResult | None:
    """The first result that is this business, in Google's order, or ``None`` (A4). No model.

    A result matches when its address **starts with** the census street number and holds the
    5-digit ZIP, **and** its name carries the legal or the DBA name's distinguishing words (see
    ``MIN_SHARED_WORDS`` in ``app.sourcing.schemas``). A shared office building is told apart by
    the name, within the limit that constant records for a name of one distinguishing word. A live
    match is preferred to a permanently closed one listed ahead of it (PR #32 M2); a closed match
    comes back only when it is the only one.
    """
    checkable = _checkable(fields)
    if checkable is None:
        return None
    closed: PlaceResult | None = None
    for result in results:
        if not _at_address(result, checkable):
            continue
        theirs = name_words(result.display_name or "")
        if not any(shares_name(ours, theirs) for ours in checkable.names):
            continue
        if result.business_status != CLOSED_PERMANENTLY:
            return result
        closed = closed or result
    return closed


class MissVerdict(NamedTuple):
    """What :func:`classify_miss` read from a miss, **in memory**: our kind and the place ID."""

    kind: MissKind
    place_id: str


def classify_miss(fields: CandidateFields, results: Sequence[PlaceResult]) -> MissVerdict | None:
    """Our verdict on a miss at the census address, or ``None``. Pure, no model.

    Call it **only after** :func:`match_place` returned ``None``: it does not look for this
    business, it says what *else* is at the address (website-verification OQ-2). Only results at
    the address, by :func:`match_place`'s own test, count, in Google's order, with precedence
    ``mail_drop``, then ``no_listing``, then ``other_business``:

    - a result whose types include a :data:`MAIL_DROP_TYPES` type is a mail drop, whatever its
      status (a closed post office is still a post office), if it is at our unit: one naming a
      suite the census street does not name is a neighbour's (PR #36 M1, :func:`_our_unit`);
    - a result whose types are all :data:`ADDRESS_TYPES` is nothing listed;
    - a business sharing **no** distinguishing word with the legal or DBA name is another business.
      A one-word overlap stays a plain miss, which is what keeps a storefront that shortened our
      name (Spearpoint) from being called someone else.

    ``None`` for an uncheckable candidate, no results, or nothing at the address: an empty answer is
    no evidence of anything, and there is no place ID to cite.
    """
    checkable = _checkable(fields)
    if checkable is None:
        return None
    here = [result for result in results if _at_address(result, checkable)]
    for result in here:
        if MAIL_DROP_TYPES.intersection(result.types) and _our_unit(result, checkable):
            return MissVerdict(MissKind.mail_drop, result.id)
    for result in here:
        if result.types and ADDRESS_TYPES.issuperset(result.types):
            return MissVerdict(MissKind.no_listing, result.id)
    for result in here:
        if result.types and ADDRESS_TYPES.issuperset(result.types):
            continue
        theirs = name_words(result.display_name or "")
        if theirs and all(not (ours & theirs) for ours in checkable.names):
            return MissVerdict(MissKind.other_business, result.id)
    return None
