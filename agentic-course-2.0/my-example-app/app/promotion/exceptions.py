"""The HubSpot gateway's failure modes, as types.

Everything here derives from :class:`LocalProspectEngineError`, so the centralized handlers in
``app.main`` render any of them as structured JSON without knowing this slice exists.

The hierarchy exists to make the one distinction a caller actually acts on: a failure retrying
could fix (rate limiting), one it never could (a missing scope), and one where retrying is
positively dangerous (a create that may already have succeeded upstream).

The promotion errors at the bottom are **not** HubSpot failures — nothing has reached the portal
when one is raised — so they derive from :class:`LocalProspectEngineError` directly.
"""

from enum import StrEnum
from typing import ClassVar
from uuid import UUID

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


class HubSpotResponseShapeError(HubSpotError):
    """A 2xx whose body has a shape we do not recognise, where guessing would lose data.

    Raised rather than read as "nothing there": an association walk that silently returned no ids
    would make every logged call and note vanish from the cadence's evidence, and the machine would
    nag about all of it. Not retried — the same request returns the same shape.
    """

    default_code: ClassVar[str] = "hubspot_response_shape_unrecognised"


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
    ungated by design, and a test asserts it. **Engine metadata** is the one other carve-out (T9,
    OQ6-A): the closed ``RecordMeta`` — vertical, review status, route label, owner — merged after
    the prospect fields pass, because those are facts about our process with nothing to cite.

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


class RefusalReason(StrEnum):
    """Why a candidate was not promoted (T9, OQ1-D). Eligibility checks them in its own fixed order
    (``app.promotion.eligibility``: disqualified, unverified, the missing fields, dead_score), which
    is not this declaration order.

    ``no_principal`` is ``missing_fields`` where the owner is the **only** gap — counted on its own
    because "no owner found" is the yield question T6 answers, and a company is never promoted
    without its contact. ``unverified`` is neither Places-verified nor website-verified
    (website-verification B6): such a candidate never reaches the owner node, so without its own
    reason it would be counted ``no_principal`` and hide the real gap. Kept here, beside the error
    that carries it, so it has one home.
    """

    disqualified = "disqualified"
    unverified = "unverified"
    dead_score = "dead_score"
    no_principal = "no_principal"
    missing_fields = "missing_fields"


class PromotionRefusedError(LocalProspectEngineError):
    """A candidate that may not be promoted: suppressed, unverified, a cited-dead score, or missing
    a field.

    Raised before anything is written, so it costs no HubSpot request and leaves no ledger row.
    ``fields`` names **every** missing required field in declaration order, not the first — the
    same "collect every offender, raise once" rule as the write-gate.
    """

    default_code: ClassVar[str] = "promotion_refused"
    status_code: ClassVar[int] = 422

    def __init__(
        self,
        message: str,
        *,
        candidate_id: str,
        fields: tuple[str, ...],
        reason: RefusalReason,
    ) -> None:
        super().__init__(message)
        self.candidate_id = candidate_id
        self.fields = fields
        self.reason = reason


class RunNotPromotableError(LocalProspectEngineError):
    """The run is still ``running``, or ``failed``.

    A failed run's batch goes back to the pool and is re-batched as **new** candidate rows, so
    promoting from it would promote the same business twice under two candidate ids. Only
    ``completed`` and ``degraded`` runs are promoted.
    """

    default_code: ClassVar[str] = "run_not_promotable"
    status_code: ClassVar[int] = 409


class PromotionStateError(LocalProspectEngineError):
    """A ledger transition refused because the row was no longer in the expected state.

    Another invocation moved it first — a lost compare-and-set. Theirs stands. ``status`` is the
    state the row was found in, not ours; it is named ``status``, never ``status_code``, which is
    the ClassVar the central handlers read.
    """

    default_code: ClassVar[str] = "promotion_state_conflict"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, promotion_id: UUID, status: str | None) -> None:
        super().__init__(message)
        self.promotion_id = promotion_id
        self.status = status
