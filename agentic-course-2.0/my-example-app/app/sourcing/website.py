"""A business's website, from the domain of the email the registry states: one free homepage fetch.

**Measured 2026-10-11** (live census, freight v8's free predicates): of 4,462 DFW broker rows, 2,976
carry an email, but 1,896 of those are free-mail (gmail 1,344, yahoo 310…). Only 1,080 (24%) sit on
a business domain, and about 45 of those are shared service domains (process agents, franchisors).
So free-mail is refused by name here, and a domain three or more pool businesses share is refused
by the pool itself (``SourcingService.shared_email_domains``), naming no domain in code.

The fetch is the citation: a website is kept only when ``https://{domain}/`` answers a 2xx HTML page
on that host (or its ``www.``), so what is cited is a page that was read, not a guess from a domain.
**It never raises.** A missing website costs nothing, and a failed fetch must not fail a paid batch.
It makes one attempt, not ``http.get``'s retry loop: retrying 150 dead domains would add minutes for
an optional field.

**The registrant chooses the domain, so the fetch is fenced** (PR #32 H2). An IP address is no
domain. Every hop is checked *before* it is requested: ``https`` only, and only the domain or its
``www.``, so a redirect cannot aim the engine at a metadata endpoint or a LAN host. Only the status
and headers are read, never the body, and the whole check is bounded by :data:`_TOTAL_SECONDS`,
because httpx's timeout is per read and a page that drips bytes would outlast it. Not fenced: a
domain whose own DNS points at a private address; its first hop is requested, and what comes back
is read for the status and headers only.
"""

import asyncio
import ipaddress
from collections.abc import Callable
from datetime import datetime

import httpx

from app.core.logging import get_logger
from app.shared.provenance import ProvenancedValue, RetrievalMethod

logger = get_logger(__name__)

FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
        "mac.com", "att.net", "sbcglobal.net", "bellsouth.net", "comcast.net", "verizon.net",
        "charter.net", "cox.net", "proton.me", "protonmail.com", "gmx.com", "mail.com",
        "zoho.com", "yandex.com",
    }
)  # fmt: skip
"""Generic consumer mail providers: an address there says nothing about a business's own site.
Generic across verticals, so code rather than manifest data."""

SHARED_DOMAIN_MIN_BUSINESSES = 3
"""A domain this many distinct pool businesses share is a service's (a process agent, a franchisor,
a freight platform), not one business's own site (measured: ``fmcsaprocessagents.com`` 10 …)."""

MAX_REDIRECTS = 3
_TIMEOUT = httpx.Timeout(5.0)
_TOTAL_SECONDS = 10.0
"""The whole check, every hop included: ``_TIMEOUT`` bounds each read, not a page that drips."""
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SUCCESS_MIN, _SUCCESS_MAX = 200, 300


def email_domain(email: str) -> str | None:
    """The lowercased domain of ``email``, or ``None`` for free-mail or a malformed address."""
    local, at, domain = email.strip().lower().partition("@")
    domain = domain.rstrip(".")
    if not at or not local or not domain or "@" in domain or "." not in domain:
        return None
    if any(char.isspace() or char in "/?#:" for char in domain):
        return None
    if domain in FREE_MAIL_DOMAINS or _is_ip_address(domain):
        return None
    return domain


def _is_ip_address(domain: str) -> bool:
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return False
    return True


class HomepageChecker:
    """Fetches ``https://{domain}/`` once, following at most :data:`MAX_REDIRECTS` hops."""

    def __init__(self, client: httpx.AsyncClient, *, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock

    async def check(self, domain: str) -> ProvenancedValue[str] | None:
        """The homepage origin (``https://www.acme.com``), cited ``web_lookup`` to the page read.

        ``None`` — logged with a reason, never raised — on a timeout, a transport or TLS error, a
        URL that does not parse, a hop off ``https`` or off the domain, too many redirects, a
        non-2xx answer, or a page that is not HTML.
        """
        try:
            async with asyncio.timeout(_TOTAL_SECONDS):
                return await self._follow(domain)
        except (TimeoutError, httpx.TimeoutException):
            return self._unreachable(domain, "timeout")
        except httpx.HTTPError as exc:
            return self._unreachable(domain, f"transport_{type(exc).__name__}")
        except (httpx.InvalidURL, ValueError):
            # `InvalidURL` is no `HTTPError`; a bad `Location` must not fail a paid batch (H1).
            return self._unreachable(domain, "bad_url")

    async def _follow(self, domain: str) -> ProvenancedValue[str] | None:
        """Up to :data:`MAX_REDIRECTS` hops, each fenced before it is requested (H2)."""
        allowed_hosts = {domain, f"www.{domain}"}
        url = httpx.URL(f"https://{domain}/")
        for _hop in range(MAX_REDIRECTS + 1):
            if url.scheme != "https":
                return self._unreachable(domain, "not_https")
            if url.host not in allowed_hosts:
                return self._unreachable(domain, "other_host")
            async with self._client.stream(
                "GET", url, timeout=_TIMEOUT, follow_redirects=False
            ) as response:
                status = response.status_code
                headers = response.headers
            location = headers.get("location")
            if status in _REDIRECT_STATUSES and location:
                url = url.join(location)
                continue
            break
        else:
            return self._unreachable(domain, "too_many_redirects")

        if not _SUCCESS_MIN <= status < _SUCCESS_MAX:
            return self._unreachable(domain, f"status_{status}")
        content_type = headers.get("content-type", "").lower()
        if not content_type.startswith("text/html"):
            return self._unreachable(domain, "not_html")

        origin = f"{url.scheme}://{url.host}"
        logger.info("sourcing.website.homepage_found", domain=domain, origin=origin)
        return ProvenancedValue(
            value=origin,
            source_url=str(url),
            retrieved_at=self._clock(),
            retrieval_method=RetrievalMethod.web_lookup,
        )

    def _unreachable(self, domain: str, reason: str) -> ProvenancedValue[str] | None:
        """Log why there is no homepage, and say so: always ``None``."""
        logger.info("sourcing.website.homepage_unreachable", domain=domain, reason=reason)
        return None
