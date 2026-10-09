"""``lpe cadence`` — run the sync and see what is overdue, from the terminal.

``sync`` is the same operation as ``POST /cadence/sync``, offered here because launchd can run a
command without a server being up. **It is meant to run daily** (at least), from an external
launchd job, separately from T10's weekly sourcing run; see ``app/cadence/README.md`` for the
exact command. ``overdue`` reads only our own schedule, so it works with no
HubSpot token at all.

There is deliberately **no** ``enrol`` subcommand: enrolling an existing contact by hand would
start it at touch one, and cycle position is never reset. See ``routes.py``.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.machine import CADENCE_TZ, CYCLES
from app.cadence.schemas import OverdueTouch, SyncReport
from app.cadence.service import CadenceService
from app.core.database import dispose_engine, get_sessionmaker
from app.promotion.client import aclose_hubspot_client, get_hubspot_client


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``sync`` and ``overdue`` to the ``cadence`` command's parser."""
    subcommands = parser.add_subparsers(dest="cadence_command", required=True)
    subcommands.add_parser(
        "sync",
        help=(
            "read outcomes from HubSpot, advance done touches, report what is overdue — run it at "
            "least daily (launchd); voicemail and email are same-day touches"
        ),
    )
    subcommands.add_parser("overdue", help="list live touches past their due date")


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``cadence`` subcommand and return a process exit code."""
    subcommand: str = args.cadence_command
    if subcommand == "sync":
        return asyncio.run(_run_sync())
    if subcommand == "overdue":
        return asyncio.run(_run_overdue())
    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


async def _with_service[T](operation: Callable[[CadenceService], Awaitable[T]]) -> T:
    """Run one operation, then close the pool and the HubSpot client — a CLI is not a service.

    ``get_hubspot_client`` is looked up here at call time rather than bound as a default, so a
    test can point the CLI at a mock portal by patching this module.
    """
    try:
        async with get_sessionmaker()() as session:
            return await operation(_service(session))
    finally:
        await dispose_engine()
        await aclose_hubspot_client()


def _service(session: AsyncSession) -> CadenceService:
    return CadenceService(session, hubspot=get_hubspot_client)


async def _run_sync() -> int:
    async def operation(service: CadenceService) -> SyncReport:
        return await service.sync()

    report = await _with_service(operation)
    if report.skipped:
        print("another cadence sync is running — skipped; it will do this run's work")
        return 0
    for warning in report.warnings:
        print(f"warning: {warning}")
    print(
        f"checked {report.checked} · touches closed {report.touches_closed} · "
        f"tasks created {report.tasks_created} · parked {report.parked} · "
        f"unchanged {report.unchanged}"
    )
    for task_id in report.pending_tasks_adopted:
        print(
            f"task {task_id}: an earlier sync's interrupted create — found and adopted, not redone"
        )
    for task_id in report.open_tasks_superseded:
        print(f"open task {task_id}: its touch was already logged — close it in HubSpot")
    for contact_id in report.missing_tasks:
        print(f"contact {contact_id}: its cadence task was deleted in HubSpot — not recreated")
    for failure in report.failures:
        print(f"contact {failure.hubspot_contact_id}: failed ({failure.code}) — {failure.error}")
    _print_overdue(report.overdue)
    return 1 if report.failures else 0


async def _run_overdue() -> int:
    async def operation(service: CadenceService) -> list[OverdueTouch]:
        return await service.overdue()

    _print_overdue(await _with_service(operation))
    return 0


def _print_overdue(overdue: list[OverdueTouch]) -> None:
    if not overdue:
        print("nothing overdue")
        return
    print(f"{len(overdue)} overdue:")
    print(f"  {'contact':<14} {'touch':<22} {'due (Dallas)':<17} {'late':>5}  task")
    for touch in overdue:
        due = touch.due_at.astimezone(CADENCE_TZ).strftime("%Y-%m-%d %H:%M")
        label = f"{touch.cycle}/{CYCLES} {touch.touch.value}"
        print(
            f"  {touch.hubspot_contact_id:<14} {label:<22} {due:<17} "
            f"{touch.days_overdue:>4}d  {touch.hubspot_task_id or '-'}"
        )
