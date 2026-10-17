"""``lpe runs`` — the weekly run, in-process, and the Friday report.

* ``start --vertical V`` runs the whole week's work in this process: source, qualify, verify,
  enrich, route, then promote to review. It is what launchd calls weekly, and the same service as
  ``POST /runs``. ``--dry-run`` sources for real and promotes as a dry run (no HubSpot write).
* ``show <id>`` prints one weekly run as ``start`` does.
* ``report [--week YYYY-MM-DD]`` prints the Friday report: the playbook's four numbers, then each
  of the week's runs.

Stdout is for a person: sentences, aligned counters, never a bare id. Warnings and errors go to
stderr. Exit code 0 for a completed or degraded run (with a ``warning:`` for degraded); 1 for a
failed run or a refusal (``app/cli.py`` turns a refusal into one ``error:`` line).
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable
from datetime import date
from uuid import UUID

from app.core.cost import format_usd
from app.core.database import dispose_engine, get_sessionmaker
from app.promotion.client import aclose_hubspot_client, get_hubspot_client
from app.runs.report import WeeklyReportService
from app.runs.schemas import (
    FUNNEL_COUNTERS,
    PROMOTION_COUNTERS,
    PlaybookNumber,
    RunSummary,
    WeeklyReport,
    WeeklyRunRequest,
    WeeklyRunResponse,
    WeeklyRunStatus,
)
from app.runs.service import WeeklyRunService

_PREFIX_HINT = (
    'log as "Door knock — …", "Owner conversation — …", "Assessment booked — …" to count them'
)
_NOT_CONFIGURED = "not configured (no $999 assessment pipeline, GATE-A)"


def _positive(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a date (YYYY-MM-DD)") from exc


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``start``, ``show`` and ``report`` to the ``runs`` command's parser."""
    subcommands = parser.add_subparsers(dest="runs_command", required=True)
    start = subcommands.add_parser(
        "start",
        help=(
            "run the week's work for a vertical in this process: source, verify, qualify, route, "
            "and promote to pending review in HubSpot"
        ),
    )
    start.add_argument("--vertical", required=True, help="a vertical with an ACTIVE manifest")
    start.add_argument(
        "--batch-size",
        type=_positive,
        metavar="N",
        help="candidates to take from the backlog (default SOURCING_BATCH_SIZE, 150)",
    )
    start.add_argument(
        "--allow-degraded",
        action="store_true",
        help=(
            "go on when a declared source cannot run; the batch is used up without that evidence "
            "and is never looked up again"
        ),
    )
    start.add_argument(
        "--dry-run",
        action="store_true",
        help="source for real, then promote as a dry run: the same reads, no HubSpot write",
    )
    show = subcommands.add_parser("show", help="print one weekly run")
    show.add_argument("run_id", type=UUID, help="a weekly run id")
    report = subcommands.add_parser(
        "report", help="the Friday report: the playbook's four numbers and the week's runs"
    )
    report.add_argument(
        "--week",
        type=_day,
        metavar="YYYY-MM-DD",
        help="any date in the week to report (Monday to Sunday, Dallas time); default this week",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``runs`` subcommand and return a process exit code."""
    subcommand: str = args.runs_command
    if subcommand == "start":
        request = WeeklyRunRequest(
            vertical=args.vertical,
            batch_size=args.batch_size,
            allow_degraded=args.allow_degraded,
            dry_run=args.dry_run,
        )
        return _emit(asyncio.run(_closing(_service().run(request))))
    if subcommand == "show":
        run_id: UUID = args.run_id
        return _emit(asyncio.run(_closing(_service().get(run_id))))
    if subcommand == "report":
        week: date | None = args.week
        for line in report_lines(asyncio.run(_closing(_report(week)))):
            print(line)
        return 0
    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


def _service() -> WeeklyRunService:
    return WeeklyRunService(get_sessionmaker(), hubspot=get_hubspot_client)


async def _report(week: date | None) -> WeeklyReport:
    async with get_sessionmaker()() as session:
        return await WeeklyReportService(session, hubspot=get_hubspot_client).build(week)


async def _closing[T](operation: Awaitable[T]) -> T:
    """Await one operation, then close the pool and the HubSpot client: a CLI is not a service."""
    try:
        return await operation
    finally:
        await dispose_engine()
        await aclose_hubspot_client()


def _emit(run: WeeklyRunResponse) -> int:
    for line in run_lines(run):
        print(line)
    if run.status is WeeklyRunStatus.failed:
        print(f"error: the weekly run failed — {run.status_detail}", file=sys.stderr)
        return 1
    if run.status is WeeklyRunStatus.degraded:
        print(f"warning: {run.status_detail}", file=sys.stderr)
    return 0


def _count(value: int | None) -> str:
    return "-" if value is None else str(value)


def _m7(run: WeeklyRunResponse, target: int | None) -> str:
    minutes = run.brief_to_hubspot_minutes
    suffix = "" if target is None else f" (target {target})"
    if minutes is None:
        return f"  {'brief → HubSpot':<28}{'not reached':>8}{suffix}"
    return f"  {'brief → HubSpot':<28}{f'{minutes:.0f} min':>8}{suffix}"


def run_lines(run: WeeklyRunResponse, *, target: int | None = None) -> list[str]:
    """One weekly run: its line, M7, cost, the funnel and promotion's counters."""
    head = f"weekly run {run.id}  {run.vertical}  {run.status.value}"
    lines = [head + ("  DRY RUN" if run.dry_run else ""), _m7(run, target)]
    sourcing = run.sourcing
    if sourcing is not None:
        lines.append(f"  {'cost':<28}{format_usd(sourcing.cost.total_usd()):>8}")
        lines.append("  funnel")
        lines.extend(
            f"    {name:<26}{_count(sourcing.counts.get(name)):>8}" for name in FUNNEL_COUNTERS
        )
    if run.promoted_at is not None:
        lines.append("  promotion" + ("  (dry run: nothing written)" if run.dry_run else ""))
        lines.extend(
            f"    {name:<26}{run.promotion_counts.get(name, 0):>8}" for name in PROMOTION_COUNTERS
        )
    return lines


def _playbook_line(number: PlaybookNumber) -> str:
    shown = _NOT_CONFIGURED if number.value is None else str(number.value)
    return f"  {number.name:<28}{shown}"


def _summary_lines(summary: RunSummary, target: int) -> list[str]:
    lines = run_lines(summary.run, target=target)
    review = summary.review
    lines.append(
        f"  review    accepted {review.accepted}  rejected {review.rejected}  "
        f"pending {review.pending}  withdrawn {review.withdrawn}"
    )
    return lines


def report_lines(report: WeeklyReport) -> list[str]:
    """The Friday report as a person reads it: the numbers first, then each run."""
    lines = [
        f"Week of {report.week_start:%a %d %b} to {report.week_end:%a %d %b %Y} "
        f"({report.timezone})",
        "",
        *(_playbook_line(number) for number in report.playbook),
        "",
        f"  unclassified touches: {report.unclassified_touches} ({_PREFIX_HINT})",
        "",
    ]
    if not report.runs:
        lines.append("No weekly run this week.")
        return lines
    for summary in report.runs:
        lines.extend(_summary_lines(summary, report.target_minutes))
        lines.append("")
    return lines[:-1]
