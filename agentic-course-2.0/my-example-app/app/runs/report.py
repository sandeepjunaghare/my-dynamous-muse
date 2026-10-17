"""The Friday report: the playbook's four numbers, then what each of the week's weekly runs did.

1. **Playbook numbers** from HubSpot notes in the Dallas-time week, classified by prefix
   (``touches.py``). Paid assessments sold is **not configured** (GATE-A, D10): there is no $999
   assessment pipeline to read, and the report says so rather than showing a zero.
2. **Runs**: each ``weekly_run`` requested in the week, with its sourcing funnel and cost, M7
   against ``WEEKLY_RUN_TARGET_MINUTES``, and promotion's counts.
3. **Review** (M6): accepted, rejected and pending per run, from the ``promotion`` ledger, so as of
   the last daily ``lpe promotion sync``.

Read live every time; nothing is stored (split store: outcomes live in HubSpot). HubSpot's search
index lags writes by seconds to minutes, so a note logged just before the report runs may be missed:
acceptable for a Friday summary. Any HubSpot failure propagates, because a report with a missing
section is a wrong report.
"""

from collections import Counter
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CADENCE_TZ
from app.cadence.service import Clock, HubSpotFactory, utc_now
from app.core.config import get_settings
from app.core.logging import get_logger
from app.promotion.client import get_hubspot_client
from app.promotion.schemas import (
    ObjectType,
    ReviewTally,
    SearchFilter,
    SearchFilterGroup,
    SearchOperator,
    SearchRequest,
)
from app.promotion.service import PromotionService
from app.runs.exceptions import WeeklyRunError
from app.runs.models import WeeklyRun
from app.runs.repository import WeeklyRunRepository
from app.runs.schemas import (
    FUNNEL_COUNTERS,
    PROMOTION_COUNTERS,
    PlaybookNumber,
    RunSummary,
    TouchKind,
    WeeklyReport,
    WeeklyRunResponse,
)
from app.runs.touches import classify_note, epoch_ms, local_date, week_window
from app.sourcing.service import SourcingService

logger = get_logger(__name__)

PAGE_SIZE = 200
"""HubSpot's maximum ``limit`` for a search."""

MAX_PAGES = 20
"""4,000 notes in a week is far past what two founders log. Past it, refuse; never truncate."""

_NOTE_PROPERTIES = ["hs_body_preview", "hs_timestamp"]
"""``hs_body_preview`` is the note as plain text; ``hs_note_body`` is HTML."""

NOT_CONFIGURED = "paid assessments sold"


class WeeklyReportService:
    """Builds the Friday report for one week."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        hubspot: HubSpotFactory = get_hubspot_client,
        clock: Clock = utc_now,
    ) -> None:
        self._session = session
        self._hubspot = hubspot
        self._clock = clock

    async def build(self, week_of: date | None = None) -> WeeklyReport:
        """The report for the week containing ``week_of``; this week (Dallas time) when ``None``."""
        settings = get_settings()
        start, end, monday, sunday = week_window(
            week_of or local_date(self._clock(), CADENCE_TZ), CADENCE_TZ
        )
        touches, unclassified = await self._touches(start, end)
        playbook = (
            PlaybookNumber(
                name="doors knocked", value=touches[TouchKind.door_knock], configured=True
            ),
            PlaybookNumber(
                name="owner conversations",
                value=touches[TouchKind.owner_conversation],
                configured=True,
            ),
            PlaybookNumber(
                name="assessments booked",
                value=touches[TouchKind.assessment_booked],
                configured=True,
            ),
            # GATE-A (D10): no $999 assessment pipeline exists to count. Not configured, never 0.
            PlaybookNumber(name=NOT_CONFIGURED, value=None, configured=False),
        )
        runs = await self._runs(start, end, settings.weekly_run_target_minutes)
        return WeeklyReport(
            week_start=monday,
            week_end=sunday,
            timezone=CADENCE_TZ.key,
            playbook=playbook,
            unclassified_touches=unclassified,
            runs=runs,
            target_minutes=settings.weekly_run_target_minutes,
        )

    async def _touches(self, start: datetime, end: datetime) -> tuple[Counter[TouchKind], int]:
        """Every note with ``hs_timestamp`` in ``[start, end)``, counted by prefix."""
        client = self._hubspot()
        window = SearchFilterGroup(
            filters=[
                SearchFilter(
                    property_name="hs_timestamp", operator=SearchOperator.gte, value=epoch_ms(start)
                ),
                SearchFilter(
                    property_name="hs_timestamp", operator=SearchOperator.lt, value=epoch_ms(end)
                ),
            ]
        )
        counted: Counter[TouchKind] = Counter()
        unclassified = notes = 0
        after: str | None = None
        for _ in range(MAX_PAGES):
            page = await client.search(
                ObjectType.notes,
                SearchRequest(
                    filter_groups=[window],
                    properties=_NOTE_PROPERTIES,
                    limit=PAGE_SIZE,
                    after=after,
                ),
            )
            for note in page.results:
                notes += 1
                kind = classify_note(note.properties.get("hs_body_preview"))
                if kind is None:
                    unclassified += 1
                else:
                    counted[kind] += 1
            after = page.next_after
            if after is None:
                break
        else:
            raise WeeklyRunError(
                f"more than {MAX_PAGES * PAGE_SIZE:,} notes in one week; the report refuses to "
                "count part of a week"
            )
        logger.info(
            "runs.report.notes_read",
            notes=notes,
            unclassified=unclassified,
            **{kind.value: counted[kind] for kind in TouchKind},
        )
        return counted, unclassified

    async def _runs(
        self, start: datetime, end: datetime, target_minutes: int
    ) -> tuple[RunSummary, ...]:
        rows = await WeeklyRunRepository(self._session).requested_between(start, end)
        sourcing_ids = [row.sourcing_run_id for row in rows if row.sourcing_run_id is not None]
        tallies = await PromotionService(self._session, hubspot=self._hubspot).review_tally(
            sourcing_ids
        )
        return tuple([await self._summary(row, tallies, target_minutes) for row in rows])

    async def _summary(
        self, row: WeeklyRun, tallies: dict[UUID, ReviewTally], target_minutes: int
    ) -> RunSummary:
        run = WeeklyRunResponse.model_validate(row)
        if row.sourcing_run_id is not None:
            run.sourcing = await SourcingService(self._session).get_run(row.sourcing_run_id)
        sourcing = run.sourcing
        minutes = run.brief_to_hubspot_minutes
        return RunSummary(
            run=run,
            within_target=None if minutes is None else minutes <= target_minutes,
            total_usd=None if sourcing is None else sourcing.cost.total_usd(),
            funnel={
                name: None if sourcing is None else sourcing.counts.get(name)
                for name in FUNNEL_COUNTERS
            },
            promotion=(
                {}
                if run.promoted_at is None
                else {name: run.promotion_counts.get(name, 0) for name in PROMOTION_COUNTERS}
            ),
            review=(
                tallies.get(row.sourcing_run_id, ReviewTally())
                if row.sourcing_run_id is not None
                else ReviewTally()
            ),
        )
