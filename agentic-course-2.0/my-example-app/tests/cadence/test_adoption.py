"""T13's pure core: the roster, the position syntax, and the reconstruction. No database, no portal.

Reconstruction is :func:`~app.cadence.sync.plan_advance` run from touch one over the contact's
history, so these tests pin the *mapping* — what each kind of history becomes as a starting row —
and leave the done-policy itself to ``test_policy.py``.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app.cadence.adoption import load_roster, parse_position, reconstruct
from app.cadence.exceptions import RosterError
from app.cadence.machine import CadencePosition, Touch, due_at_enrolment, end_of_local_day
from app.cadence.schemas import Activity, ActivityKind, RosterAction
from tests.cadence.conftest import START

SINCE = START - timedelta(days=20)
"""When the contact was created in HubSpot: evidence counts from here."""


def _activity(kind: ActivityKind, at_days: float, activity_id: str) -> Activity:
    return Activity(kind=kind, activity_id=activity_id, occurred_at=SINCE + timedelta(days=at_days))


class TestParsePosition:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1-call", CadencePosition(cycle=1, touch=Touch.call)),
            ("3-email", CadencePosition(cycle=3, touch=Touch.email)),
            (" 2-voicemail ", CadencePosition(cycle=2, touch=Touch.voicemail)),
        ],
    )
    def test_the_task_key_suffix_is_a_position(self, text: str, expected: CadencePosition) -> None:
        assert parse_position(text) == expected

    @pytest.mark.parametrize("text", ["0-call", "4-call", "2-door", "call", "1call", "", "1-Call"])
    def test_anything_else_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="position"):
            parse_position(text)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "adopt.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestRoster:
    def test_a_valid_roster_parses(self, tmp_path: Path) -> None:
        roster = load_roster(
            _write(
                tmp_path,
                """
                [[prospect]]
                contact = "552902851267"
                action = "adopt"

                [[prospect]]
                contact = 552902851268
                action = "adopt"
                start = "2-call"
                company = "346975458005"

                [[prospect]]
                contact = "552902851269"
                action = "park"
                """,
            )
        )

        assert [entry.contact for entry in roster.prospect] == [
            "552902851267",
            "552902851268",
            "552902851269",
        ], "an unquoted id is read as the same id"
        assert [entry.action for entry in roster.prospect] == [
            RosterAction.adopt,
            RosterAction.adopt,
            RosterAction.park,
        ]
        second = roster.prospect[1]
        assert second.start_position == CadencePosition(cycle=2, touch=Touch.call)
        assert second.company == "346975458005"
        assert roster.prospect[0].start_position is None

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ('[[prospect]]\ncontact = "1"\naction = "adopt"\nreason = "x"\n', "reason"),
            (
                '[[prospect]]\ncontact = "1"\naction = "adopt"\n'
                '[[prospect]]\ncontact = "1"\naction = "park"\n',
                "contact 1 appears more than once",
            ),
            ('[[prospect]]\ncontact = "1"\naction = "park"\nstart = "2-call"\n', "start"),
            ("prospect = []\n", "prospect"),
            ("", "prospect"),
            ('[[prospect]]\ncontact = "Jorge"\naction = "adopt"\n', "digits"),
            ('[[prospect]]\ncontact = "1"\naction = "enrol"\n', "action"),
            ('[[prospect]]\ncontact = "1"\naction = "adopt"\nstart = "4-call"\n', "position"),
            ('[[prospect]\ncontact = "1"\n', "not valid TOML"),
        ],
        ids=[
            "unknown-key",
            "duplicate-contact",
            "start-on-park",
            "empty",
            "no-prospects",
            "non-digit-contact",
            "unknown-action",
            "impossible-start",
            "malformed-toml",
        ],
    )
    def test_a_bad_roster_is_one_error_naming_the_file(
        self, tmp_path: Path, text: str, fragment: str
    ) -> None:
        path = _write(tmp_path, text)

        with pytest.raises(RosterError) as raised:
            load_roster(path)

        assert str(path) in raised.value.message
        assert fragment in raised.value.message
        assert raised.value.code == "invalid_roster"

    def test_the_failing_entry_is_named_by_its_number(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            '[[prospect]]\ncontact = "1"\naction = "adopt"\n'
            '[[prospect]]\ncontact = "2"\naction = "park"\nstart = "1-call"\n',
        )

        with pytest.raises(RosterError, match="prospect 2"):
            load_roster(path)

    def test_a_missing_file_is_a_roster_error(self, tmp_path: Path) -> None:
        path = tmp_path / "nowhere.toml"

        with pytest.raises(RosterError) as raised:
            load_roster(path)

        assert str(path) in raised.value.message


class TestReconstruct:
    def test_no_history_starts_at_the_first_call_due_today(self) -> None:
        result = reconstruct([], since=SINCE, now=START, override=None)

        assert result.start == CadencePosition.first()
        assert result.due_at == due_at_enrolment(START)
        assert (result.anchor_at, result.anchor_ref) == (SINCE, None)
        assert result.steps == ()
        assert (result.finished, result.overridden) == (False, False)

    def test_two_logged_calls_resume_at_the_email(self) -> None:
        """The ticket's test: touched twice by hand, the machine resumes at touch three."""
        history = [
            _activity(ActivityKind.call, 2, "1001"),
            _activity(ActivityKind.call, 2.01, "1002"),
        ]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert result.start == CadencePosition(cycle=1, touch=Touch.email)
        assert [step.position.touch for step in result.steps] == [Touch.call, Touch.voicemail]
        assert result.due_at == end_of_local_day(START.date()), "same-day, floored to today"

    def test_three_notes_resume_at_the_next_cycle_due_no_earlier_than_today(self) -> None:
        history = [_activity(ActivityKind.note, 1 + n / 100, f"200{n}") for n in range(3)]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert result.start == CadencePosition(cycle=2, touch=Touch.call)
        assert result.due_at == due_at_enrolment(START), "four days after day 1 is long past"

    def test_the_anchor_is_the_last_credited_activity(self) -> None:
        last = _activity(ActivityKind.note, 3, "3002")
        history = [_activity(ActivityKind.note, 2, "3001"), last]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert (result.anchor_at, result.anchor_ref) == (last.occurred_at, "notes:3002")

    def test_activity_before_the_contact_existed_is_not_evidence(self) -> None:
        history = [_activity(ActivityKind.call, -1, "4001")]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert result.start == CadencePosition.first()

    def test_an_activity_at_exactly_creation_counts(self) -> None:
        """The anchor ``(since, None)`` sorts below every ref: D5's tie, toward done."""
        history = [_activity(ActivityKind.call, 0, "4002")]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert result.start == CadencePosition(cycle=1, touch=Touch.voicemail)

    def test_a_booked_meeting_is_not_yet_evidence(self) -> None:
        future = Activity(
            kind=ActivityKind.meeting,
            activity_id="5001",
            occurred_at=START + timedelta(days=2),
        )

        result = reconstruct([future], since=SINCE, now=START, override=None)

        assert result.start == CadencePosition.first()

    def test_nine_touches_logged_finishes_the_cadence(self) -> None:
        history = [_activity(ActivityKind.note, n, f"600{n}") for n in range(9)]

        result = reconstruct(history, since=SINCE, now=START, override=None)

        assert result.finished
        assert (result.start, result.due_at) == (None, None)
        assert len(result.steps) == 9
        assert result.anchor_ref == "notes:6008"

    def test_an_override_starts_from_now_and_still_shows_the_evidence(self) -> None:
        """The roster says "from here, from now": what was logged is shown, not counted."""
        history = [_activity(ActivityKind.call, 2, "7001")]
        override = CadencePosition(cycle=2, touch=Touch.call)

        result = reconstruct(history, since=SINCE, now=START, override=override)

        assert result.start == override
        assert result.due_at == due_at_enrolment(START)
        assert (result.anchor_at, result.anchor_ref) == (START, None)
        assert [step.position.touch for step in result.steps] == [Touch.call]
        assert (result.overridden, result.finished) == (True, False)
