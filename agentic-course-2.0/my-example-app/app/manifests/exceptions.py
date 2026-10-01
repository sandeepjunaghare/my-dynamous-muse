"""Deliberate failures in the manifests slice.

Every one derives from :class:`~app.core.exceptions.LocalProspectEngineError`, so ``app.main``'s
centralized handler renders it as structured JSON at the right status code. **Route handlers must
not catch these** — a local ``try/except`` would produce a second, inconsistent error shape.
"""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class ManifestError(LocalProspectEngineError):
    """Base for every deliberate failure in the manifests slice."""

    default_code: ClassVar[str] = "manifest_error"


class ManifestNotFoundError(ManifestError):
    """No manifest exists with the given id."""

    default_code: ClassVar[str] = "manifest_not_found"
    status_code: ClassVar[int] = 404

    def __init__(self, message: str, *, manifest_id: str) -> None:
        super().__init__(message)
        self.manifest_id = manifest_id


class ActiveManifestNotFoundError(ManifestError):
    """A vertical has no ACTIVE manifest — it may have drafts, or nothing at all.

    Deliberately distinct from :class:`ManifestNotFoundError`. T5 asking for a vertical's active
    manifest and finding only drafts is a different and more interesting failure than a bad id: it
    means nobody has accepted the terms yet, and the run must stop loudly rather than source
    against nothing.
    """

    default_code: ClassVar[str] = "active_manifest_not_found"
    status_code: ClassVar[int] = 404

    def __init__(self, message: str, *, vertical: str) -> None:
        super().__init__(message)
        self.vertical = vertical


class ManifestNotDraftError(ManifestError):
    """Only a DRAFT can be activated — an ACTIVE or SUPERSEDED row is already decided."""

    default_code: ClassVar[str] = "manifest_not_draft"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, manifest_id: str, status: str) -> None:
        super().__init__(message)
        self.manifest_id = manifest_id
        self.status = status


class TermsOfUseNotRecordedError(ManifestError):
    """A declared source has no recorded terms-of-use decision, so the manifest cannot go active.

    The gate this ticket exists to build. The refusal names the missing sources, because the whole
    remedy is for a person to read those terms and re-run ``activate`` naming them.
    """

    default_code: ClassVar[str] = "terms_of_use_not_recorded"
    status_code: ClassVar[int] = 409

    def __init__(self, message: str, *, vertical: str, missing_sources: tuple[str, ...]) -> None:
        super().__init__(message)
        self.vertical = vertical
        self.missing_sources = missing_sources


class UnknownSourceError(ManifestError):
    """``--accept-terms`` named a source this manifest does not declare.

    Accepting terms for a source that is not there is almost always a typo, and silently ignoring
    it would let a real source stay undecided while the caller believes they decided it.
    """

    default_code: ClassVar[str] = "unknown_source"
    status_code: ClassVar[int] = 422

    def __init__(self, message: str, *, vertical: str, unknown_sources: tuple[str, ...]) -> None:
        super().__init__(message)
        self.vertical = vertical
        self.unknown_sources = unknown_sources
