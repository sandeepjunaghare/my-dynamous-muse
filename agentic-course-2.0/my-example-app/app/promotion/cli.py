"""``lpe promotion`` — promote a finished run's prospects to HubSpot; read review decisions back.

* ``promote --run <id>`` creates each qualified, fully cited candidate as a Company + Contact at
  ``lpe_review_status = pending_review``. ``--dry-run`` makes the same reads (portal dedupe
  included) and writes nothing — run it first, and read it.
* ``sync`` reads each pending company's ``lpe_review_status`` back: **accepted** ones enter the
  cadence (its first task is created then), rejected ones are recorded and left alone. Meant to
  run daily from the 07:00 launchd job, **before** ``lpe cadence sync`` (OQ3-A).

Stdout is the review surface, so every line is a sentence a person can act on, with the record's
HubSpot URL when ``HUBSPOT_PORTAL_ID`` is set. Exit code 1 when anything failed; never a traceback.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CADENCE_TZ
from app.core.database import dispose_engine, get_sessionmaker
from app.promotion.client import aclose_hubspot_client, get_hubspot_client
from app.promotion.schemas import (
    DuplicateProspect,
    PromotedProspect,
    PromotionReport,
    ReviewDecision,
    ReviewOutcome,
    ReviewSyncReport,
)
from app.promotion.service import PromotionService


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``promote`` and ``sync`` to the ``promotion`` command's parser."""
    subcommands = parser.add_subparsers(dest="promotion_command", required=True)
    promote = subcommands.add_parser(
        "promote",
        help=(
            "create a finished run's qualified prospects in HubSpot at pending review; run with "
            "--dry-run first"
        ),
    )
    promote.add_argument("--run", type=UUID, required=True, help="a completed or degraded run id")
    promote.add_argument(
        "--dry-run",
        action="store_true",
        help="print what promoting would do, after the same HubSpot reads; write nothing",
    )
    sync = subcommands.add_parser(
        "sync",
        help=(
            "read review decisions back from HubSpot and enrol accepted prospects in the cadence — "
            "run daily, before `lpe cadence sync`"
        ),
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="print what the sync would do; enrol nobody and record nothing",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``promotion`` subcommand and return a process exit code."""
    subcommand: str = args.promotion_command
    dry_run: bool = args.dry_run
    if subcommand == "promote":
        run_id: UUID = args.run
        report = asyncio.run(
            _with_service(lambda service: service.promote_run(run_id, dry_run=dry_run))
        )
        return _emit(promote_lines(report), report.stopped, failed=report.counts["failed"])
    if subcommand == "sync":
        sync_report = asyncio.run(
            _with_service(lambda service: service.sync_reviews(dry_run=dry_run))
        )
        if sync_report.unrecognised:
            values = ", ".join(sync_report.unrecognised)
            print(
                f"warning: lpe_review_status value(s) nobody taught the engine: {values} — "
                "those companies are left waiting",
                file=sys.stderr,
            )
        return _emit(
            sync_lines(sync_report), sync_report.stopped, failed=sync_report.counts["failed"]
        )
    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


async def _with_service[T](operation: Callable[[PromotionService], Awaitable[T]]) -> T:
    """Run one operation, then close the pool and the HubSpot client — a CLI is not a service.

    ``get_hubspot_client`` is looked up at call time, so a test can patch this module.
    """
    try:
        async with get_sessionmaker()() as session:
            return await operation(_service(session))
    finally:
        await dispose_engine()
        await aclose_hubspot_client()


def _service(session: AsyncSession) -> PromotionService:
    return PromotionService(session, hubspot=get_hubspot_client)


def _emit(lines: list[str], stopped: str | None, *, failed: int) -> int:
    for line in lines:
        print(line)
    if stopped is not None:
        print(f"error: the pass stopped early — {stopped}", file=sys.stderr)
    return 1 if failed or stopped is not None else 0


def _record(url: str | None, object_id: str | None) -> str:
    return url or object_id or "-"


def _created_line(prospect: PromotedProspect, *, dry_run: bool) -> list[str]:
    verb = "would create" if dry_run else ("recovered" if prospect.recovered else "created")
    line = f"{verb}  {prospect.legal_name}"
    if not dry_run:
        line += (
            f"  company {_record(prospect.company_url, prospect.company_id)}"
            f"  contact {_record(prospect.contact_url, prospect.contact_id)}"
        )
    line += f"  route {prospect.route_label or '-'}"
    lines = [line]
    if prospect.name_ambiguous:
        lines.append(
            f"  ! name split uncertain: first={prospect.first_name or '-'} "
            f"last={prospect.last_name} — check it at review"
        )
    return lines


def _duplicate_line(duplicate: DuplicateProspect, *, dry_run: bool) -> str:
    verb = "would skip" if dry_run else "duplicate"
    if duplicate.duplicate_of is not None:
        reason = f"the same business as {duplicate.duplicate_of} in this vertical"
    else:
        reason = (
            "already in HubSpot: "
            f"{_record(duplicate.matched_company_url, duplicate.matched_company_id)}"
        )
    return f"{verb}  {duplicate.legal_name} — {reason}"


def promote_lines(report: PromotionReport) -> list[str]:
    """What ``promote`` prints: one line per outcome, then the counts and the field tally."""
    mode = "promote (dry run)" if report.dry_run else "promote"
    lines = [f"run {report.run_id}  {report.vertical}  {mode}"]
    for prospect in report.created:
        lines.extend(_created_line(prospect, dry_run=report.dry_run))
    lines.extend(_duplicate_line(dup, dry_run=report.dry_run) for dup in report.duplicates)
    lines.extend(f"failed  {failure.registry_id}: {failure.message}" for failure in report.failures)
    lines.extend(f"  {name:<28}{report.counts[name]:>8}" for name in report.counts)
    if report.missing_field_tally:
        lines.append("missing required fields:")
        lines.extend(
            f"  {name:<28}{count:>8}" for name, count in sorted(report.missing_field_tally.items())
        )
    return lines


def _decision_line(decision: ReviewDecision, *, dry_run: bool) -> str:
    company = _record(decision.company_url, decision.company_id)
    if decision.outcome is ReviewOutcome.enrolled:
        if dry_run:
            return f"would enrol  {decision.registry_id}  contact {decision.contact_id}"
        due = (
            ""
            if decision.first_due_at is None
            else f" — first call due {decision.first_due_at.astimezone(CADENCE_TZ).date()}"
        )
        orphan = (
            ""
            if decision.orphan_task_id is None
            else f" (task {decision.orphan_task_id} was left open — close it by hand)"
        )
        return f"enrolled  {decision.registry_id}  contact {decision.contact_id}{due}{orphan}"
    verb = decision.outcome.value if not dry_run else f"would mark {decision.outcome.value}"
    if decision.outcome is ReviewOutcome.withdrawn:
        return (
            f"{verb}  {decision.registry_id} — company {decision.company_id} is gone from HubSpot"
        )
    return f"{verb}  {decision.registry_id}  company {company}"


def sync_lines(report: ReviewSyncReport) -> list[str]:
    """What ``sync`` prints: one line per decision acted on, then the counts."""
    lines = ["promotion sync (dry run)" if report.dry_run else "promotion sync"]
    lines.extend(_decision_line(decision, dry_run=report.dry_run) for decision in report.decisions)
    lines.extend(f"failed  {failure.registry_id}: {failure.message}" for failure in report.failures)
    lines.extend(f"  {name:<28}{report.counts[name]:>8}" for name in report.counts)
    return lines
