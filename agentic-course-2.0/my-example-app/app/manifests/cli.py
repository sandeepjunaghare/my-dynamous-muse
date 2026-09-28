"""``lpe manifest`` — the manifest review surface.

This is the whole review UI: a CLI, no frontend and no second login (architecture → *Review
surface*). ``show`` renders every citation so a person can judge what was researched, and
``activate`` is the single door to ACTIVE, which is also where the per-source terms-of-use decision
is recorded.

``propose`` is **T12's**, and is deliberately absent rather than stubbed: a subcommand that appears
in ``--help`` and then raises reads as a broken feature.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import dispose_engine, get_sessionmaker
from app.manifests.schemas import (
    ManifestBody,
    ManifestResponse,
    ManifestStatus,
    TermsDecision,
)
from app.manifests.service import ManifestService, parse_accept_terms
from app.shared.provenance import ProvenancedValue

ACTOR_UNSPECIFIED = "cli"
"""Recorded as ``decided_by`` when nobody is named. Single internal user; no auth in the MVP."""


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``list``, ``show`` and ``activate`` to the ``manifest`` command's parser."""
    subcommands = parser.add_subparsers(dest="manifest_command", required=True)

    list_parser = subcommands.add_parser("list", help="list manifests")
    list_parser.add_argument("--vertical", default=None, help="only this vertical")
    list_parser.add_argument(
        "--status",
        default=None,
        choices=[status.value for status in ManifestStatus],
        help="only manifests in this lifecycle state",
    )

    show_parser = subcommands.add_parser("show", help="render one manifest with every citation")
    show_parser.add_argument("manifest_id", type=UUID, help="the manifest's id")

    activate_parser = subcommands.add_parser(
        "activate",
        help="record the terms-of-use decision per source and flip a draft active",
    )
    activate_parser.add_argument("manifest_id", type=UUID, help="the draft's id")
    activate_parser.add_argument(
        "--accept-terms",
        required=True,
        help="comma-separated sources whose terms you have read and accept (e.g. fmcsa,places)",
    )
    activate_parser.add_argument(
        "--actor",
        default=ACTOR_UNSPECIFIED,
        help="who is accepting, recorded on every terms decision",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Run the requested ``manifest`` subcommand and return a process exit code."""
    subcommand: str = args.manifest_command

    if subcommand == "list":
        vertical: str | None = args.vertical
        raw_status: str | None = args.status
        status = None if raw_status is None else ManifestStatus(raw_status)
        return asyncio.run(_run_list(vertical, status))

    if subcommand == "show":
        manifest_id: UUID = args.manifest_id
        return asyncio.run(_run_show(manifest_id))

    if subcommand == "activate":
        target_id: UUID = args.manifest_id
        accept_terms: str = args.accept_terms
        actor: str = args.actor
        return asyncio.run(_run_activate(target_id, accept_terms, actor))

    raise AssertionError(f"unreachable: argparse accepted unknown subcommand {subcommand!r}")


async def _with_session[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one operation against a session, then close the pool.

    Without the dispose, the process exits holding live connections and asyncpg complains on the
    way out — a CLI is not a long-running service.
    """
    try:
        async with get_sessionmaker()() as session:
            return await operation(session)
    finally:
        await dispose_engine()


async def _run_list(vertical: str | None, status: ManifestStatus | None) -> int:
    async def operation(session: AsyncSession) -> list[ManifestResponse]:
        return await ManifestService(session).list(vertical=vertical, status=status, limit=100)

    manifests = await _with_session(operation)
    if not manifests:
        print("no manifests")
        return 0

    print(f"{'id':<38} {'vertical':<12} {'ver':>3}  {'status':<11} sources")
    for manifest in manifests:
        sources = ", ".join(manifest.body.source_names())
        print(
            f"{manifest.id!s:<38} {manifest.vertical:<12} {manifest.version:>3}  "
            f"{manifest.status.value:<11} {sources}"
        )
    return 0


async def _run_show(manifest_id: UUID) -> int:
    async def operation(session: AsyncSession) -> ManifestResponse:
        return await ManifestService(session).get(manifest_id)

    manifest = await _with_session(operation)
    _render(manifest)
    return 0


async def _run_activate(manifest_id: UUID, accept_terms: str, actor: str) -> int:
    accepted = parse_accept_terms(accept_terms)

    async def operation(session: AsyncSession) -> ManifestResponse:
        return await ManifestService(session).activate(manifest_id, accepted, actor)

    manifest = await _with_session(operation)
    print(
        f"activated {manifest.vertical} v{manifest.version} ({manifest.id}); "
        f"terms accepted by {actor} for: {', '.join(sorted(accepted))}"
    )
    return 0


def _render(manifest: ManifestResponse) -> None:
    """Print one manifest in full: every value, every citation, and the terms position."""
    body = manifest.body
    print(f"{manifest.vertical} v{manifest.version}  [{manifest.status.value}]")
    print(f"id:         {manifest.id}")
    print(f"created:    {manifest.created_at.isoformat()}")
    if manifest.activated_at is not None:
        print(f"activated:  {manifest.activated_at.isoformat()} by {manifest.activated_by}")

    print("\nsources:")
    for cited_source in body.sources:
        source = cited_source.value
        print(f"  - {source.name} ({source.kind.value}) {source.base_url}")
        print(f"    {source.description}")
        if source.rate_limit_per_minute is not None:
            print(f"    rate limit: {source.rate_limit_per_minute}/min")
        _print_citation(cited_source)

    print("\ndisqualifier rules:")
    for cited_rule in body.disqualifier_rules:
        rule = cited_rule.value
        clause = (
            f" [{rule.field} {rule.operator.value} {rule.value!r}]"
            if rule.operator is not None
            else ""
        )
        print(f"  - {rule.id} ({rule.kind.value}){clause}")
        print(f"    {rule.description}")
        _print_citation(cited_rule)

    print("\nqualifying signals:")
    for cited_signal in body.qualifying_signals:
        signal = cited_signal.value
        print(f"  - {signal.id} -> {signal.feeds.value}")
        print(f"    {signal.question}")
        _print_citation(cited_signal)

    band = body.icp_band.value
    print("\nicp band:")
    print(
        f"  - {band.headcount_min}-{band.headcount_max} employees, "
        f"office function {'required' if band.requires_office_function else 'not required'}"
    )
    _print_citation(body.icp_band)

    print("\nvocabulary:")
    print(f"  - {', '.join(body.vocabulary.value.terms)}")
    _print_citation(body.vocabulary)

    _print_terms(body)


def _print_citation[T](provenanced: ProvenancedValue[T]) -> None:
    """Print the three provenance fields of a cited value, indented under it.

    Generic because the citations rendered here wrap six different value types, and the parameter
    is what each of them has in common — ``ProvenancedValue`` is invariant in ``T``, so
    ``ProvenancedValue[object]`` would accept none of them.
    """
    print(
        f"    cited: {provenanced.source_url} | "
        f"{provenanced.retrieved_at.isoformat()} | "
        f"{provenanced.retrieval_method.value}"
    )


def _print_terms(body: ManifestBody) -> None:
    """Print one line per declared source saying where its terms-of-use decision stands."""
    print("\nterms of use:")
    decisions = {entry.source_name: entry for entry in body.terms}
    for name in body.source_names():
        entry = decisions.get(name)
        if entry is None:
            print(f"  - {name}: UNDECIDED — blocks activation")
            continue
        if entry.decision is TermsDecision.rejected:
            print(
                f"  - {name}: REJECTED by {entry.decided_by} on "
                f"{entry.decided_at.date().isoformat()} — blocks activation"
            )
            continue
        licence = f" ({entry.license})" if entry.license else ""
        print(
            f"  - {name}: accepted by {entry.decided_by} on "
            f"{entry.decided_at.date().isoformat()}{licence}"
        )
