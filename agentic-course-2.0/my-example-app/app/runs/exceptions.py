"""The weekly run's refusals. Each derives from ``LocalProspectEngineError``, so ``app.main``
renders it as structured JSON at its own status code, and ``lpe`` prints it as one line."""

from typing import ClassVar

from app.core.exceptions import LocalProspectEngineError


class WeeklyRunError(LocalProspectEngineError):
    """Base for the weekly run's own failures."""

    default_code: ClassVar[str] = "weekly_run_error"


class WeeklyRunInProgressError(WeeklyRunError):
    """A weekly run is already ``running``. The message names it and when it was requested."""

    default_code: ClassVar[str] = "weekly_run_in_progress"
    status_code: ClassVar[int] = 409


class WeeklyRunNotFoundError(WeeklyRunError):
    """No weekly run has that id."""

    default_code: ClassVar[str] = "weekly_run_not_found"
    status_code: ClassVar[int] = 404
