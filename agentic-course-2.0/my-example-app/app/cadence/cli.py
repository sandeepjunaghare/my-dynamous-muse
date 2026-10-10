"""``lpe cadence`` — run the sync, rehearse it, adopt or park prospects, and see what is overdue.

``sync`` is the same operation as ``POST /cadence/sync``, offered here because launchd can run a
command without a server being up. **It is meant to run daily** (at least), from an external
launchd job, separately from T10's weekly sourcing run; see ``app/cadence/README.md`` for the
exact command. ``sync --dry-run`` makes the same reads and decisions and applies none of them.
``park`` and ``overdue`` read and write only our own schedule, so they work with no HubSpot token
at all.

There is deliberately **no** ``enrol`` subcommand: enrolling an existing contact by hand would
start it at touch one, and cycle position is never reset. See ``routes.py``. ``adopt`` (T13) is
the one exception, and only because it never starts anyone at touch one by default: it
**reconstructs** each contact's position from its logged history, after a dry run a person has
read, from a roster a person wrote.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.cadence.adoption import Adopter, load_roster
from app.cadence.machine import CADENCE_TZ, CYCLES, CadencePosition
from app.cadence.schemas import (
    AdoptionPlan,
    AdoptionReport,
    AdvanceStep,
    CompanyOnlyTask,
    OverdueTouch,
    ParkResult,
    PendingTask,
    ProspectPlan,
    RosterAction,
    SignalSource,
    SyncReport,
)
from app.cadence.service import CadenceService
from app.core.database import dispose_engine, get_sessionmaker
from app.promotion.client import aclose_hubspot_client, get_hubspot_client


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``sync``, ``adopt``, ``park`` and ``overdue`` to the ``cadence`` command's parser."""
    subcommands = parser.add_subparsers(dest="cadence_command", required=True)
    sync = subcommands.add_parser(
        "sync",
        help=(
            "read outcomes from HubSpot, advance done touches, report what is overdue — run it at "
            "least daily (launchd); voicemail and email are same-day touches"
        ),
    )
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="print what the sync would do; create no task and write nothing",
    )
    adopt = subcommands.add_parser(
        "adopt",
        help=(
            "bring hand-worked prospects into the cadence at the touch they reached; run with "
            "--dry-run first"
        ),
    )
    adopt.add_argument(
        "--roster",
        type=Path,
        metavar="PATH",
        help="a TOML roster marking each contact adopt or park (optional start, company)",
    )
    adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="print what adopting would do — every candidate, or the roster's; write nothing",
    )
    adopt.set_defaults(adopt_parser=adopt)
    park = subcommands.add_parser(
        "park",
        help=(
            "finish a prospect's cadence by hand, for good; its open task is left for you to "
            "close in HubSpot"
        ),
    )
    park.add_argument("contact_id", help="the HubSpot contact id")
    subcommands.add_parser("overdue", help="list live touches past their due date")


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``cadence`` subcommand and return a process exit code."""
    subcommand: str = args.cadence_command
    if subcommand == "sync":
        dry_run: bool = args.dry_run
        return asyncio.run(_run_dry_run() if dry_run else _run_sync())
    if subcommand == "adopt":
        roster: Path | None = args.roster
        adopt_dry_run: bool = args.dry_run
        if roster is None and not adopt_dry_run:
            adopt_parser: argparse.ArgumentParser = args.adopt_parser
            adopt_parser.error("adopt needs --roster; run adopt --dry-run to list candidates")
        return asyncio.run(_run_adopt(roster, dry_run=adopt_dry_run))
    if subcommand == "park":
        contact_id: str = args.contact_id
        return asyncio.run(_run_park(contact_id))
    if subcommand == "overdue":
        return asyncio.run(_run_overdue())
    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


async def _with_session[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one operation, then close the pool and the HubSpot client — a CLI is not a service."""
    try:
        async with get_sessionmaker()() as session:
            return await operation(session)
    finally:
        await dispose_engine()
        await aclose_hubspot_client()


async def _with_service[T](operation: Callable[[CadenceService], Awaitable[T]]) -> T:
    """Run one service operation.

    ``get_hubspot_client`` is looked up here at call time rather than bound as a default, so a
    test can point the CLI at a mock portal by patching this module.
    """
    return await _with_session(lambda session: operation(_service(session)))


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


async def _run_dry_run() -> int:
    async def operation(service: CadenceService) -> SyncReport:
        return await service.sync(dry_run=True)

    report = await _with_service(operation)
    print("dry run — nothing will be created or written")
    for warning in report.warnings:
        print(f"warning: {warning}")
    for plan in report.plans:
        _print_plan(plan)
    advancing = sum(1 for plan in report.plans if plan.changed)
    print(
        f"checked {report.checked} · would advance {advancing} · "
        f"unchanged {len(report.plans) - advancing} · failed {len(report.failures)}"
    )
    for failure in report.failures:
        print(f"contact {failure.hubspot_contact_id}: failed ({failure.code}) — {failure.error}")
    _print_overdue(report.overdue)
    return 1 if report.failures else 0


def _print_plan(plan: ProspectPlan) -> None:
    """One prospect's rehearsed sync. A prospect with nothing new prints nothing."""
    lines: list[str] = []
    if plan.pending_task is PendingTask.found:
        lines.append(f"interrupted task create: found task {plan.pending_task_id}, would adopt it")
    elif plan.pending_task is PendingTask.would_create:
        lines.append("interrupted task create: no task found, would create it")
        if plan.pending_task_superseded:
            lines.append("  …and leave it open, since its touch is already logged — close it then")
    lines.extend(f"would close {_label(step)} — {_closed_by(step)}" for step in plan.steps)
    if plan.parks:
        lines.append("would park — the cadence is finished")
    elif plan.changed and plan.next_position is not None and plan.next_due_at is not None:
        due = plan.next_due_at.astimezone(CADENCE_TZ).strftime("%Y-%m-%d %H:%M")
        position = plan.next_position
        lines.append(
            f"would create {position.cycle}/{CYCLES} {position.touch.value}, due {due} (Dallas)"
        )
    if plan.superseded_task_id is not None:
        lines.append(f"open task {plan.superseded_task_id}: already logged — close it in HubSpot")
    if plan.task_missing:
        lines.append("its cadence task was deleted in HubSpot — not recreated")
    if not lines:
        return
    print(f"contact {plan.hubspot_contact_id}:")
    for line in lines:
        print(f"  {line}")


def _label(step: AdvanceStep) -> str:
    return f"{step.position.cycle}/{CYCLES} {step.position.touch.value}"


def _closed_by(step: AdvanceStep) -> str:
    """What closed a touch: the tick, the logged activity, or both — ``note 88000005``."""
    signal = step.signal
    activity = _activity_text(signal.anchor_ref)
    if signal.source is SignalSource.task_completed:
        return "task ticked"
    if signal.source is SignalSource.both:
        return f"task ticked + {activity}"
    return activity


def _activity_text(ref: str | None) -> str:
    """``"notes:88000005"`` → ``"note 88000005"``."""
    if ref is None:
        return "logged activity"
    kind, _, activity_id = ref.partition(":")
    return f"{kind.removesuffix('s')} {activity_id}"


async def _run_adopt(roster_path: Path | None, *, dry_run: bool) -> int:
    """Discover (no roster), rehearse (roster + dry run) or apply (roster alone)."""
    roster = None if roster_path is None else load_roster(roster_path)

    async def operation(session: AsyncSession) -> AdoptionReport:
        adopter = Adopter(session, hubspot=get_hubspot_client)
        if roster is None:
            return await adopter.discover()
        if dry_run:
            return await adopter.rehearse(roster)
        return await adopter.apply(roster)

    report = await _with_session(operation)
    if report.dry_run:
        print("dry run — nothing will be created or written")
    for warning in report.warnings:
        print(f"warning: {warning}")
    for plan in report.plans:
        _print_adoption_plan(plan, dry_run=report.dry_run)
    listed = {plan.hubspot_contact_id for plan in report.plans}
    enrolled = set(report.already_enrolled)
    for task in report.company_only:
        print(_company_only_text(task, listed=listed, enrolled=enrolled))
    for contact_id in report.already_enrolled:
        print(f"contact {contact_id}: already has a cadence — skipped")
    for failure in report.failures:
        print(f"contact {failure.hubspot_contact_id}: failed ({failure.code}) — {failure.error}")
    if report.dry_run:
        parks = sum(1 for plan in report.plans if plan.parks)
        print(
            f"would adopt {len(report.plans) - parks} · would park {parks} · already enrolled "
            f"{len(report.already_enrolled)} · failed {len(report.failures)}"
        )
        return 1 if report.failures else 0
    for state in report.adopted:
        due = "" if state.due_at is None else f", due {_dallas(state.due_at)} (Dallas)"
        print(
            f"adopted {state.hubspot_contact_id} at {state.cycle}/{CYCLES} {state.touch.value} — "
            f"task {state.hubspot_task_id}{due}"
        )
    for state in report.parked:
        print(
            f"parked {state.hubspot_contact_id} at {state.cycle}/{CYCLES} {state.touch.value} — "
            "final, it will not be enrolled again"
        )
    to_close = [
        task.task_id for plan in report.plans for task in plan.hand_tasks if not task.completed
    ]
    if to_close:
        print(f"old hand tasks to close in HubSpot: {', '.join(to_close)}")
    if report.orphaned_tasks:
        print(
            "tasks this run created for a contact another run enrolled first — close in HubSpot: "
            f"{', '.join(report.orphaned_tasks)}"
        )
    print("log nothing here — outcomes stay in HubSpot")
    return 1 if report.failures else 0


def _company_only_text(task: CompanyOnlyTask, *, listed: set[str], enrolled: set[str]) -> str:
    """What to do about a hand task on a company: usually a duplicate of a contact's own task.

    A company whose contact is listed (or already enrolled) needs nothing new — adopting that
    contact covers it. One whose contacts carry no open task needs them put in the roster. Only a
    company with no contact at all needs one added in HubSpot.
    """
    noun = "company" if len(task.company_ids) == 1 else "companies"
    where = f"{noun} {', '.join(task.company_ids)}" if task.company_ids else "no company"
    lead = f'task {task.task_id} "{task.subject}" is on {where}'
    covered = [contact for contact in task.company_contacts if contact in listed]
    adopted = [contact for contact in task.company_contacts if contact in enrolled]
    if covered:
        return (
            f"{lead}, whose {_contacts(covered, 'is', 'are')} listed above — adopting "
            f"{_them(covered)} covers this; close this task by hand"
        )
    if adopted:
        return (
            f"{lead}, whose {_contacts(adopted, 'is', 'are')} already in the cadence — close this "
            "task by hand"
        )
    if task.company_contacts:
        them = _them(task.company_contacts)
        return (
            f"{lead}, whose {_contacts(task.company_contacts, 'has', 'have')} no open hand task — "
            f"put {them} in the roster to adopt {them}"
        )
    return f"{lead} with no contact — add a contact in HubSpot, then put it in the roster"


def _them(ids: list[str]) -> str:
    """``it`` for one contact, ``them`` for several."""
    return "it" if len(ids) == 1 else "them"


def _contacts(ids: list[str], singular: str, plural: str) -> str:
    """``contact 1 is`` / ``contacts 1, 2 are`` — the verb agreeing with the count."""
    if len(ids) == 1:
        return f"contact {ids[0]} {singular}"
    return f"contacts {', '.join(ids)} {plural}"


def _print_adoption_plan(plan: AdoptionPlan, *, dry_run: bool) -> None:
    """One contact: who, the evidence, where it would start, and the hand tasks around it."""
    would = "would " if dry_run else ""
    header = f"contact {plan.hubspot_contact_id}"
    if plan.display_name is not None:
        header += f" ({plan.display_name})"
    if plan.company_id is not None:
        header += f" · company {plan.company_id}"
    print(header)
    if not plan.steps:
        print("  evidence: none logged on the contact")
    for index, step in enumerate(plan.steps):
        label = "evidence:" if index == 0 else " " * len("evidence:")
        when = _dallas(step.signal.done_at)
        print(f"  {label} {_label(step)} ← {_closed_by(step)}, {when} (Dallas)")
    if plan.action is RosterAction.park:
        print(f"  {would}park — the roster says so; it will never be enrolled")
    elif plan.finished:
        print(f"  {would}park — all {CYCLES * 3} touches are already logged")
    elif plan.start is not None and plan.due_at is not None:
        note = " (roster start — the evidence above is not counted)" if plan.overridden else ""
        print(f"  {would}start {_position(plan.start)}, due {_dallas(plan.due_at)} (Dallas){note}")
    open_tasks = [task for task in plan.hand_tasks if not task.completed]
    ticked = [task for task in plan.hand_tasks if task.completed]
    if open_tasks:
        listed = ", ".join(f'{task.task_id} "{task.subject}"' for task in open_tasks)
        print(f"  open hand tasks — close them in HubSpot after adopting: {listed}")
    if ticked:
        listed = ", ".join(f'{task.task_id} "{task.subject}"' for task in ticked)
        print(
            "  ticked hand tasks (not counted as evidence — set `start` if they were touches): "
            f"{listed}"
        )
    for warning in plan.warnings:
        print(f"  warning: {warning}")


def _position(position: CadencePosition) -> str:
    return f"{position.cycle}/{CYCLES} {position.touch.value}"


def _dallas(moment: datetime) -> str:
    return moment.astimezone(CADENCE_TZ).strftime("%Y-%m-%d %H:%M")


async def _run_park(contact_id: str) -> int:
    async def operation(service: CadenceService) -> ParkResult:
        return await service.park(contact_id)

    result = await _with_service(operation)
    print(
        f"parked {result.hubspot_contact_id} at {result.cycle}/{CYCLES} {result.touch.value} — "
        "final, it will not be enrolled again"
    )
    if result.open_task_id is not None:
        print(f"open task {result.open_task_id} is left as it is — close it in HubSpot")
    if result.pending_task_key is not None:
        print(
            "warning: a task create was interrupted, so a task may exist in HubSpot — look for "
            f"one ending 'Ref: {result.pending_task_key}' and close it"
        )
    print("log what happened as a note on the contact in HubSpot — outcomes are not kept here")
    return 0


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
