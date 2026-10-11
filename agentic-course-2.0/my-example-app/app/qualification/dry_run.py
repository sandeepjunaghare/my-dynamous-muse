"""The manifest dry-run: a manifest's free rules over real source data, before anyone activates it.

Decided 2026-10-09 as the answer to "who reviews manifest *quality*": freight v1 shipped a rule that
matched nothing and v2 an exclusion list that let through everything it did not list, and both
were caught only by a person running the rules against the real census by hand. This is that hand
check, mechanised. It prints the pool after each rule, with sample rows, and flags the shapes of
failure that check found. A person still reads it — they now read numbers rather than reasoning.

Reads **local bulk-file extracts** (decided 2026-10-10), one per ``bulk_file`` source. It writes
nothing and calls nothing: a rule on a per-record API (QCMobile) is reported as not evaluable
offline rather than called. Files are streamed, never loaded whole — the TX census is ~378k rows.
"""

import csv
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.manifests.schemas import (
    DisqualifierRule,
    DryRunFlag,
    DryRunReport,
    DryRunSample,
    DryRunSourceFile,
    DryRunStep,
    ManifestResponse,
    RuleOperator,
    SourceKind,
)
from app.qualification.exceptions import DryRunSourceError, RuleNotEvaluableError
from app.qualification.rules import check_shape, evaluate, predicate_rules
from app.qualification.schemas import OutcomeKind

logger = get_logger(__name__)

_LITERAL_OPERATORS = frozenset(
    {
        RuleOperator.equals,
        RuleOperator.not_equals,
        RuleOperator.contains,
        RuleOperator.not_contains,
        RuleOperator.in_set,
        RuleOperator.not_in_set,
    }
)
"""Operators whose literal can be looked for in the data — the v1 ``'asset_based'`` check."""

_HASH_CHUNK = 1 << 20


@dataclass
class _Tally:
    """One rule's running counts over one source's rows."""

    rule: DisqualifierRule
    pool_before: int = 0
    removed: int = 0
    matched: int = 0
    not_evaluable: int = 0
    literal_seen: bool = False
    samples: list[DryRunSample] = field(default_factory=list[DryRunSample])


def _literal_seen(rule: DisqualifierRule, text: str) -> bool:
    value = rule.value
    if rule.operator in {RuleOperator.contains, RuleOperator.not_contains}:
        return str(value) in text
    if isinstance(value, tuple):
        return text in {item.strip() for item in value}
    return text == str(value).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _sample(row_id: str, record: Mapping[str, str | None], fields: Sequence[str]) -> DryRunSample:
    return DryRunSample(
        row_id=row_id,
        fields=tuple((name, (record.get(name) or "").strip()) for name in fields),
    )


def _check_files(manifest: ManifestResponse, files: Mapping[str, Path]) -> None:
    """Every declared ``bulk_file`` source needs its extract, and only those may be given one.

    A manifest that declares **no** bulk file (an API-only registry, like the fire manifest's) is
    dry-run with no files at all: every predicate is then recorded as not evaluable offline, so the
    record says plainly that nothing was checked against data — rather than the gate being
    impossible to satisfy (PR #18 review, H1).
    """
    kinds = {cited.value.name: cited.value.kind for cited in manifest.body.sources}
    for name in files:
        if name not in kinds:
            raise DryRunSourceError(
                f"manifest {manifest.id} declares no source {name!r}; it declares: "
                f"{', '.join(kinds)}",
                source_name=name,
            )
        if kinds[name] is not SourceKind.bulk_file:
            raise DryRunSourceError(
                f"source {name!r} is read as {kinds[name].value}, not a bulk file, so a dry-run "
                "cannot read it from a local extract",
                source_name=name,
            )

    bulk = [name for name, kind in kinds.items() if kind is SourceKind.bulk_file]
    missing = [name for name in bulk if name not in files]
    if missing:
        raise DryRunSourceError(
            f"manifest {manifest.id} declares bulk-file source(s) {', '.join(missing)}; pass "
            f"--source-file <source>=<extract.csv> for each",
            source_name=missing[0],
        )


def _run_source(
    name: str, path: Path, tallies: Sequence[_Tally], *, samples: int
) -> tuple[DryRunSourceFile, frozenset[str]]:
    """Stream one source's extract through its rules; return the file summary and its header."""
    fields_read = list(dict.fromkeys(t.rule.field for t in tallies if t.rule.field is not None))
    rows = 0
    pool_end = 0
    passing: list[DryRunSample] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            raise DryRunSourceError(f"{path.name} has no header row", source_name=name)
        columns = [column.strip() for column in header]
        duplicates = sorted({column for column in columns if columns.count(column) > 1})
        if duplicates:
            # The last duplicate would silently win in the row dict, and a rule would read the
            # wrong column with nothing to show for it.
            raise DryRunSourceError(
                f"{path.name} repeats header column(s): {', '.join(duplicates)}", source_name=name
            )
        row_id_column = columns[0]
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue  # a blank line is not a row, and must not inflate the pool
            rows += 1
            record: dict[str, str | None] = dict(zip(columns, raw, strict=False))
            row_id = (record.get(row_id_column) or "").strip() or f"row {rows}"
            records = {name: record}
            in_pool = True
            for tally in tallies:
                outcome = evaluate(tally.rule, records, source=name)
                if outcome.observed is not None and not tally.literal_seen:
                    tally.literal_seen = _literal_seen(tally.rule, outcome.observed)
                if outcome.kind is OutcomeKind.fired:
                    tally.matched += 1
                if not in_pool:
                    continue
                tally.pool_before += 1
                if outcome.kind is OutcomeKind.fired:
                    tally.removed += 1
                    in_pool = False
                    if len(tally.samples) < samples:
                        tally.samples.append(_sample(row_id, record, fields_read))
                elif outcome.kind is OutcomeKind.not_evaluable:
                    tally.not_evaluable += 1
            if in_pool:
                pool_end += 1
                if len(passing) < samples:
                    passing.append(_sample(row_id, record, fields_read))

    summary = DryRunSourceFile(
        source_name=name,
        file_name=path.name,
        sha256=_sha256(path),
        rows=rows,
        pool_end=pool_end,
        row_id_column=row_id_column,
        passing_samples=tuple(passing),
    )
    return summary, frozenset(columns)


def _flags(tally: _Tally, columns: frozenset[str]) -> tuple[DryRunFlag, ...]:
    rule = tally.rule
    if rule.field not in columns:
        return (DryRunFlag.unknown_field,)
    flags: list[DryRunFlag] = []
    if rule.operator in _LITERAL_OPERATORS and not tally.literal_seen:
        flags.append(DryRunFlag.value_not_seen)
    if tally.matched == 0:
        flags.append(DryRunFlag.matches_nothing)
    elif tally.removed == 0:
        flags.append(DryRunFlag.excludes_nothing)
    if tally.pool_before > 0 and tally.pool_before == tally.removed:
        flags.append(DryRunFlag.excludes_everything)
    return tuple(flags)


def dry_run(
    manifest: ManifestResponse, files: Mapping[str, Path], *, samples: int = 3
) -> DryRunReport:
    """Run the manifest's free predicate rules over local extracts and report the funnel.

    ``files`` maps a declared ``bulk_file`` source to its extract. Each source's rules run over that
    source's rows, in declaration order; there is no cross-source join offline.
    """
    _check_files(manifest, files)
    body = manifest.body

    steps: dict[str, DryRunStep] = {}
    tallies_by_source: dict[str, list[_Tally]] = {name: [] for name in files}
    for rule in predicate_rules(body):
        source = body.source_for(rule)
        try:
            check_shape(rule)
        except RuleNotEvaluableError:
            steps[rule.id] = _empty_step(rule, source, DryRunFlag.malformed)
            continue
        if source not in files:
            steps[rule.id] = _empty_step(rule, source, DryRunFlag.not_evaluable_offline)
            continue
        tallies_by_source[source].append(_Tally(rule=rule))

    summaries: list[DryRunSourceFile] = []
    for name, path in files.items():
        summary, columns = _run_source(name, path, tallies_by_source[name], samples=samples)
        summaries.append(summary)
        for tally in tallies_by_source[name]:
            steps[tally.rule.id] = DryRunStep(
                rule_id=tally.rule.id,
                source=name,
                pool_before=tally.pool_before,
                removed=tally.removed,
                matched_standalone=tally.matched,
                not_evaluable=tally.not_evaluable,
                flags=_flags(tally, columns),
                removed_samples=tuple(tally.samples),
            )

    report = DryRunReport(
        manifest_id=manifest.id,
        files=tuple(summaries),
        steps=tuple(steps[rule.id] for rule in predicate_rules(body)),
    )
    logger.info(
        "qualification.dry_run.run_completed",
        manifest_id=str(manifest.id),
        files=[summary.file_name for summary in summaries],
        rows=sum(summary.rows for summary in summaries),
        pool_end=sum(summary.pool_end for summary in summaries),
        flagged=list(report.flagged()),
    )
    return report


def _empty_step(rule: DisqualifierRule, source: str, flag: DryRunFlag) -> DryRunStep:
    return DryRunStep(
        rule_id=rule.id,
        source=source,
        pool_before=0,
        removed=0,
        matched_standalone=0,
        not_evaluable=0,
        flags=(flag,),
    )
