"""``lpe manifest`` — the manifest review surface.

This is the whole review UI: a CLI, no frontend and no second login (architecture → *Review
surface*). ``show`` renders every citation so a person can judge what was researched, and
``activate`` is the single door to ACTIVE, which is also where the per-source terms-of-use decision
is recorded.

``propose`` runs the authoring agent (T12) and writes what it found as a **DRAFT** — never ACTIVE.
It prints the draft with every citation, what was left out and why, and the terms-of-use question
per source.

``dry-run`` (T7) is the manifest quality gate: it runs the free rules over a local extract of the
real source data, prints the pool after each rule with sample rows and flags, and records the
result. ``activate`` refuses a manifest that has never been dry-run.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cost import RunCost, format_usd
from app.core.database import dispose_engine, get_sessionmaker
from app.manifests.agent import run_agent
from app.manifests.proposal import DraftProposal, build_draft
from app.manifests.schemas import (
    SLUG_MAX_LENGTH,
    SLUG_PATTERN,
    DryRunReport,
    DryRunResponse,
    DryRunSample,
    ManifestBody,
    ManifestResponse,
    ManifestStatus,
    TermsDecision,
)
from app.manifests.service import ManifestService, parse_accept_terms
from app.qualification.dry_run import dry_run
from app.shared.provenance import ProvenancedValue

ACTOR_UNSPECIFIED = "cli"
"""Recorded as ``decided_by`` when nobody is named. Single internal user; no auth in the MVP."""

QUALITY_REVIEW_NOTE = (
    "NOTE: nothing has yet checked these disqualifier rules against real data. Run "
    "`lpe manifest dry-run <id> --source-file <source>=<extract.csv>` and read the funnel and its "
    "flags before activating; `activate` refuses a manifest that has never been dry-run. A "
    "mis-chosen *source* is still only caught by reading the citations above."
)
"""Printed on every proposal. A plausible-but-wrong rule fails silently (E10); the dry-run (T7)
is the gate that catches it, and the reviewer is told, every time, to run it."""


def _slug(raw: str) -> str:
    """argparse type for ``--vertical``: the same lowercase slug the manifest schema demands."""
    if re.fullmatch(SLUG_PATTERN, raw) is None or len(raw) > SLUG_MAX_LENGTH:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not a lowercase slug of at most {SLUG_MAX_LENGTH} characters "
            "(e.g. collision_centers)"
        )
    return raw


def _source_file(raw: str) -> tuple[str, Path]:
    """argparse type for ``--source-file NAME=PATH``: a source slug and an existing local file."""
    name, separator, path = raw.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError(f"{raw!r} is not NAME=PATH (e.g. fmcsa=census.csv)")
    if re.fullmatch(SLUG_PATTERN, name) is None:
        raise argparse.ArgumentTypeError(f"{name!r} is not a source slug")
    file = Path(path).expanduser()
    if not file.is_file():
        raise argparse.ArgumentTypeError(f"{path!r} is not a readable file")
    return name, file


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``list``, ``show``, ``propose``, ``dry-run`` and ``activate`` to ``manifest``."""
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

    propose_parser = subcommands.add_parser(
        "propose",
        help="research a brief with the authoring agent and write a cited DRAFT manifest",
    )
    propose_parser.add_argument("brief", help='e.g. "collision centers, DFW"')
    propose_parser.add_argument(
        "--vertical",
        type=_slug,
        default=None,
        help="the vertical's slug; when omitted the agent proposes one",
    )

    dry_run_parser = subcommands.add_parser(
        "dry-run",
        help="run a manifest's free rules over a local extract and record the funnel",
    )
    dry_run_parser.add_argument("manifest_id", type=UUID, help="the manifest's id")
    dry_run_parser.add_argument(
        "--source-file",
        type=_source_file,
        action="append",
        required=True,
        metavar="SOURCE=PATH",
        help="a local extract of a bulk_file source, e.g. fmcsa=census.csv (repeatable)",
    )
    dry_run_parser.add_argument(
        "--samples", type=int, default=3, help="sample rows to show per rule (default 3)"
    )
    dry_run_parser.add_argument(
        "--actor", default=ACTOR_UNSPECIFIED, help="who ran it, recorded on the dry-run"
    )

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

    if subcommand == "propose":
        brief: str = args.brief
        proposed_vertical: str | None = args.vertical
        return asyncio.run(_run_propose(brief, proposed_vertical))

    if subcommand == "dry-run":
        dry_run_id: UUID = args.manifest_id
        source_files: list[tuple[str, Path]] = args.source_file
        samples: int = args.samples
        dry_run_actor: str = args.actor
        return asyncio.run(_run_dry_run(dry_run_id, source_files, samples, dry_run_actor))

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


async def _run_propose(brief: str, vertical: str | None) -> int:
    """Research, gate every field on its citation, then write one DRAFT.

    The agent runs with **no database session open**: a research loop takes minutes, and holding a
    pooled connection across it buys nothing. The write is one short session afterwards.
    """
    cost = RunCost()
    agent_run = await run_agent(brief, cost=cost, vertical=vertical)
    draft = build_draft(agent_run.proposal, agent_run.reads, vertical_override=vertical)

    async def operation(session: AsyncSession) -> ManifestResponse:
        return await ManifestService(session).create_draft(draft.vertical, draft.body)

    manifest = await _with_session(operation)
    _render(manifest)
    _print_proposal_review(draft, cost, known_cost=agent_run.cost_usd is not None)
    return 0


def _print_proposal_review(draft: DraftProposal, cost: RunCost, *, known_cost: bool) -> None:
    """What the reviewer needs beyond the row itself: evidence, gaps, open decisions, cost."""
    print("\nevidence quoted by the agent:")
    for field in draft.cited:
        print(f'  - {field.label}: "{field.quote}"')
        print(f"    {field.url}")

    print("\nleft out (no usable citation):")
    if not draft.omitted:
        print("  - nothing")
    for omitted in draft.omitted:
        print(f"  - {omitted.label}: {omitted.reason}")

    print("\nterms-of-use questions (answered only at `lpe manifest activate --accept-terms`):")
    for question in draft.terms_questions:
        where = question.terms_url or "the agent did not find published terms; locate them"
        print(f"  - {question.source_name}: have you read and do you accept its terms? -> {where}")

    spent = format_usd(cost.total_usd()) if known_cost else "unknown (the SDK reported no cost)"
    print(
        f"\nagent run cost: {spent} over {cost.total_calls()} turn(s)"
        " (logged as core.cost.call_recorded)"
    )
    print(f"\n{QUALITY_REVIEW_NOTE}")


async def _run_dry_run(
    manifest_id: UUID, source_files: Sequence[tuple[str, Path]], samples: int, actor: str
) -> int:
    """Read the manifest, run the rules with **no session open**, then record the result.

    Mirrors ``_run_propose``: the pass over a ~378k-row extract takes a while, and holding a pooled
    connection across it buys nothing. Two short sessions instead.
    """
    names = [name for name, _ in source_files]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        print(f"error: more than one --source-file for: {', '.join(duplicates)}", file=sys.stderr)
        return 1

    async def read(session: AsyncSession) -> ManifestResponse:
        return await ManifestService(session).get(manifest_id)

    manifest = await _with_session(read)
    report = dry_run(manifest, dict(source_files), samples=samples)

    async def record(session: AsyncSession) -> DryRunResponse:
        return await ManifestService(session).record_dry_run(manifest_id, report, actor)

    recorded = await _with_session(record)
    _print_dry_run(manifest, report)
    print(
        f"\nrecorded dry-run {recorded.id}; `lpe manifest activate {manifest_id}` will now accept "
        "this manifest. Read every flag above before you do."
    )
    return 0


def _print_sample(sample: DryRunSample) -> None:
    fields = ", ".join(f"{name}={value!r}" for name, value in sample.fields)
    print(f"      {sample.row_id}: {fields}")


def _print_dry_run(manifest: ManifestResponse, report: DryRunReport) -> None:
    """The funnel per rule, the samples, and the flags a person must read."""
    print(f"dry-run of {manifest.vertical} v{manifest.version} ({manifest.id})")
    for file in report.files:
        print(
            f"  {file.source_name}: {file.file_name}, {file.rows} rows "
            f"(row id: {file.row_id_column}), sha256 {file.sha256[:12]}…"
        )

    print(
        f"\n{'rule':<28} {'source':<12} {'before':>8} {'removed':>8} {'alone':>8} {'n/a':>6}  flags"
    )
    for step in report.steps:
        flags = ", ".join(flag.value for flag in step.flags) or "-"
        print(
            f"{step.rule_id:<28} {step.source:<12} {step.pool_before:>8} {step.removed:>8} "
            f"{step.matched_standalone:>8} {step.not_evaluable:>6}  {flags}"
        )
    for file in report.files:
        print(f"\n{file.source_name}: {file.rows} rows in, {file.pool_end} left after the rules")

    for step in report.steps:
        if step.removed_samples:
            print(f"\n  removed by {step.rule_id}:")
            for sample in step.removed_samples:
                _print_sample(sample)
    for file in report.files:
        if file.passing_samples:
            print(f"\n  passing ({file.source_name}):")
            for sample in file.passing_samples:
                _print_sample(sample)

    flagged = report.flagged()
    print("\nFLAGGED:" if flagged else "\nFLAGGED: nothing")
    for entry in flagged:
        print(f"  - {entry}")


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
