"""``lpe`` — the command line entry point.

Thin on purpose: it owns the root parser, the slice registrations, and the one place a deliberate
failure turns into an exit code. Each slice contributes its own command group, so adding one is an
import and two lines here.

Exit codes: ``0`` success; ``1`` a failure the person can act on — a ``LocalProspectEngineError``
or a database refusal — printed as one readable line, **never a traceback**; ``2`` argparse's own
usage error.
"""

import argparse
import sys
from collections.abc import Sequence

import structlog
from sqlalchemy.exc import IntegrityError

from app.cadence import cli as cadence_cli
from app.core.exceptions import LocalProspectEngineError
from app.core.logging import setup_logging
from app.manifests import cli as manifests_cli


def _stderr_logger(*logger_name: str) -> structlog.PrintLogger:
    """A logger writing to whatever ``sys.stderr`` is **right now**.

    Not ``PrintLoggerFactory(sys.stderr)``, which captures the stream object once: under pytest's
    output capture the captured stream is replaced per test, and a cached logger then writes to a
    closed file.

    ``logger_name`` is the module name structlog passes to every logger factory; this sink does not
    use it, and accepting it is what makes the function usable as one.
    """
    return structlog.PrintLogger(sys.stderr)


def _configure_logging() -> None:
    """Configure logging as the service does, then send it to stderr instead of stdout.

    The service's sink is stdout, which is right for a process whose output *is* its log. A CLI's
    stdout is the review surface — a person reading a manifest's citations, or a shell piping the
    table somewhere — so interleaving JSON log lines into it would corrupt exactly the output this
    command exists to produce. Only the sink changes; the processors stay as ``setup_logging`` set
    them, so the events themselves are identical.
    """
    setup_logging()
    structlog.configure(logger_factory=_stderr_logger, cache_logger_on_first_use=False)


def build_parser() -> argparse.ArgumentParser:
    """Build the root parser with every slice's command group attached."""
    parser = argparse.ArgumentParser(
        prog="lpe",
        description="Local Prospect Engine — internal GTM tooling.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    manifests_cli.register(
        commands.add_parser("manifest", help="review and activate vertical manifests")
    )
    cadence_cli.register(commands.add_parser("cadence", help="sync the cadence; list overdue"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and **return** its exit code.

    Returns rather than calling ``sys.exit`` so tests can assert on the code directly; the
    ``[project.scripts]`` wrapper turns the return value into the process status for free.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging()

    command: str = args.command
    try:
        if command == "manifest":
            return manifests_cli.dispatch(args)
        if command == "cadence":
            return cadence_cli.dispatch(args)
    except LocalProspectEngineError as exc:
        # A deliberate failure is a message, not a stack trace: every one of these is something a
        # person can act on (read the terms, fix the id, activate a draft first).
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except IntegrityError:
        # The database refused the write. "Never a traceback" is unconditional, so it has to hold
        # for refusals a slice did not model as a domain error too.
        #
        # The wording names no command and no table, because this handler covers **every** slice's
        # commands — present and future — and more than one constraint can land here. Today that is
        # the partial unique index (two activations racing one vertical) and the (vertical, version)
        # constraint reachable through `create_draft`'s select-max-then-insert, which is what T12's
        # `propose` will call. A message naming one of them is wrong advice for the other.
        #
        # It is prose rather than `str(exc)` for a second reason: `IntegrityError`'s own text is a
        # multi-line SQL and parameter dump, which is a traceback by another name.
        # PR #6 review, finding 2 (round 1) and the shared-file wording (round 2).
        print(
            "error: the database refused the write — a concurrent write may have landed first; "
            "re-read the record before retrying",
            file=sys.stderr,
        )
        return 1

    raise AssertionError(f"unreachable: argparse accepted unknown command {command!r}")
