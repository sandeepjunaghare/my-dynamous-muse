"""Note classification and the report's week, against real notes from the 2026-10-17 read."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.cadence.machine import CADENCE_TZ
from app.runs.schemas import TouchKind
from app.runs.touches import classify_note, epoch_ms, local_date, week_window


class TestClassifyNote:
    @pytest.mark.parametrize(
        "text",
        ["🚪 Door knock — 9/29/2026", "Door-knock visit, 9/24/26", "DOOR KNOCK", "door_knock: no"],
    )
    def test_door_knocks(self, text: str) -> None:
        assert classify_note(text) is TouchKind.door_knock

    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("Owner conversation: Paul says call in Q1", TouchKind.owner_conversation),
            ("  📅 Assessment booked — Acme, 10/22", TouchKind.assessment_booked),
            ("— assessment-booked for Tuesday", TouchKind.assessment_booked),
        ],
    )
    def test_the_other_two(self, text: str, kind: TouchKind) -> None:
        assert classify_note(text) is kind

    @pytest.mark.parametrize(
        "text",
        [
            "Cold call 9/22/2026 - No answer.",
            "Left voicemail for dispatch",
            "connected with Paul, he asked us to email",
            "Call with Jorge Rodriguez about their routing",
        ],
    )
    def test_real_prose_notes_are_unclassified(self, text: str) -> None:
        """The convention is new; notes logged before it count as unclassified, not as touches."""
        assert classify_note(text) is None

    def test_a_label_mid_sentence_is_not_a_prefix(self) -> None:
        assert classify_note("Called; door knock tomorrow") is None

    @pytest.mark.parametrize("text", [None, "", "   ", "🚪"])
    def test_empty_is_unclassified(self, text: str | None) -> None:
        assert classify_note(text) is None


class TestWeekWindow:
    def test_a_wednesday_maps_to_monday_through_monday(self) -> None:
        start, end, monday, sunday = week_window(date(2026, 10, 14), CADENCE_TZ)

        assert (monday, sunday) == (date(2026, 10, 12), date(2026, 10, 18))
        # CDT is UTC-5.
        assert start == datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
        assert end == datetime(2026, 10, 19, 5, 0, tzinfo=UTC)

    def test_a_monday_is_its_own_week(self) -> None:
        _, _, monday, _ = week_window(date(2026, 10, 12), CADENCE_TZ)
        assert monday == date(2026, 10, 12)

    def test_a_sunday_belongs_to_the_week_before(self) -> None:
        _, _, monday, sunday = week_window(date(2026, 10, 18), CADENCE_TZ)
        assert (monday, sunday) == (date(2026, 10, 12), date(2026, 10, 18))

    def test_the_fall_back_week_is_169_hours(self) -> None:
        """2026-11-01 is the DST change; the week starting 2026-10-26 holds it."""
        start, end, _, _ = week_window(date(2026, 10, 28), CADENCE_TZ)
        assert end - start == timedelta(hours=169)
        assert end == datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # CST is UTC-6.

    def test_every_datetime_is_utc_aware(self) -> None:
        start, end, _, _ = week_window(date(2026, 10, 14), CADENCE_TZ)
        assert start.tzinfo is UTC
        assert end.tzinfo is UTC


class TestLocalDate:
    def test_monday_morning_utc_is_still_sunday_in_dallas(self) -> None:
        """A note at Monday 03:00 UTC was logged on Sunday evening, so it is last week's."""
        moment = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
        assert local_date(moment, CADENCE_TZ) == date(2026, 10, 18)

    def test_monday_half_past_midnight_local_is_this_week(self) -> None:
        moment = datetime(2026, 10, 19, 5, 30, tzinfo=UTC)  # 00:30 CDT
        _, _, monday, _ = week_window(local_date(moment, CADENCE_TZ), CADENCE_TZ)
        assert monday == date(2026, 10, 19)


class TestEpochMs:
    def test_a_known_instant(self) -> None:
        assert epoch_ms(datetime(2026, 10, 12, 5, 0, tzinfo=UTC)) == "1791781200000"

    def test_milliseconds_survive(self) -> None:
        moment = datetime(1970, 1, 1, 0, 0, 1, 250_000, tzinfo=UTC)
        assert epoch_ms(moment) == "1250"
