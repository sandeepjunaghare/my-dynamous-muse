"""The one place this service talks to HubSpot.

Everything that reaches the portal goes through :class:`HubSpotClient`, for one reason: the
write-gate has to be a boundary rather than a convention. :func:`to_property_payload` is the
enforcement point, and it is reached by *signature* — a prospect field write takes
``ProvenancedValue``s, so a caller holding a raw ``dict[str, str]`` does not type-check. Routing
around the gate is not something a later slice can do by accident, only by editing this module.

**Paths are date-versioned.** HubSpot moved from ``v1``-``v4`` to dated versions on 2026-03-30;
the legacy URLs still work but are explicitly the frozen surface, so starting there would mean
owing a migration on day one. :data:`API_VERSION` is the single place it lives — a dated version is
*Current* for 6 months and unsupported at 18, so expect to bump this constant about twice a year.

**The throttle matters more than the retry.** HubSpot's own guidance is to stay under the limit
rather than react to rejection, and on search there is no choice: search responses carry no
rate-limit headers at all and the ceiling is 5 requests/second. A run that dedupes 120 candidates
is 120-plus searches — roughly 25 seconds of deliberate pacing. That is budgeted, not discovered.
"""

import asyncio
import random
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger
from app.promotion.exceptions import (
    HubSpotAuthError,
    HubSpotRateLimitError,
    HubSpotResponseError,
    HubSpotResponseShapeError,
    HubSpotTransportError,
    UnprovenancedWriteError,
)
from app.promotion.schemas import (
    Association,
    AssociationsPage,
    AssociationTarget,
    AssociationType,
    BatchReadInput,
    BatchReadRequest,
    BatchReadResponse,
    ExistingProperty,
    HubSpotErrorBody,
    HubSpotObject,
    ObjectType,
    ObjectWriteRequest,
    PropertyDefinition,
    PropertyGroupDefinition,
    SearchRequest,
    SearchResponse,
    TaskCreate,
    TaskCreateRequest,
)
from app.shared.provenance import ProvenancedValue, is_promotable

logger = get_logger(__name__)

BASE_URL = "https://api.hubapi.com"

# Bumping this is a one-line change, which is the entire reason it is a constant. Dated versions
# are Current for 6 months, Supported for 12 more, and unsupported at 18.
API_VERSION = "2026-09"

# Default HUBSPOT_DEFINED association type ids.
_TASK_TO_CONTACT = 204
_TASK_TO_COMPANY = 192

# Search is capped at 5 requests/second per account and its responses carry no headers to react to.
_SEARCH_MIN_INTERVAL_SECONDS = 0.2

# On everything else, throttle from the headers once the remaining budget gets thin.
_RATE_LIMIT_LOW_WATER = 5
_DEFAULT_INTERVAL_MS = 10_000

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 0.5
_JITTER_FRACTION = 0.1
_LOCKED_RETRY_SECONDS = 2.0
# A locked record gets one retry, not the full `_MAX_ATTEMPTS` budget: the lock is held for
# the duration of someone else's import or merge, so a third and fourth 2 s wait buy nothing
# the caller's next run would not.
_LOCKED_MAX_ATTEMPTS = 2

_DAILY_POLICY = "DAILY"

# HubSpot's cap on inputs per batch call.
_BATCH_READ_LIMIT = 100
# The associations endpoint's maximum page size, and a circuit breaker on the walk.
_ASSOCIATION_PAGE_SIZE = 500
_ASSOCIATION_MAX_PAGES = 20

# Spelled out rather than taken from the enum httpx ships: its members carry a
# (value, phrase) pair through a custom `__new__`, which Pyright strict reads as a tuple and
# then reports every comparison against it as permanently false.
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_CONFLICT = 409
_LOCKED_STATUS = 423
_TOO_MANY_REQUESTS = 429
_MIGRATION_STATUS = 477

type ProvenancedProperty = (
    ProvenancedValue[str]
    | ProvenancedValue[int]
    | ProvenancedValue[float]
    | ProvenancedValue[datetime]
)


def _is_cited(value: ProvenancedProperty | None) -> bool:
    """:func:`is_promotable`, applied to this slice's union of provenanced property types.

    The shared predicate is generic in ``T``, and pydantic generics are invariant, so a union of
    four parametrizations offers no single ``T`` to bind — the call does not type-check as written.
    The cast states what is already true: every member of the union is a ``ProvenancedValue``.

    Doing it this way rather than writing ``value is not None`` inline keeps the gate defined in
    exactly one place. An inlined copy is the version that quietly stops matching the real
    predicate the first time it grows a second condition. Same move, and the same reason, as the
    cast in ``app/shared/provenance.py``.
    """
    return is_promotable(cast(ProvenancedValue[object] | None, value))


def render_property_value(value: str | int | float | datetime) -> str:
    """Render a provenanced value as the string HubSpot stores.

    ``bool`` is checked before ``int`` because it *is* an ``int`` at runtime, and ``str(True)``
    would put the literal ``"True"`` into the portal.
    """
    if isinstance(value, datetime):
        return str(int(value.timestamp() * 1000))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def to_property_payload(properties: Mapping[str, ProvenancedProperty | None]) -> dict[str, str]:
    """**The write-gate.** Render prospect fields for HubSpot, refusing any that cannot be cited.

    Every prospect field carries the URL it came from, when it was retrieved and how; a field
    nobody can cite is exactly the field that should never have been written (E18: 14 of 34
    fire-vertical contacts carry a company name in the first-name field). The signature does most
    of the work — the only way to offer an uncitable field is to pass ``None``, which is a sourcing
    stage explicitly saying *I could not cite this*.

    **Every offender is collected and reported in one exception**, rather than raising on the
    first: a caller fixing fields one round-trip at a time is a caller who stops reading the error.
    Nothing is serialized until the whole mapping has passed, so no partial payload can exist.

    This governs prospect field writes only. Task creation is ungated — see
    :meth:`HubSpotClient.create_task`.
    """
    unprovenanced = tuple(
        sorted(name for name, value in properties.items() if not _is_cited(value))
    )
    if unprovenanced:
        logger.warning("promotion.hubspot.write_refused", fields=list(unprovenanced))
        raise UnprovenancedWriteError(
            f"refusing to write {len(unprovenanced)} field(s) without provenance: "
            f"{', '.join(unprovenanced)}",
            fields=unprovenanced,
        )
    # `is not None` is the same check `_is_cited` just made, repeated for the type checker's
    # benefit — the guard above already proved the mapping holds no None.
    return {
        name: render_property_value(value.value)
        for name, value in properties.items()
        if value is not None
    }


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with a little jitter, so retries do not resynchronize."""
    jitter = 1.0 + random.random() * _JITTER_FRACTION
    return _BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)) * jitter


def _parse_error_body(response: httpx.Response) -> HubSpotErrorBody | None:
    """Parse HubSpot's error envelope, tolerating anything that is not one.

    A proxy in front of HubSpot answers with an HTML page, not JSON. Letting that raise would turn
    a handled upstream outage into an unhandled ``ValidationError`` three frames away from the
    cause.
    """
    try:
        return HubSpotErrorBody.model_validate(response.json())
    except (ValueError, TypeError):
        logger.info("promotion.hubspot.body_unparsed", status_code=response.status_code)
        return None


def _missing_scope(error: HubSpotErrorBody | None) -> str | None:
    """Pull the scope name out of a 403 message. "Which scope" is the whole content of the fix."""
    if error is None or error.message is None:
        return None
    match = re.search(r"crm\.[a-z0-9_.]+", error.message)
    return match.group(0) if match else None


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


class HubSpotClient:
    """An async HubSpot client that self-throttles, retries carefully, and cannot be routed around.

    Construct it with a ``transport`` to test against fixtures; the singleton accessors below build
    the real one from settings.
    """

    def __init__(self, *, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(10.0, read=30.0),
            transport=transport,
        )
        self._search_lock = asyncio.Lock()
        self._last_search_at: float | None = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        retry_on_server_error: bool,
        allow_status: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """Every call to HubSpot funnels through here.

        ``retry_on_server_error`` is not a style choice. Retrying a 429 is always safe — the
        request never executed — so that is handled unconditionally below. Retrying a **5xx on a
        create is not**: HubSpot may have created the object and failed to answer, and the retry
        makes a second one. Reads and updates pass ``True``; creates pass ``False``.

        ``allow_status`` lets a caller interpret a specific non-2xx itself — a 404 from a property
        read means "create it", not "fail".
        """
        attempt = 0
        while True:
            attempt += 1
            started = time.perf_counter()
            logger.info(
                "promotion.hubspot.request_started", method=method, path=path, attempt=attempt
            )

            try:
                response = await self._client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                logger.error(
                    "promotion.hubspot.request_failed",
                    method=method,
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                    duration_seconds=round(time.perf_counter() - started, 3),
                    exc_info=True,
                )
                # A timeout on a create is not permission to retry — same ambiguity as the 5xx.
                if retry_on_server_error and attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise HubSpotTransportError(
                    f"HubSpot request failed before a response: {method} {path}"
                ) from exc

            duration = round(time.perf_counter() - started, 3)
            status = response.status_code

            if status < 300:
                logger.info(
                    "promotion.hubspot.request_succeeded",
                    method=method,
                    path=path,
                    status_code=status,
                    duration_seconds=duration,
                )
                await self._respect_rate_limit_headers(response)
                return response

            if status in allow_status:
                logger.info(
                    "promotion.hubspot.request_handled",
                    method=method,
                    path=path,
                    status_code=status,
                    duration_seconds=duration,
                )
                await self._respect_rate_limit_headers(response)
                return response

            error = _parse_error_body(response)
            correlation_id = error.correlation_id if error is not None else None
            category = error.category if error is not None else None

            if status == _TOO_MANY_REQUESTS:
                policy = error.policy_name if error is not None else None
                logger.warning(
                    "promotion.hubspot.rate_limited", path=path, attempt=attempt, policy=policy
                )
                if policy == _DAILY_POLICY:
                    raise HubSpotRateLimitError(
                        "HubSpot daily quota exhausted — 250,000 calls in a day is a bug, not load",
                        attempts=attempt,
                        policy=policy,
                    )
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise HubSpotRateLimitError(
                    f"HubSpot still rate limiting after {attempt} attempts",
                    attempts=attempt,
                    policy=policy,
                )

            if status in (_UNAUTHORIZED, _FORBIDDEN):
                logger.error(
                    "promotion.hubspot.request_failed",
                    method=method,
                    path=path,
                    status_code=status,
                    correlation_id=correlation_id,
                )
                raise HubSpotAuthError(
                    f"HubSpot rejected the token for {method} {path} ({status}) — "
                    "check the private app's scopes",
                    missing_scope=_missing_scope(error),
                )

            if status == _MIGRATION_STATUS:
                raise HubSpotResponseError(
                    "HubSpot reports a data-centre migration (477); it can last hours, so this is "
                    "raised rather than waited out in-process",
                    status_code_received=status,
                    category=category,
                    correlation_id=correlation_id,
                )

            # A 423 carries the same ambiguity as a 5xx: HubSpot locks a record during an import
            # or a merge and never says whether our write landed before the lock. So a create is
            # not retried here either — a duplicate company cannot be caught within the same run,
            # because the search index lags the write (see `dedupe.py`).
            if (
                status == _LOCKED_STATUS
                and retry_on_server_error
                and attempt < _LOCKED_MAX_ATTEMPTS
            ):
                logger.warning("promotion.hubspot.request_locked", path=path, attempt=attempt)
                await asyncio.sleep(_LOCKED_RETRY_SECONDS)
                continue

            if status >= 500 and retry_on_server_error and attempt < _MAX_ATTEMPTS:
                logger.warning(
                    "promotion.hubspot.request_retrying",
                    path=path,
                    status_code=status,
                    attempt=attempt,
                )
                await asyncio.sleep(_backoff_seconds(attempt))
                continue

            logger.error(
                "promotion.hubspot.request_failed",
                method=method,
                path=path,
                status_code=status,
                category=category,
                correlation_id=correlation_id,
                duration_seconds=duration,
            )
            raise HubSpotResponseError(
                f"HubSpot returned {status} for {method} {path}",
                status_code_received=status,
                category=category,
                correlation_id=correlation_id,
            )

    async def _respect_rate_limit_headers(self, response: httpx.Response) -> None:
        """Sleep out the window when the remaining budget gets thin.

        Search responses carry none of these headers, so this is a no-op there — which is why the
        search path paces itself instead.
        """
        remaining = _int_or_none(response.headers.get("X-HubSpot-RateLimit-Remaining"))
        if remaining is None or remaining > _RATE_LIMIT_LOW_WATER:
            return
        interval_ms = (
            _int_or_none(response.headers.get("X-HubSpot-RateLimit-Interval-Milliseconds"))
            or _DEFAULT_INTERVAL_MS
        )
        logger.warning(
            "promotion.hubspot.throttle_engaged", remaining=remaining, interval_ms=interval_ms
        )
        await asyncio.sleep(interval_ms / 1000)

    async def _space_search_calls(self) -> None:
        """Hold each search at least 200 ms after the last one — the 5/second ceiling."""
        if self._last_search_at is not None:
            elapsed = time.monotonic() - self._last_search_at
            if elapsed < _SEARCH_MIN_INTERVAL_SECONDS:
                await asyncio.sleep(_SEARCH_MIN_INTERVAL_SECONDS - elapsed)
        self._last_search_at = time.monotonic()

    async def search(self, object_type: ObjectType, request: SearchRequest) -> SearchResponse:
        """Run a CRM search, paced to stay inside the 5 requests/second search limit."""
        async with self._search_lock:
            await self._space_search_calls()
            response = await self._request(
                "POST",
                f"/crm/objects/{API_VERSION}/{object_type.value}/search",
                json_body=request.model_dump(by_alias=True, exclude_none=True),
                retry_on_server_error=True,
            )
        return SearchResponse.model_validate(response.json())

    async def get_property(self, object_type: ObjectType, name: str) -> ExistingProperty | None:
        """Read one property definition. ``None`` means it does not exist yet."""
        response = await self._request(
            "GET",
            f"/crm/properties/{API_VERSION}/{object_type.value}/{name}",
            retry_on_server_error=True,
            allow_status=frozenset({_NOT_FOUND}),
        )
        if response.status_code == _NOT_FOUND:
            return None
        return ExistingProperty.model_validate(response.json())

    async def create_property(
        self, object_type: ObjectType, definition: PropertyDefinition
    ) -> bool:
        """Create a property. ``False`` means HubSpot said it already exists.

        The 409 is treated as success rather than branched on by message — the conflict body is
        community-reported, not documented, so matching its wording would be building on sand.
        """
        response = await self._request(
            "POST",
            f"/crm/properties/{API_VERSION}/{object_type.value}",
            json_body=definition.model_dump(by_alias=True, exclude_none=True),
            retry_on_server_error=False,
            allow_status=frozenset({_CONFLICT}),
        )
        return response.status_code != _CONFLICT

    async def property_group_exists(self, object_type: ObjectType, name: str) -> bool:
        """Whether a property group is already present on this object type."""
        response = await self._request(
            "GET",
            f"/crm/properties/{API_VERSION}/{object_type.value}/groups/{name}",
            retry_on_server_error=True,
            allow_status=frozenset({_NOT_FOUND}),
        )
        return response.status_code != _NOT_FOUND

    async def create_property_group(
        self, object_type: ObjectType, group: PropertyGroupDefinition
    ) -> bool:
        """Create a property group. ``False`` means it already existed."""
        response = await self._request(
            "POST",
            f"/crm/properties/{API_VERSION}/{object_type.value}/groups",
            json_body=group.model_dump(by_alias=True, exclude_none=True),
            retry_on_server_error=False,
            allow_status=frozenset({_CONFLICT}),
        )
        return response.status_code != _CONFLICT

    async def _write_object(
        self,
        object_type: ObjectType,
        object_id: str | None,
        properties: Mapping[str, ProvenancedProperty | None],
    ) -> HubSpotObject:
        """Gate, render, then write. A create is never retried; an update is idempotent by value."""
        body = ObjectWriteRequest(properties=to_property_payload(properties))
        if object_id is None:
            response = await self._request(
                "POST",
                f"/crm/objects/{API_VERSION}/{object_type.value}",
                json_body=body.model_dump(by_alias=True, exclude_none=True),
                retry_on_server_error=False,
            )
        else:
            response = await self._request(
                "PATCH",
                f"/crm/objects/{API_VERSION}/{object_type.value}/{object_id}",
                json_body=body.model_dump(by_alias=True, exclude_none=True),
                retry_on_server_error=True,
            )
        return HubSpotObject.model_validate(response.json())

    async def create_company(
        self, properties: Mapping[str, ProvenancedProperty | None]
    ) -> HubSpotObject:
        """Create a company from provenanced fields. Dedupe first — see ``dedupe.py``."""
        return await self._write_object(ObjectType.companies, None, properties)

    async def update_company(
        self, object_id: str, properties: Mapping[str, ProvenancedProperty | None]
    ) -> HubSpotObject:
        """Update a company from provenanced fields."""
        return await self._write_object(ObjectType.companies, object_id, properties)

    async def create_contact(
        self, properties: Mapping[str, ProvenancedProperty | None]
    ) -> HubSpotObject:
        """Create a contact from provenanced fields. Dedupe first — see ``dedupe.py``."""
        return await self._write_object(ObjectType.contacts, None, properties)

    async def update_contact(
        self, object_id: str, properties: Mapping[str, ProvenancedProperty | None]
    ) -> HubSpotObject:
        """Update a contact from provenanced fields."""
        return await self._write_object(ObjectType.contacts, object_id, properties)

    async def create_task(
        self,
        task: TaskCreate,
        *,
        contact_id: str | None = None,
        company_id: str | None = None,
    ) -> HubSpotObject:
        """Create a cadence task. **Deliberately ungated — plain values, no provenance.**

        The write-gate governs prospect *field* writes, not task creation. T13 adopts the 22
        hand-typed prospects already in the portal; they carry no citable fields at all, and a gate
        applied here would refuse the ticket that exists to rescue them. A task is a thing we are
        asking a human to do, not a claim about a business, so there is nothing to cite.
        """
        associations: list[Association] = []
        if contact_id is not None:
            associations.append(
                Association(
                    to=AssociationTarget(id=contact_id),
                    types=[AssociationType(association_type_id=_TASK_TO_CONTACT)],
                )
            )
        if company_id is not None:
            associations.append(
                Association(
                    to=AssociationTarget(id=company_id),
                    types=[AssociationType(association_type_id=_TASK_TO_COMPANY)],
                )
            )

        body = TaskCreateRequest(properties=task.to_properties(), associations=associations)
        response = await self._request(
            "POST",
            f"/crm/objects/{API_VERSION}/{ObjectType.tasks.value}",
            json_body=body.model_dump(by_alias=True, exclude_none=True),
            retry_on_server_error=False,
        )
        logger.info(
            "promotion.hubspot.task_created",
            subject=task.subject,
            contact_id=contact_id,
            company_id=company_id,
        )
        return HubSpotObject.model_validate(response.json())

    async def batch_read(
        self,
        object_type: ObjectType,
        ids: Sequence[str],
        properties: Sequence[str],
    ) -> list[HubSpotObject]:
        """Read many records of one type, 100 per call.

        **A POST, but a read** — so a 5xx is retried, unlike a create: reading twice cannot
        duplicate anything. Ids that do not exist (deleted, archived) are absent from the result
        rather than an error; what an absence means is the caller's policy, not transport's.
        """
        unique_ids = list(dict.fromkeys(ids))
        results: list[HubSpotObject] = []
        for start in range(0, len(unique_ids), _BATCH_READ_LIMIT):
            chunk = unique_ids[start : start + _BATCH_READ_LIMIT]
            body = BatchReadRequest(
                properties=list(properties),
                inputs=[BatchReadInput(id=object_id) for object_id in chunk],
            )
            response = await self._request(
                "POST",
                f"/crm/objects/{API_VERSION}/{object_type.value}/batch/read",
                json_body=body.model_dump(by_alias=True, exclude_none=True),
                retry_on_server_error=True,
            )
            results.extend(BatchReadResponse.model_validate(response.json()).results)
        return results

    async def list_associated_ids(
        self,
        from_type: ObjectType,
        object_id: str,
        to_type: ObjectType,
    ) -> list[str]:
        """Every id of ``to_type`` associated with one record, following the paging cursor.

        There is no "activities since X" endpoint, and ``hs_timestamp`` is not a server-side
        filter on batch read — so the cadence's activity read is this walk plus a batch read,
        compared client-side. The page cap is a circuit breaker against a cursor that never ends,
        not a real limit: 20 pages of 500 is 10,000 engagements on one contact.
        """
        found: list[str] = []
        after: str | None = None
        for _ in range(_ASSOCIATION_MAX_PAGES):
            params: dict[str, str | int] = {"limit": _ASSOCIATION_PAGE_SIZE}
            if after is not None:
                params["after"] = after
            path = (
                f"/crm/objects/{API_VERSION}/{from_type.value}/{object_id}/associations/"
                f"{to_type.value}"
            )
            # The cursor is opaque: encoded, so a `+` or `=` in it survives the round trip.
            response = await self._request(
                "GET", f"{path}?{urlencode(params)}", retry_on_server_error=True
            )
            page = AssociationsPage.model_validate(response.json())
            ids = [
                associated.object_id
                for associated in page.results
                if associated.object_id is not None
            ]
            unrecognised = len(page.results) - len(ids)
            if unrecognised:
                # Neither `toObjectId` nor `id`. Reading this as "no associations" would make every
                # logged call and note vanish from the cadence's evidence — silent and total.
                logger.warning(
                    "promotion.hubspot.associations_unrecognised",
                    from_type=from_type.value,
                    to_type=to_type.value,
                    unrecognised=unrecognised,
                    results=len(page.results),
                )
                raise HubSpotResponseShapeError(
                    f"HubSpot's {from_type.value}→{to_type.value} associations came back in a "
                    f"shape with no object ids ({unrecognised} of {len(page.results)} results); "
                    "save the body as a fixture and teach AssociatedObject the new key"
                )
            found.extend(ids)
            after = page.next_after
            if after is None:
                return list(dict.fromkeys(found))
        logger.warning(
            "promotion.hubspot.associations_capped",
            from_type=from_type.value,
            to_type=to_type.value,
            pages=_ASSOCIATION_MAX_PAGES,
        )
        return list(dict.fromkeys(found))

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()


_client: HubSpotClient | None = None


def get_hubspot_client() -> HubSpotClient:
    """Return the process-wide client, building it on first use.

    The token is validated here rather than at startup: ``Settings`` keeps it optional (T1's
    decision) so the service — and ``/health`` — still boots without HubSpot configured.
    """
    global _client
    if _client is None:
        token = get_settings().hubspot_private_app_token
        # Blank counts as missing. `.env.example` ships the key uncommented with no value, so the
        # empty string is the shape a fresh clone actually produces — and a client built on it
        # sends `Bearer ` and fails with a 401 three layers away from the cause.
        if token is None or not token.strip():
            raise ConfigurationError(
                "HUBSPOT_PRIVATE_APP_TOKEN is not set — the HubSpot gateway cannot be built "
                "without it"
            )
        _client = HubSpotClient(token=token)
        logger.info("promotion.hubspot.client_created")
    return _client


def is_initialised() -> bool:
    """Whether the lazily-built client currently exists, so tests can assert isolation."""
    return _client is not None


async def aclose_hubspot_client() -> None:
    """Close the client. Called from the application lifespan on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        logger.info("promotion.hubspot.client_closed")
        _client = None
