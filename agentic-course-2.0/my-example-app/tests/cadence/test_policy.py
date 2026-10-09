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


class TestPairingStopsAtTheTick:
    """H2: a ticked task pairs only with matching activity logged **at or before** the tick.

    Anything logged after the tick belongs to the next touch. A tick with nothing to pair with
    must not move the anchor past activity nobody has credited yet.
    """

    def test_a_voicemail_logged_after_the_call_tick_closes_the_voicemail(self) -> None:
        """Reviewer's row 1: call ticked 10:00, voicemail logged as a call 10:05, email 10:30.
        Before the fix the call consumed the voicemail's call and the voicemail task re-fired."""
        logged = [
            activity(ActivityKind.call, later(5), "51230001"),
            activity(ActivityKind.email, later(30), "71250001"),
        ]

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, done(ANCHOR), logged, NOW)

        assert [(s.position.touch, s.signal.source) for s in plan.steps] == [
            (Touch.call, SignalSource.task_completed),
            (Touch.voicemail, SignalSource.activity),
            (Touch.email, SignalSource.activity),
        ]
        assert plan.next_position == CadencePosition(cycle=2, touch=Touch.call)

    def test_notes_logged_after_the_call_tick_close_the_touches_they_were_for(self) -> None:
        """Reviewer's row 2: call ticked 10:00, voicemail note 10:05, email note 10:30. Before
        the fix the email task was created for an email already sent."""
        logged = [
            activity(ActivityKind.note, later(5), "61240001"),
            activity(ActivityKind.note, later(30), "61240002"),
        ]

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, done(ANCHOR), logged, NOW)

        assert [step.position.touch for step in plan.steps] == [
            Touch.call,
            Touch.voicemail,
            Touch.email,
        ]
        assert plan.next_position == CadencePosition(cycle=2, touch=Touch.call)
        assert plan.anchor_ref == "notes:61240002"

    def test_a_late_tick_does_not_skip_an_email_logged_before_it(self) -> None:
        """Reviewer's row 3: the voicemail was never logged and its task was ticked on Thursday;
        the email was logged on Monday. The email must still close the email touch."""
        monday = ANCHOR + timedelta(hours=2)
        thursday = ANCHOR + timedelta(days=3)
        email = activity(ActivityKind.email, monday, "71250001")

        plan = plan_advance(
            CadencePosition(cycle=1, touch=Touch.voicemail),
            ANCHOR,
            "calls:51230001",
            done(thursday),
            [email],
            thursday + timedelta(hours=1),
        )

        assert [step.position.touch for step in plan.steps] == [Touch.voicemail, Touch.email]
        assert plan.next_position == CadencePosition(cycle=2, touch=Touch.call)
        assert plan.anchor_ref == email.ref

    def test_a_tick_pairs_with_an_activity_logged_at_the_same_instant(self) -> None:
        """The tie, toward done: logged at exactly the tick is the same act."""
        call = activity(ActivityKind.call, later(10))

        signal = find_signal(Touch.call, ANCHOR, None, done(later(10)), [call], NOW)

        assert signal is not None
        assert signal.source is SignalSource.both
        assert signal.anchor_ref == call.ref

    def test_a_tick_does_not_pair_with_an_activity_logged_after_it(self) -> None:
        call = activity(ActivityKind.call, later(11))

        signal = find_signal(Touch.call, ANCHOR, None, done(later(10)), [call], NOW)

        assert signal is not None
        assert signal.source is SignalSource.task_completed
        assert signal.done_at == later(10)

    def test_a_tick_alone_moves_the_anchor_no_further_than_the_first_uncredited_activity(
        self,
    ) -> None:
        email = activity(ActivityKind.email, later(5), "71250001")

        signal = find_signal(Touch.call, ANCHOR, None, done(later(60)), [email], NOW)

        assert signal is not None
        assert (signal.anchor_at, signal.anchor_ref) == (later(5), None)

    def test_a_tick_alone_with_nothing_logged_moves_the_anchor_to_the_tick(self) -> None:
        """So a call logged *afterwards*, back-dated to before the tick, is not credited to the
        voicemail on the next sync."""
        signal = find_signal(Touch.call, ANCHOR, None, done(later(60)), [], NOW)

        assert signal is not None
        assert (signal.anchor_at, signal.anchor_ref) == (later(60), None)

    def test_each_activity_still_closes_at_most_one_touch(self) -> None:
        """One note after a tick closes the voicemail, and nothing else."""
        note = activity(ActivityKind.note, later(5), "61240001")

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, done(ANCHOR), [note], NOW)

        assert [step.position.touch for step in plan.steps] == [Touch.call, Touch.voicemail]
        assert plan.next_position == CadencePosition(cycle=1, touch=Touch.email)


class TestFutureDatedActivity:
    """M2: a meeting's ``hs_timestamp`` is its start time, so booking one for next week must not
    close a touch today or drag the anchor into the future."""

    def test_a_meeting_booked_for_next_week_does_not_close_the_call(self) -> None:
        meeting = activity(ActivityKind.meeting, NOW + timedelta(days=8), "81260001")

        plan = plan_advance(CadencePosition.first(), ANCHOR, None, OPEN, [meeting], NOW)

        assert not plan.changed

    def test_a_future_activity_counts_once_it_is_in_the_past(self) -> None:
        meeting = activity(ActivityKind.meeting, NOW + timedelta(days=8), "81260001")
        then = NOW + timedelta(days=8, minutes=1)

        signal = find_signal(Touch.call, ANCHOR, None, OPEN, [meeting], then)

        assert signal is not None
        assert signal.anchor_ref == meeting.ref

    def test_a_future_activity_does_not_hold_back_a_tick_alone(self) -> None:
        meeting = activity(ActivityKind.meeting, NOW + timedelta(days=8), "81260001")

        signal = find_signal(Touch.voicemail, ANCHOR, None, done(later(30)), [meeting], NOW)

        assert signal is not None
        assert (signal.anchor_at, signal.anchor_ref) == (later(30), None)
