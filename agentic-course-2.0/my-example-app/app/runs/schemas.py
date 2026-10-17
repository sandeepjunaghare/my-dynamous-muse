"""The weekly run's shapes, and the Friday report's.

A weekly run is a request (vertical and run options) and a response (the ``weekly_run`` row, its
sourcing run, and M7's measured minutes). The Friday report holds the four playbook numbers, the
touches that could not be counted, and one summary per weekly run of that week.

**"Not configured" is a type, not a zero.** :class:`PlaybookNumber` refuses a value when it is not
configured and refuses ``None`` when it is, so "paid assessments sold" (GATE-A) can never be shown
as a real ``0``.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.manifests.schemas import SLUG_PATTERN
from app.promotion.schemas import ReviewTally
from app.sourcing.schemas import SourcingRunResponse


class WeeklyRunStatus(StrEnum):
    """Where a weekly run is. A sourcing run's four words, with their own meanings here."""

    running = "running"
    """Accepted, and sourcing or promoting now."""

    completed = "completed"
    """Sourced and promoted with nothing to report."""

    degraded = "degraded"
    """Finished, short of its goal: sourcing degraded, or promotion stopped early or had failures.

    The detail names every reason."""

    failed = "failed"
    """Stopped: sourcing failed (nothing promoted), promotion raised, or the run was reaped."""


class WeeklyRunRequest(BaseModel):
    """What ``POST /runs`` and ``lpe runs start`` take."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vertical: str = Field(pattern=SLUG_PATTERN, max_length=64)
    """A vertical with an ACTIVE manifest. Its ICP band is the manifest's own."""
    batch_size: int | None = Field(default=None, gt=0)
    """Candidates taken from the backlog. ``None`` means ``SOURCING_BATCH_SIZE``."""
    allow_degraded: bool = False
    """Go on without a declared source that cannot run (the batch is spent without its evidence)."""
    dry_run: bool = False
    """Source for real, then promote as a dry run: the same reads, no HubSpot write."""


class WeeklyRunResponse(BaseModel):
    """One ``weekly_run`` row, with its sourcing run when there is one."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vertical: str
    status: WeeklyRunStatus
    status_detail: str | None
    dry_run: bool
    batch_size: int | None
    allow_degraded: bool
    requested_at: datetime
    sourcing_run_id: UUID | None
    sourced_at: datetime | None
    promoted_at: datetime | None
    finished_at: datetime | None
    promotion_counts: dict[str, int]
    promotion_stopped: str | None
    sourcing: SourcingRunResponse | None = None
    """Filled by the service from the sourcing slice, never by the ORM."""
    brief_to_hubspot_minutes: float | None = None
    """M7: minutes from the request to promotion finishing. ``None`` until promoted.

    Derived from the two timestamps on every validation, never taken from input (a field plus a
    validator rather than ``computed_field``, whose property decorator needs a type suppression)."""

    @model_validator(mode="after")
    def _measure_m7(self) -> Self:
        self.brief_to_hubspot_minutes = (
            None
            if self.promoted_at is None
            else (self.promoted_at - self.requested_at).total_seconds() / 60
        )
        return self


class TouchKind(StrEnum):
    """The touches a note's prefix counts toward (DECISION 1, 2026-10-17)."""

    door_knock = "door_knock"
    owner_conversation = "owner_conversation"
    assessment_booked = "assessment_booked"


class PlaybookNumber(BaseModel):
    """One of the four Friday numbers. Either counted, or honestly not configured."""

    model_config = ConfigDict(frozen=True)

    name: str
    value: int | None
    configured: bool

    @model_validator(mode="after")
    def _configured_iff_counted(self) -> Self:
        if self.configured != (self.value is not None):
            raise ValueError(
                "a configured number has a value, and one that is not configured has none — "
                "never a 0 standing in for 'we do not measure this'"
            )
        return self


FUNNEL_COUNTERS: tuple[str, ...] = (
    "batched",
    "qualification_disqualified",
    "places_verified",
    "verify_website_only",
    "places_mail_drop",
    "places_no_listing",
    "places_other_business",
    "judgment_disqualified",
    "owner_absent",
    "owner_unverified_skipped",
    "routing_mail_drop",
    "routing_geocode_failed",
    "routing_clustered",
)
"""The sourcing counts the report shows per run, in funnel order. Each is a key a stage reports;
a test pins that, so a renamed counter breaks a test rather than quietly showing ``None``."""

PROMOTION_COUNTERS: tuple[str, ...] = (
    "created",
    "promoted_unverified",
    "refused_contradicted",
    "refused_no_principal",
    "refused_missing_fields",
    "refused_disqualified",
    "duplicate_in_hubspot",
    "failed",
)
"""The promotion counts the report shows per run. Each is in ``PROMOTE_COUNTS``."""


class RunSummary(BaseModel):
    """One weekly run, as the Friday report shows it."""

    model_config = ConfigDict(frozen=True)

    run: WeeklyRunResponse
    within_target: bool | None
    """M7 against ``WEEKLY_RUN_TARGET_MINUTES``. ``None`` when the run never reached promotion."""
    total_usd: Decimal | None
    """What sourcing spent. ``None`` when no sourcing run was opened."""
    funnel: dict[str, int | None]
    """:data:`FUNNEL_COUNTERS`. ``None`` means the stage never reported that key."""
    promotion: dict[str, int]
    """:data:`PROMOTION_COUNTERS`. Empty when promotion never ran."""
    review: ReviewTally


class WeeklyReport(BaseModel):
    """The Friday report: the playbook's four numbers, then each weekly run of the week."""

    model_config = ConfigDict(frozen=True)

    week_start: date
    """The Monday, in :attr:`timezone`."""
    week_end: date
    """The Sunday, inclusive."""
    timezone: str
    playbook: tuple[PlaybookNumber, ...]
    unclassified_touches: int
    """Notes in the week with none of the prefixes: touches the numbers above cannot count."""
    runs: tuple[RunSummary, ...]
    target_minutes: int
