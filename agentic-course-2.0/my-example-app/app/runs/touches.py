"""The Friday report's pure core: which touch a note is, and which week a moment belongs to.

**Touches are counted from note prefixes** (DECISION 1, 2026-10-17). A read of the portal found
every real touch logged as a free-text note; its calls and meetings are HubSpot's sample data. The
founders already start some door knocks with a label (``🚪 Door knock — 9/29/2026``,
``Door-knock visit, 9/24/26``). So a note whose text **starts with** one of :data:`PREFIXES` counts
toward that number. Leading emoji and punctuation are ignored, case is ignored, and ``-`` / ``_``
read as spaces. Anything else is an *unclassified touch*, which the report shows, so the
convention's adoption is visible rather than hidden.

No LLM here: the report stays deterministic, and a wrong count is fixed by the founder's label.

**A week is Monday 00:00 to the next Monday 00:00 in Dallas time** (``CADENCE_TZ``). It is built
from local midnights, never from fixed UTC offsets, so a week that crosses a DST change is 167 or
169 hours long, as it should be.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.runs.schemas import TouchKind

PREFIXES: Mapping[TouchKind, str] = {
    TouchKind.door_knock: "door knock",
    TouchKind.owner_conversation: "owner conversation",
    TouchKind.assessment_booked: "assessment booked",
}
"""What a note must start with, after normalising, to count toward each number."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _normalise(text: str) -> str:
    start = 0
    while start < len(text) and not text[start].isalnum():
        start += 1
    lowered = text[start:].lower().replace("-", " ").replace("_", " ")
    return " ".join(lowered.split())


def classify_note(text: str | None) -> TouchKind | None:
    """The touch a note's text starts with, or ``None`` when it starts with none of them.

    A prefix, never a search: ``Called; door knock tomorrow`` is a plan, not a door knock.
    """
    if not text:
        return None
    normalised = _normalise(text)
    for kind, prefix in PREFIXES.items():
        if normalised.startswith(prefix):
            return kind
    return None


def week_window(week_of: date, tz: ZoneInfo) -> tuple[datetime, datetime, date, date]:
    """The week containing ``week_of`` in ``tz``.

    Returns ``(start, end, monday, sunday)``: the half-open window ``[start, end)`` as aware UTC
    datetimes, and the week's first and last local dates.
    """
    monday = week_of - timedelta(days=week_of.weekday())
    next_monday = monday + timedelta(days=7)
    start = datetime.combine(monday, time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(next_monday, time(), tzinfo=tz).astimezone(UTC)
    return start, end, monday, next_monday - timedelta(days=1)


def local_date(moment: datetime, tz: ZoneInfo) -> date:
    """The date ``moment`` falls on in ``tz``: which week "this week" is."""
    return moment.astimezone(tz).date()


def epoch_ms(moment: datetime) -> str:
    """``moment`` as HubSpot's datetime filter value: epoch milliseconds, as a string."""
    return str((moment - _EPOCH) // timedelta(milliseconds=1))
