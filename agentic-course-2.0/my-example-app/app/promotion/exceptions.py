"""The HubSpot gateway's failure modes, as types.

Everything here derives from :class:`LocalProspectEngineError`, so the centralized handlers in
``app.main`` render any of them as structured JSON without knowing this slice exists.

The hierarchy exists to make the one distinction a caller actually acts on: a failure retrying
could fix (rate limiting), one it never could (a missing scope), and one where retrying is
positively dangerous (a create that may already have succeeded upstream).
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class HubSpotError(LocalProspectEngineError):
    """Base for every failure of the HubSpot gateway.

    ``502`` rather than ``500``: when one of these reaches a route, the fault is upstream of us.
    :class:`UnprovenancedWriteError` overrides it, because that one is ours.
    """

    default_code: ClassVar[str] = "hubspot_error"
    status_code: ClassVar[int] = 502


class HubSpotAuthError(HubSpotError):
    """A 401 or 403.

    The fix is a token with the right scopes, not a retry — so this is never retried. HubSpot names
    the missing scope in the 403 message; it is carried through on ``missing_scope`` when it can be
    parsed, because "which scope" is the entire content of the fix.
    """

    default_code: ClassVar[str] = "hubspot_auth_error"

    def __init__(self, message: str, *, missing_scope: str | None = None) -> None:
        super().__init__(message)
        self.missing_scope = missing_scope


class HubSpotRateLimitError(HubSpotError):
    """Raised only once the bounded retries are exhausted, or immediately on a daily-quota 429.

    ``policy`` is HubSpot's ``policyName``: ``TEN_SECONDLY_ROLLING`` is burst pressure and worth
    waiting out, ``DAILY`` means 250,000 calls in a day — a bug, not load, so it fails fast rather
    than sleeping.
    """

    default_code: ClassVar[str] = "hubspot_rate_limited"

    def __init__(self, message: str, *, attempts: int, policy: str | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.policy = policy


class HubSpotResponseError(HubSpotError):
    """Any other non-2xx response.

    The received status lives on ``status_code_received``, **not** ``status_code`` — the latter is
    the ClassVar ``app.main`` reads to choose the status of the error it renders, and shadowing it
    with an instance attribute would make a HubSpot 404 come back to the caller as an HTTP 404
    describing our own service.
    """

    default_code: ClassVar[str] = "hubspot_response_error"

    def __init__(
        self,
        message: str,
        *,
        status_code_received: int,
        category: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code_received = status_code_received
        self.category = category
        self.correlation_id = correlation_id


class HubSpotTransportError(HubSpotError):
    """The request never produced a response — a connect failure, a timeout, a broken socket.

    Distinct from :class:`HubSpotResponseError` because the caller cannot know whether the request
    arrived. On a create that ambiguity is the whole problem: a timeout is not permission to retry.
    """

    default_code: ClassVar[str] = "hubspot_transport_error"


class UnprovenancedWriteError(HubSpotError):
    """The write-gate refusing: a prospect field was offered with nothing to cite it.

    **This governs prospect field writes, not task creation.** T13 adopts the 22 hand-typed records
    already in the portal, which carry no citable fields at all; a gate that refused them would
    refuse the ticket that exists to rescue them. :meth:`HubSpotClient.create_task` is therefore
    ungated by design, and a test asserts it.

    ``status_code`` is 500, not the base class's 502: nothing left the process, so calling this a
    bad *gateway* would point the reader upstream at exactly the wrong moment. The offending field
    names are on ``fields`` — all of them, not the first, so a caller fixes one round-trip's worth
    of problems at a time.
    """

    default_code: ClassVar[str] = "unprovenanced_write"
    status_code: ClassVar[int] = 500

    def __init__(self, message: str, *, fields: tuple[str, ...]) -> None:
        super().__init__(message)
        self.fields = fields
