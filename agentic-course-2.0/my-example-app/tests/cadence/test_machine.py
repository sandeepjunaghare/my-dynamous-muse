"""The state machine itself: the walk, the due dates, and what each task asks of a human."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.cadence.machine import (
    CADENCE_TZ,
    TOTAL_TOUCHES,
    CadencePosition,
    Touch,
    due_after,
    due_at_enrolment,
    end_of_local_day,
    local_date,
    next_position,
    task_for,
    task_key,
)
from app.promotion.schemas import TaskType


def _walk() -> list[CadencePosition]:
    positions = [CadencePosition.first()]
    while (following := next_position(positions[-1])) is not None:
        positions.append(following)
    return positions


class TestTheWalk:
    def test_three_cycles_of_call_voicemail_email_then_park(self) -> None:
        walk = [(p.cycle, p.touch) for p in _walk()]

        assert walk == [
            (1, Touch.call),
            (1, Touch.voicemail),
            (1, Touch.email),
            (2, Touch.call),
            (2, Touch.voicemail),
            (2, Touch.email),
            (3, Touch.call),
            (3, Touch.voicemail),
            (3, Touch.email),
        ]
        assert len(walk) == TOTAL_TOUCHES == 9

    def test_the_last_email_parks(self) -> None:
        assert next_position(CadencePosition(cycle=3, touch=Touch.email)) is None

    def test_ordinals_count_one_to_nine(self) -> None:
        assert [p.ordinal for p in _walk()] == list(range(1, 10))

    @pytest.mark.parametrize("cycle", [0, 4])
    def test_an_impossible_cycle_cannot_be_constructed(self, cycle: int) -> None:
        """T13 reconstructs positions from history; a fourth cycle must fail at construction."""
        with pytest.raises(ValidationError):
            CadencePosition(cycle=cycle, touch=Touch.call)


class TestDueDates:
    def test_end_of_local_day_is_2359_dallas_time(self) -> None:
        due = end_of_local_day(date(2026, 10, 5))

        assert due == datetime(2026, 10, 6, 4, 59, tzinfo=UTC)  # CDT is UTC-5
        assert due.astimezone(CADENCE_TZ).strftime("%H:%M") == "23:59"

    def test_the_first_call_is_due_the_day_of_enrolment(self) -> None:
        enrolled = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)

        assert local_date(due_at_enrolment(enrolled)) == date(2026, 10, 5)

    def test_voicemail_and_email_are_same_day(self) -> None:
        call_done = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)

        for touch in (Touch.voicemail, Touch.email):
            due = due_after(CadencePosition(cycle=1, touch=touch), call_done)
            assert local_date(due) == date(2026, 10, 5)

    def test_same_day_means_the_dallas_day_not_the_utc_day(self) -> None:
        """8 pm in Dallas is 1 am UTC tomorrow; a UTC date would push the email a day late."""
        evening = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)  # 2026-10-05 20:00 CDT

        due = due_after(CadencePosition(cycle=1, touch=Touch.email), evening)

        assert local_date(due) == date(2026, 10, 5)

    def test_the_next_cycle_waits_four_days_after_the_email(self) -> None:
        email_done = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)

        due = due_after(CadencePosition(cycle=2, touch=Touch.call), email_done)

        assert local_date(due) == date(2026, 10, 9)

    def test_the_wait_survives_the_end_of_daylight_saving(self) -> None:
        """DST ends 2026-11-01. The call is still due at 23:59 Dallas time, now UTC-6."""
        email_done = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)

        due = due_after(CadencePosition(cycle=2, touch=Touch.call), email_done)

        assert due == datetime(2026, 11, 4, 5, 59, tzinfo=UTC)
        assert due.astimezone(CADENCE_TZ).strftime("%Y-%m-%d %H:%M") == "2026-11-03 23:59"

    def test_a_naive_time_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            local_date(datetime(2026, 10, 5, 9, 0))


class TestTasks:
    DUE = datetime(2026, 10, 6, 4, 59, tzinfo=UTC)

    def test_call_and_voicemail_are_call_tasks_and_email_is_an_email_task(self) -> None:
        types = {
            touch: task_for(
                CadencePosition(cycle=1, touch=touch), self.DUE, None, key="k"
            ).task_type
            for touch in Touch
        }

        assert types == {
            Touch.call: TaskType.call,
            Touch.voicemail: TaskType.call,
            Touch.email: TaskType.email,
        }

    def test_the_email_touch_is_a_draft_a_human_sends(self) -> None:
        """Spike 4: nothing sends. The task says so, and carries no generated copy (E16)."""
        task = task_for(CadencePosition(cycle=2, touch=Touch.email), self.DUE, None, key="k")

        assert "send by hand" in task.subject
        assert task.body is not None
        assert task.body.startswith("DRAFT")
        assert "Nothing is sent automatically" in task.body

    @pytest.mark.parametrize("touch", list(Touch))
    def test_every_touch_carries_the_never_lead_with_ai_rule(self, touch: Touch) -> None:
        task = task_for(CadencePosition(cycle=1, touch=touch), self.DUE, None, key="k")

        assert task.body is not None
        assert "Never lead with AI" in task.body

    def test_the_subject_names_the_cycle_and_the_touch(self) -> None:
        task = task_for(CadencePosition(cycle=3, touch=Touch.voicemail), self.DUE, "77001", key="k")

        assert task.subject == "Cadence 3/3 · voicemail"
        assert task.owner_id == "77001"
        assert task.due_at == self.DUE

    def test_the_body_ends_with_the_idempotency_key(self) -> None:
        position = CadencePosition(cycle=2, touch=Touch.voicemail)
        key = task_key("400112233", position)

        task = task_for(position, self.DUE, None, key=key)

        assert key == "lpe-cadence:400112233:2-voicemail"
        assert task.body is not None
        assert task.body.endswith(f"Ref: {key}")
