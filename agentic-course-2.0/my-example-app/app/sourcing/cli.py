"""``lpe sourcing run`` — source one brief by hand, before T10's ``POST /runs`` exists.

The same pipeline T10 will trigger weekly: refresh the vertical's backlog from its registry, take a
batch best first, gather the free evidence, record cited candidates. It runs under the vertical's
**ACTIVE** manifest only — a draft is refused, because nobody has decided its sources' terms.

The brief's ICP band is the active manifest's own: the CLI asks for "the vertical's run", and a
band typed here would only be recorded, never applied (T7 reconciles bands).
"""

import argparse
import asyncio
import sys

from app.core.cost import format_usd
from app.core.database import dispose_engine, get_sessionmaker
from app.manifests.service import ManifestService
from app.sourcing.pipeline import SourcingPipeline
from app.sourcing.schemas import RunStatus, SourcingBrief, SourcingRunResponse
from app.tools.registry import Stage, registered_stages
from app.tools.search_registry import SearchRegistryStage

DEFAULT_GEOGRAPHY = "dfw"


def _positive(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``run`` to the ``sourcing`` command's parser."""
    subcommands = parser.add_subparsers(dest="sourcing_command", required=True)
    run = subcommands.add_parser(
        "run",
        help=(
            "refresh the vertical's backlog from its registry and record this run's batch of "
            "cited candidates (free sources only)"
        ),
    )
    run.add_argument("--vertical", required=True, help="a vertical with an ACTIVE manifest")
    run.add_argument(
        "--geography",
        default=DEFAULT_GEOGRAPHY,
        help=f"recorded on the run (default {DEFAULT_GEOGRAPHY}); the manifest sets the area",
    )
    run.add_argument(
        "--batch-size",
        type=_positive,
        metavar="N",
        help="candidates to take from the backlog this run (default SOURCING_BATCH_SIZE, 150)",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``sourcing`` subcommand and return a process exit code."""
    subcommand: str = args.sourcing_command
    if subcommand == "run":
        vertical: str = args.vertical
        geography: str = args.geography
        batch_size: int | None = args.batch_size
        return asyncio.run(_run(vertical, geography, batch_size))
    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


def _stages(batch_size: int | None) -> tuple[Stage, ...]:
    """Every registered stage, with ``search_registry`` rebuilt for a batch size given here."""
    if batch_size is None:
        return registered_stages()
    return tuple(
        stage.with_batch_size(batch_size) if isinstance(stage, SearchRegistryStage) else stage
        for stage in registered_stages()
    )


async def _run(vertical: str, geography: str, batch_size: int | None) -> int:
    try:
        async with get_sessionmaker()() as session:
            manifest = await ManifestService(session).get_active(vertical)
        brief = SourcingBrief(
            vertical=vertical, geography=geography, icp_band=manifest.body.icp_band.value
        )
        run = await SourcingPipeline(stages=_stages(batch_size)).run(brief)
    finally:
        await dispose_engine()
    _print(run)
    return 0


def _print(run: SourcingRunResponse) -> None:
    print(f"run {run.id}  {run.vertical}  {run.status.value}")
    for name in sorted(run.counts):
        print(f"  {name:<28}{run.counts[name]:>8}")
    print(f"  {'cost':<28}{format_usd(run.cost.total_usd()):>8}")
    if run.status is RunStatus.degraded and run.status_detail:
        print(f"warning: {run.status_detail}", file=sys.stderr)
