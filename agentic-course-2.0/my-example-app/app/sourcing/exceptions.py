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


class NoRegistrySourceError(SourcingError):
    """The manifest binds no discovery source, or more than one.

    Every candidate comes from the one registry a manifest names (D13 rule 3: Places never
    discovers). Zero means nothing to search; two would mean two pools with no rule for merging
    them. Either way the manifest needs fixing, not the run retrying.
    """

    default_code: ClassVar[str] = "no_registry_source"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, manifest_id: str, bound: tuple[str, ...]) -> None:
        super().__init__(message)
        self.manifest_id = manifest_id
        self.bound = bound


class SourceRequestError(SourcingError):
    """A declared source failed after retries, or answered something unusable."""

    default_code: ClassVar[str] = "source_request_failed"
    status_code: ClassVar[int] = 502

    def __init__(self, message: str, *, source: str, status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


class SourceAuthError(SourceRequestError):
    """A source refused our credential, or we have none to send (QCMobile's webKey).

    Distinct from a transient failure: retrying the next record will not help, so the stage stops
    calling that source for the rest of the run and finishes ``degraded``.
    """

    default_code: ClassVar[str] = "source_auth_failed"
