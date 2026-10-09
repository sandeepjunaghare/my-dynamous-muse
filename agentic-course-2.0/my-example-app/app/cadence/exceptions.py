"""Deliberate failures in the cadence slice.

Every one derives from :class:`~app.core.exceptions.LocalProspectEngineError`, so ``app.main``'s
centralized handler renders it as structured JSON at the right status code, and ``lpe`` prints it
as one line. **Route handlers must not catch these.**

HubSpot's own failures are not redeclared here: they are the gateway's
(:mod:`app.promotion.exceptions`), and a sync records them per prospect rather than raising.
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class CadenceError(LocalProspectEngineError):
    """Base for every deliberate failure in the cadence slice."""

    default_code: ClassVar[str] = "cadence_error"


class AlreadyEnrolledError(CadenceError):
    """This contact already has a cadence — live or parked.

    Re-enrolling would start the cycle again from touch one, and **cycle position is never reset**
    (CLAUDE.md). A parked prospect coming back is a decision for a human, not a second ``enrol``.
    """

    default_code: ClassVar[str] = "already_enrolled"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, contact_id: str) -> None:
        super().__init__(message)
        self.contact_id = contact_id
