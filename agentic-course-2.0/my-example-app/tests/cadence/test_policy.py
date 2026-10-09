"""The D5 done-policy, as pure functions — no database, no portal.

A touch is done when its task is complete **or** a matching activity was logged after the touch
became current; ties break toward done; each activity closes at most one touch.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.cadence.machine import CadencePosition, Touch
from app.cadence.schemas import Activity, ActivityKind, SignalSource, TaskSnapshot
from app.cadence.sync import find_signal, plan_advance

ANCHOR = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
NOW = ANCHOR + timedelta(hours=6)

OPEN = TaskSnapshot(task_id="88000001", completed=False, completed_at=None)


def done(at: datetime) -> TaskSnapshot:
    return TaskSnapshot(task_id="88000001", completed=True, completed_at=at)


def activity(kind: ActivityKind, at: datetime, activity_id: str = "51230001") -> Activity:
    return Activity(kind=kind, activity_id=activity_id, occurred_at=at)


def later(minutes: int) -> datetime:
    return ANCHOR + timedelta(minutes=minutes)


class TestFindSignal:
    def test_nothing_happened_means_not_done(self) -> None:
        assert find_signal(Touch.call, ANCHOR, None, OPEN, [], NOW) is None

    def test_a_completed_task_is_done(self) -> None:
        signal = find_signal(Touch.call, ANCHOR, None, done(later(30)), [], NOW)

        assert signal is not None
        assert signal.source is SignalSource.task_completed
        assert signal.done_at == later(30)
        assert (signal.anchor_at, signal.anchor_ref) == (later(30), None)

    def test_a_matching_activity_closes_an_open_task(self) -> None:
        """The OR half of D5 — the founder logged the call and never ticked the task."""
        call = activity(ActivityKind.call, later(20))

        signal = find_signal(Touch.call, ANCHOR, None, OPEN, [call], NOW)

        assert signal is not None
        assert signal.source is SignalSource.activity
        assert signal.done_at == later(20)
        assert signal.anchor_ref == call.ref

    def test_an_activity_at_exactly_the_anchor_counts(self) -> None:
        """The tie, broken toward done."""
        call = activity(ActivityKind.call, ANCHOR)

        assert find_signal(Touch.call, ANCHOR, None, OPEN, [call], NOW) is not None

    def test_an_activity_before_the_anchor_does_not_count(self) -> None:
        call = activity(ActivityKind.call, ANCHOR - timedelta(seconds=1))

        assert find_signal(Touch.call, ANCHOR, None, OPEN, [call], NOW) is None

    def test_the_activity_that_set_the_anchor_is_never_credited_twice(self) -> None:
        call = activity(ActivityKind.call, ANCHOR)

        assert find_signal(Touch.voicemail, ANCHOR, call.ref, None, [call], NOW) is None

    def test_a_second_activity_at_the_same_instant_still_counts(self) -> None:
        """Two calls logged in the same millisecond are two calls, ordered by ref."""
        first = activity(ActivityKind.call, ANCHOR, "51230001")
        second = activity(ActivityKind.call, ANCHOR, "51230002")

        signal = find_signal(Touch.voicemail, ANCHOR, first.ref, None, [first, second], NOW)

        assert signal is not None
        assert signal.anchor_ref == second.ref

    @pytest.mark.parametrize(
        ("touch", "kind", "counts"),
        [
            (Touch.call, ActivityKind.call, True),
            (Touch.call, ActivityKind.meeting, True),
            (Touch.call, ActivityKind.note, True),
            (Touch.call, ActivityKind.email, False),
            (Touch.voicemail, ActivityKind.call, True),
            (Touch.voicemail, ActivityKind.note, True),
            (Touch.voicemail, ActivityKind.email, False),
            (Touch.voicemail, ActivityKind.meeting, False),
            (Touch.email, ActivityKind.email, True),
            (Touch.email, ActivityKind.note, True),
            (Touch.email, ActivityKind.call, False),
            (Touch.email, ActivityKind.meeting, False),
        ],
    )
    def test_which_activity_matches_which_touch(
        self, touch: Touch, kind: ActivityKind, counts: bool
    ) -> None:
        signal = find_signal(touch, ANCHOR, None, OPEN, [activity(kind, later(5))], NOW)

        assert (signal is not None) is counts

    def test_a_completed_task_and_its_logged_call_are_one_act(self) -> None:
        """Ticking the task and logging the call must not close two touches between them."""
        call = activity(ActivityKind.call, later(10))

        signal = find_signal(Touch.call, ANCHOR, None, done(later(15)), [call], NOW)

        assert signal is not None
        assert signal.source is SignalSource.both
        assert signal.done_at == later(10)
        assert signal.anchor_ref == call.ref

    def test_the_earliest_matching_activity_is_credited(self) -> None:
        early = activity(ActivityKind.note, later(5), "61240001")
        late = activity(ActivityKind.call, later(50), "51230001")

        signal = find_signal(Touch.call, ANCHOR, None, OPEN, [late, early], NOW)

        assert signal is not None
        assert signal.anchor_ref == early.ref

    def test_a_completion_stamped_before_the_anchor_does_not_move_it_back(self) -> None:
        """Clock skew must not re-open activity already credited to an earlier touch."""
        signal = find_signal(
            Touch.voicemail, ANCHOR, "calls:1", done(ANCHOR - timedelta(minutes=1)), [], NOW
        )

        assert signal is not None
        assert (signal.anchor_at, signal.anchor_ref) == (ANCHOR, "calls:1")

    def test_a_completed_task_without_a_completion_date_is_done_now(self) -> None:
        task = TaskSnapshot(task_id="88000001", completed=True, completed_at=None)

        signal = find_signal(Touch.call, ANCHOR, None, task, [], NOW)

        assert signal is not None
        assert signal.done_at == NOW


class TestPlanAdvance:
    def test_nothing_done_changes_nothing(self) -> None:
        plan = plan_advance(CadencePosition.first(), ANCHOR, None, OPEN, [], NOW)

        assert not plan.changed
        assert plan.next_position is None

    def test_a_completed_call_moves_to_the_voicemail_due_the_same_day(self) -> None:
        plan = plan_advance(CadencePosition.first(), ANCHOR, None, done(later(30)), [], NOW)

        assert plan.next_position == CadencePosition(cycle=1, touch=Touch.voicemail)
        assert plan.next_due_at == datetime(2026, 10, 6, 4, 59, tzinfo=UTC)
        assert len(plan.steps) == 1

    def test_work_already_done_by_hand_advances_rather_than_re_firing(self) -> None:
        """Call, voicemail note and email all logged before the sync ran: three touches close,
        and the only task to create is cycle two's call, four days after the email."""
        logged = [
            activity(ActivityKind.call, later(10), "51230001"),
            activity(ActivityKind.note, later(12), "61240001"),
            activity(ActivityKind.email, later(40), "71250001"),
        ]

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, OPEN, logged, NOW)

        assert [step.position.touch for step in plan.steps] == [
            Touch.call,
            Touch.voicemail,
            Touch.email,
        ]
        assert plan.next_position == CadencePosition(cycle=2, touch=Touch.call)
        assert plan.next_due_at == datetime(2026, 10, 10, 4, 59, tzinfo=UTC)  # 10-09 Dallas
        assert plan.anchor_ref == "emails:71250001"

    def test_one_logged_call_closes_one_touch(self) -> None:
        plan = plan_advance(
            CadencePosition.first(),
            ANCHOR,
            None,
            OPEN,
            [activity(ActivityKind.call, later(10))],
            NOW,
        )

        assert len(plan.steps) == 1
        assert plan.next_position == CadencePosition(cycle=1, touch=Touch.voicemail)

    def test_the_last_touch_closing_parks(self) -> None:
        plan = plan_advance(
            CadencePosition(cycle=3, touch=Touch.email), ANCHOR, None, done(later(5)), [], NOW
        )

        assert plan.parks
        assert plan.next_position is None
        assert plan.next_due_at is None

    def test_the_walk_cannot_run_past_park_however_much_was_logged(self) -> None:
        logged = [
            activity(ActivityKind.note, later(minute), f"6124{minute:04d}")
            for minute in range(1, 30)
        ]

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, OPEN, logged, NOW)

        assert len(plan.steps) == 9
        assert plan.parks
