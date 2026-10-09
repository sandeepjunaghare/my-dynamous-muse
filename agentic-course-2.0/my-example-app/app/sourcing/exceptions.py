"""Deliberate failures in the sourcing slice.

Every one derives from :class:`~app.core.exceptions.LocalProspectEngineError`, so ``app.main``'s
centralized handler can render it as structured JSON should a route ever surface one.
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class SourcingError(LocalProspectEngineError):
    """Base for every deliberate failure in the sourcing slice."""

    default_code: ClassVar[str] = "sourcing_error"


class SourcingRunNotFoundError(SourcingError):
    """No sourcing run exists with the given id."""

    default_code: ClassVar[str] = "sourcing_run_not_found"
    status_code: ClassVar[int] = 404

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


class SourcingRunNotRunningError(SourcingError):
    """The run has already finished, so nothing more may be written into it.

    A finished run is a record of what happened. Adding candidates to it afterwards, or finishing
    it a second time with different counts, would make its counts and cost describe a run that
    never took place.
    """

    default_code: ClassVar[str] = "sourcing_run_not_running"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, run_id: str, status: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.status = status
