"""The service's exception hierarchy.

Feature slices derive their own errors from :class:`LocalProspectEngineError` so the centralized
handlers in ``app.main`` can render any of them as structured JSON without knowing the slice.
"""

from typing import ClassVar


class LocalProspectEngineError(Exception):
    """Base class for every error this service raises deliberately.

    Carries a human-readable ``message`` and a stable machine-readable ``code``. The code is what
    clients and logs match on; the message is what a person reads.
    """

    default_code: ClassVar[str] = "internal_error"
    status_code: ClassVar[int] = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code


class ConfigurationError(LocalProspectEngineError):
    """The service is misconfigured — a missing, unknown or unusable setting."""

    default_code: ClassVar[str] = "configuration_error"


class DatabaseError(LocalProspectEngineError):
    """A database operation failed for a reason the caller cannot fix by retrying differently."""

    default_code: ClassVar[str] = "database_error"


class CostLimitExceededError(LocalProspectEngineError):
    """A run hit its per-kind cap on billable calls.

    **This is not a crash path.** T6 catches it to mark a run *degraded* — the run keeps whatever
    it has already verified and reports what it dropped, rather than failing. Do not turn this into
    a 500; a cap being reached is the circuit breaker working as designed (D9).
    """

    default_code: ClassVar[str] = "cost_limit_exceeded"

    def __init__(self, message: str, *, kind: str, cap: int, recorded: int) -> None:
        super().__init__(message)
        self.kind = kind
        self.cap = cap
        self.recorded = recorded
