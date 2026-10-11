"""One GET with retries, shared by every source adapter.

The same loop as the HubSpot gateway's (``app/promotion/client.py``), cut down to what a read-only
public source needs: every call here is a GET, so a 5xx or a dropped connection is always safe to
retry — there is no create to duplicate.

**Query parameters are never logged.** QCMobile's webKey travels as one, so the log line carries
the URL without its query string, and an exception message never carries the request URL at all.
"""

import asyncio
import random
import time
from collections.abc import Mapping

import httpx

from app.core.logging import get_logger
from app.sourcing.exceptions import SourceRequestError

logger = get_logger(__name__)

# Spelled out rather than taken from `httpx.codes`; see the Pyright note in app/promotion/client.py.
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_SECONDS = 0.5
_JITTER_FRACTION = 0.1
_MAX_RETRY_AFTER_SECONDS = 60.0


def _backoff_seconds(attempt: int, base: float) -> float:
    """Exponential backoff with a little jitter, so retries do not resynchronize."""
    jitter = 1.0 + random.random() * _JITTER_FRACTION
    return base * (2.0 ** (attempt - 1)) * jitter


def _retry_after_seconds(response: httpx.Response, attempt: int, base: float) -> float:
    """``Retry-After`` when the source sends a usable one, capped; backoff otherwise."""
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return min(max(float(raw), 0.0), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return _backoff_seconds(attempt, base)


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    source: str,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    allow_status: frozenset[int] = frozenset(),
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> httpx.Response:
    """GET ``url``, retrying 429, 5xx and transport errors; return a 2xx or an allowed status.

    Anything else — or a retryable failure that outlasts ``max_attempts`` — raises
    :class:`SourceRequestError` naming the source, never the URL (it may carry a key).
    """
    attempt = 0
    while True:
        attempt += 1
        started = time.perf_counter()
        try:
            response = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "sourcing.http.request_failed",
                source=source,
                url=url,
                attempt=attempt,
                error=type(exc).__name__,
            )
            if attempt < max_attempts:
                await asyncio.sleep(_backoff_seconds(attempt, backoff_seconds))
                continue
            raise SourceRequestError(
                f"{source} did not answer after {attempt} attempts ({type(exc).__name__})",
                source=source,
            ) from exc

        status = response.status_code
        duration = round(time.perf_counter() - started, 3)
        if status < 300 or status in allow_status:
            logger.info(
                "sourcing.http.request_succeeded",
                source=source,
                url=url,
                status_code=status,
                duration_seconds=duration,
            )
            return response

        retryable = status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR
        if retryable and attempt < max_attempts:
            logger.warning(
                "sourcing.http.request_retrying",
                source=source,
                url=url,
                status_code=status,
                attempt=attempt,
            )
            await asyncio.sleep(_retry_after_seconds(response, attempt, backoff_seconds))
            continue

        logger.error(
            "sourcing.http.request_refused",
            source=source,
            url=url,
            status_code=status,
            attempt=attempt,
            duration_seconds=duration,
        )
        raise SourceRequestError(
            f"{source} answered {status} after {attempt} attempt(s)",
            source=source,
            status=status,
        )


def json_body(response: httpx.Response, *, source: str) -> object:
    """The response's JSON, or :class:`SourceRequestError` when it is not JSON (a proxy's page)."""
    try:
        parsed: object = response.json()
    except ValueError as exc:
        raise SourceRequestError(
            f"{source} answered {response.status_code} with a body that is not JSON",
            source=source,
            status=response.status_code,
        ) from exc
    return parsed
