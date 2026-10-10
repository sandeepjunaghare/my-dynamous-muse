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

    ``orphan_task_id`` is a task this enrolment created before another run took the row — left
    open in HubSpot, for the person to close.
    """

    default_code: ClassVar[str] = "already_enrolled"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, contact_id: str, orphan_task_id: str | None = None) -> None:
        super().__init__(message)
        self.contact_id = contact_id
        self.orphan_task_id = orphan_task_id


class NotEnrolledError(CadenceError):
    """This contact has no cadence, so there is nothing to park."""

    default_code: ClassVar[str] = "not_enrolled"
    status_code: ClassVar[int] = 404

    def __init__(self, message: str, *, contact_id: str) -> None:
        super().__init__(message)
        self.contact_id = contact_id


class AlreadyParkedError(CadenceError):
    """This contact's cadence is already finished. Parking is final; there is no unpark."""

    default_code: ClassVar[str] = "already_parked"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, contact_id: str) -> None:
        super().__init__(message)
        self.contact_id = contact_id


class SyncRunningError(CadenceError):
    """A sync holds the run lock, so a change to the schedule must wait for it to finish.

    A running sync has already loaded its rows; parking one under it could see the sync advance a
    prospect a human just finished.
    """

    default_code: ClassVar[str] = "sync_running"
    status_code: ClassVar[int] = 409


class RosterError(CadenceError):
    """The adoption roster cannot be used: missing, not TOML, or an entry that fails validation.

    The message names the file and, where known, the entry — the person fixing it has the file
    open, not the parser's internals.
    """

    default_code: ClassVar[str] = "invalid_roster"
    status_code: ClassVar[int] = 422


class UnadoptableContactError(CadenceError):
    """One roster entry's contact cannot be planned — it no longer exists in HubSpot, or HubSpot
    gave no ``createdate`` to count its evidence from.

    Reported against that entry, never raised out of a run: the other entries carry on. ``code``
    says which (``contact_not_found``, ``contact_created_at_missing``).
    """

    default_code: ClassVar[str] = "unadoptable_contact"
    status_code: ClassVar[int] = 422

    def __init__(self, message: str, *, contact_id: str, code: str) -> None:
        super().__init__(message, code=code)
        self.contact_id = contact_id
