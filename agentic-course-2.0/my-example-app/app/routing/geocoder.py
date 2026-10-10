"""The US Census Geocoder: a census address in, a cited latitude and longitude out.

**Why this and not Google Places (D13).** Places lat/lng may be cached for only 30 days and is
barred as input to point-in-polygon analysis. The Census Geocoder is free, public domain and needs
no key, so the points it returns may be stored and clustered.

**One request per address**, on the JSON one-line endpoint. The batch endpoint saves requests but
returns CSV and loses a whole batch to one bad response; at a weekly batch of ~150 addresses the
per-address form is simpler and retries per candidate.

**Exactly one match is a match.** Zero matches is *unmatched* and two or more is *ambiguous*. Both
leave the candidate out of routing: picking one of several matches would be a guess, and a door
placed from a guess sends a founder to the wrong street.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import ValidationError

from app.core.logging import get_logger
from app.routing.exceptions import GeocoderResponseShapeError, GeocoderUnavailableError
from app.routing.schemas import (
    CensusResponse,
    GeocodeMatchStatus,
    GeocodeOutcome,
    GeoPoint,
)
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import PostalAddress

logger = get_logger(__name__)

BASE_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
"""The one-line address endpoint. Free, public domain, no key."""

BENCHMARK = "Public_AR_Current"
"""The current address-range benchmark. ``locations`` returns coordinates only, no geographies."""

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
_JITTER_FRACTION = 0.25
_TIMEOUT_SECONDS = 10.0

type Sleep = Callable[[float], Awaitable[None]]


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with a little jitter, so retries do not resynchronize."""
    jitter = 1.0 + random.random() * _JITTER_FRACTION
    return _BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)) * jitter


def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


class CensusGeocoder:
    """Geocodes census addresses one at a time. Use as an async context manager, or ``aclose``."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._base_url = base_url
        self._sleep = sleep
        self._client = httpx.AsyncClient(transport=transport, timeout=_TIMEOUT_SECONDS)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def geocode(self, address: PostalAddress) -> GeocodeOutcome:
        """Geocode one address. Raises only when the service fails, never on a miss."""
        response = await self._get(
            {
                "street": address.street,
                "city": address.city,
                "state": address.state,
                "zip": address.postal_code,
                "benchmark": BENCHMARK,
                "format": "json",
            }
        )
        try:
            parsed = CensusResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            logger.warning("routing.geocoder.response_unparseable", error=type(exc).__name__)
            raise GeocoderResponseShapeError(
                "the Census Geocoder returned a body that is not its documented shape"
            ) from exc

        matches = parsed.result.address_matches
        if not matches:
            return GeocodeOutcome(status=GeocodeMatchStatus.unmatched)
        if len(matches) > 1:
            return GeocodeOutcome(status=GeocodeMatchStatus.ambiguous)

        coordinates = matches[0].coordinates
        point = ProvenancedValue(
            # x is longitude and y is latitude.
            value=GeoPoint(latitude=coordinates.y, longitude=coordinates.x),
            source_url=str(response.request.url),
            retrieved_at=datetime.now(UTC),
            retrieval_method=RetrievalMethod.web_lookup,
        )
        return GeocodeOutcome(status=GeocodeMatchStatus.matched, point=point)

    async def _get(self, params: dict[str, str]) -> httpx.Response:
        """GET with retries on transport errors, 429 and 5xx. Any other 4xx fails at once."""
        last_status: int | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(self._base_url, params=params)
            except httpx.TransportError as exc:
                last_status = None
                logger.warning(
                    "routing.geocoder.request_failed",
                    attempt=attempt,
                    error=type(exc).__name__,
                )
            else:
                if response.is_success:
                    return response
                last_status = response.status_code
                logger.warning(
                    "routing.geocoder.request_failed",
                    attempt=attempt,
                    status=response.status_code,
                )
                if not _is_retryable(response.status_code):
                    break
            if attempt < _MAX_ATTEMPTS:
                await self._sleep(_backoff_seconds(attempt))

        raise GeocoderUnavailableError(
            f"the Census Geocoder failed (last status {last_status})",
            status=last_status,
        )
