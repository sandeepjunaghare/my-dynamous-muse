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
"""

from collections.abc import Callable
from datetime import datetime
from urllib.parse import urljoin

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
    if domain in FREE_MAIL_DOMAINS:
        return None
    return domain


class HomepageChecker:
    """Fetches ``https://{domain}/`` once, following at most :data:`MAX_REDIRECTS` hops."""

    def __init__(self, client: httpx.AsyncClient, *, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock

    async def check(self, domain: str) -> ProvenancedValue[str] | None:
        """The homepage origin (``https://www.acme.com``), cited ``web_lookup`` to the page read.

        ``None`` — logged with a reason, never raised — on a timeout, a transport or TLS error, a
        non-2xx answer, a redirect off the domain, too many redirects, or a page that is not HTML.
        """
        allowed_hosts = {domain, f"www.{domain}"}
        url = f"https://{domain}/"
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                response = await self._client.get(url, timeout=_TIMEOUT, follow_redirects=False)
                location = response.headers.get("location")
                if response.status_code in _REDIRECT_STATUSES and location:
                    url = urljoin(url, location)
                    continue
                break
            else:
                return self._unreachable(domain, "too_many_redirects")
        except httpx.TimeoutException:
            return self._unreachable(domain, "timeout")
        except httpx.HTTPError as exc:
            return self._unreachable(domain, f"transport_{type(exc).__name__}")

        final = httpx.URL(url)
        if final.host not in allowed_hosts:
            return self._unreachable(domain, "other_host")
        if not _SUCCESS_MIN <= response.status_code < _SUCCESS_MAX:
            return self._unreachable(domain, f"status_{response.status_code}")
        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("text/html"):
            return self._unreachable(domain, "not_html")

        origin = f"{final.scheme}://{final.host}"
        logger.info("sourcing.website.homepage_found", domain=domain, origin=origin)
        return ProvenancedValue(
            value=origin,
            source_url=str(final),
            retrieved_at=self._clock(),
            retrieval_method=RetrievalMethod.web_lookup,
        )

    def _unreachable(self, domain: str, reason: str) -> ProvenancedValue[str] | None:
        """Log why there is no homepage, and say so: always ``None``."""
        logger.info("sourcing.website.homepage_unreachable", domain=domain, reason=reason)
        return None
