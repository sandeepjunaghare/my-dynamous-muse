"""``lpe runs``: what a person reads, and the exit codes.

The services are stubbed: what they do is ``test_service`` and ``test_report``'s business. These
tests pin the surface: the lines, the warnings on stderr, and 0 / 1.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.cli import main
from app.core.cost import BillableKind
from app.promotion.schemas import PROMOTE_COUNTS, ReviewTally
from app.runs.exceptions import WeeklyRunInProgressError, WeeklyRunNotFoundError
from app.runs.report import WeeklyReportService
from app.runs.schemas import (
    PlaybookNumber,
    RunSummary,
    WeeklyReport,
    WeeklyRunRequest,
    WeeklyRunResponse,
    WeeklyRunStatus,
)
from app.runs.service import WeeklyRunService
from app.sourcing.schemas import RunCostSummary, RunStatus, SourcingRunResponse

RUN = UUID("4d3c2b1a-0000-4000-8000-000000000001")
SOURCING = UUID("4d3c2b1a-0000-4000-8000-000000000002")
REQUESTED = datetime(2026, 10, 19, 11, 0, tzinfo=UTC)


def _sourcing() -> SourcingRunResponse:
    return SourcingRunResponse.model_validate(
        {
            "id": SOURCING,
            "manifest_id": SOURCING,
            "vertical": "freight",
            "geography": "dfw",
            "icp_band": {
                "headcount_min": 5,
                "headcount_max": 50,
                "requires_office_function": False,
            },
            "status": RunStatus.completed,
            "status_detail": None,
            "started_at": REQUESTED,
            "finished_at": REQUESTED,
            "counts": {"batched": 150, "places_verified": 97},
            "cost": RunCostSummary(
                calls={BillableKind.places_lookup: 120},
                usd={BillableKind.places_lookup: Decimal("3.84")},
            ),
        }
    )


def _run(
    status: WeeklyRunStatus = WeeklyRunStatus.completed, **overrides: object
) -> WeeklyRunResponse:
    values: dict[str, object] = {
        "id": RUN,
        "vertical": "freight",
        "status": status,
        "status_detail": None,
        "dry_run": False,
        "batch_size": None,
        "allow_degraded": False,
        "requested_at": REQUESTED,
        "sourcing_run_id": SOURCING,
        "sourced_at": REQUESTED + timedelta(minutes=30),
        "promoted_at": REQUESTED + timedelta(minutes=41),
        "finished_at": REQUESTED + timedelta(minutes=41),
        "promotion_counts": {**{name: 0 for name in PROMOTE_COUNTS}, "created": 12},
        "promotion_stopped": None,
        "sourcing": _sourcing(),
    }
    values.update(overrides)
    return WeeklyRunResponse.model_validate(values)


def _last_err(captured: pytest.CaptureFixture[str]) -> str:
    """The last stderr line: the CLI's own message, after any log lines (they go to stderr too)."""
    return captured.readouterr().err.splitlines()[-1]


def _stub_run(monkeypatch: pytest.MonkeyPatch, outcome: WeeklyRunResponse | Exception) -> None:
    async def run(self: WeeklyRunService, request: WeeklyRunRequest) -> WeeklyRunResponse:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(WeeklyRunService, "run", run)


def _stub_get(monkeypatch: pytest.MonkeyPatch, outcome: WeeklyRunResponse | Exception) -> None:
    async def get(self: WeeklyRunService, run_id: UUID) -> WeeklyRunResponse:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(WeeklyRunService, "get", get)


def _report(runs: tuple[RunSummary, ...] = ()) -> WeeklyReport:
    return WeeklyReport(
        week_start=date(2026, 10, 19),
        week_end=date(2026, 10, 25),
        timezone="America/Chicago",
        playbook=(
            PlaybookNumber(name="doors knocked", value=4, configured=True),
            PlaybookNumber(name="owner conversations", value=1, configured=True),
            PlaybookNumber(name="assessments booked", value=0, configured=True),
            PlaybookNumber(name="paid assessments sold", value=None, configured=False),
        ),
        unclassified_touches=3,
        runs=runs,
        target_minutes=60,
    )


def _stub_report(monkeypatch: pytest.MonkeyPatch, report: WeeklyReport) -> list[date | None]:
    weeks: list[date | None] = []

    async def build(self: WeeklyReportService, week_of: date | None = None) -> WeeklyReport:
        weeks.append(week_of)
        return report

    monkeypatch.setattr(WeeklyReportService, "build", build)
    return weeks


class TestStart:
    def test_a_completed_run_prints_m7_cost_and_counters(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_run(monkeypatch, _run())

        assert main(["runs", "start", "--vertical", "freight"]) == 0

        out = capsys.readouterr()
        lines = out.out.splitlines()
        assert lines[0] == f"weekly run {RUN}  freight  completed"
        assert lines[1] == "  brief → HubSpot               41 min"
        assert "  cost                           $3.84" in lines
        assert "    batched                        150" in lines
        assert "    routing_clustered                -" in lines
        assert "    created                         12" in lines
        assert "warning" not in out.err and "error:" not in out.err

    def test_the_options_reach_the_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[WeeklyRunRequest] = []

        async def run(self: WeeklyRunService, request: WeeklyRunRequest) -> WeeklyRunResponse:
            seen.append(request)
            return _run(dry_run=True)

        monkeypatch.setattr(WeeklyRunService, "run", run)

        assert (
            main(
                [
                    "runs",
                    "start",
                    "--vertical",
                    "freight",
                    "--batch-size",
                    "5",
                    "--allow-degraded",
                    "--dry-run",
                ]
            )
            == 0
        )
        assert seen == [
            WeeklyRunRequest(vertical="freight", batch_size=5, allow_degraded=True, dry_run=True)
        ]

    def test_a_dry_run_says_so(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_run(monkeypatch, _run(dry_run=True))

        main(["runs", "start", "--vertical", "freight", "--dry-run"])

        out = capsys.readouterr().out
        assert out.splitlines()[0].endswith("completed  DRY RUN")
        assert "  promotion  (dry run: nothing written)" in out

    def test_degraded_is_0_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_run(
            monkeypatch, _run(WeeklyRunStatus.degraded, status_detail="2 promotion(s) failed")
        )

        assert main(["runs", "start", "--vertical", "freight"]) == 0

        assert _last_err(capsys) == "warning: 2 promotion(s) failed"

    def test_failed_is_1_with_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_run(
            monkeypatch,
            _run(
                WeeklyRunStatus.failed,
                status_detail="sourcing failed: RuntimeError: places is down",
                sourced_at=None,
                promoted_at=None,
                promotion_counts={},
            ),
        )

        assert main(["runs", "start", "--vertical", "freight"]) == 1

        out = capsys.readouterr()
        assert "  brief → HubSpot             not reached" in out.out
        assert "promotion" not in out.out
        assert out.err.splitlines()[-1] == (
            "error: the weekly run failed — sourcing failed: RuntimeError: places is down"
        )

    def test_a_refusal_is_1_and_one_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_run(monkeypatch, WeeklyRunInProgressError(f"weekly run {RUN} has been running"))

        assert main(["runs", "start", "--vertical", "freight"]) == 1

        assert _last_err(capsys) == f"error: weekly run {RUN} has been running"


class TestShow:
    def test_it_renders_like_start(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_get(monkeypatch, _run())

        assert main(["runs", "show", str(RUN)]) == 0

        assert capsys.readouterr().out.splitlines()[0] == f"weekly run {RUN}  freight  completed"

    def test_an_unknown_id(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _stub_get(monkeypatch, WeeklyRunNotFoundError(f"no weekly run {RUN}"))

        assert main(["runs", "show", str(RUN)]) == 1

        assert _last_err(capsys) == f"error: no weekly run {RUN}"


class TestReport:
    def test_the_numbers_first_with_paid_sold_not_configured(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        weeks = _stub_report(monkeypatch, _report())

        assert main(["runs", "report", "--week", "2026-10-21"]) == 0

        lines = capsys.readouterr().out.splitlines()
        assert weeks == [date(2026, 10, 21)]
        assert lines[0] == "Week of Mon 19 Oct to Sun 25 Oct 2026 (America/Chicago)"
        assert lines[2:6] == [
            "  doors knocked               4",
            "  owner conversations         1",
            "  assessments booked          0",
            "  paid assessments sold       not configured (no $999 assessment pipeline, GATE-A)",
        ]
        assert lines[7] == (
            '  unclassified touches: 3 (log as "Door knock — …", "Owner conversation — …", '
            '"Assessment booked — …" to count them)'
        )
        assert lines[-1] == "No weekly run this week."

    def test_each_run_with_its_target_and_review(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        summary = RunSummary(
            run=_run(),
            within_target=True,
            total_usd=Decimal("3.84"),
            funnel={"batched": 150},
            promotion={"created": 12},
            review=ReviewTally(pending=7, accepted=4, rejected=1),
        )
        _stub_report(monkeypatch, _report((summary,)))

        assert main(["runs", "report"]) == 0

        out = capsys.readouterr().out
        assert "  brief → HubSpot               41 min (target 60)" in out
        assert "  review    accepted 4  rejected 1  pending 7  withdrawn 0" in out

    def test_a_bad_week_is_a_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exited:
            main(["runs", "report", "--week", "last friday"])

        assert exited.value.code == 2
        assert "is not a date" in capsys.readouterr().err
