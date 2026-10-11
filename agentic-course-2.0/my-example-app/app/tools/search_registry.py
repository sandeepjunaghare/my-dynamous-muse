"""Stage 1 of 5: search the manifest's registry, refresh the backlog, take this run's batch.

Generic, and parameterised entirely by the manifest (``search_registry(manifest_source, query)``
in the architecture doc). Nothing here knows what freight is, or FMCSA: the sources are bound by
their ``base_url`` (``app/sourcing/sources/base.py``), and the FMCSA adapters are one entry in the
table this stage is built with.

**Free before paid (D8)**, and all of this is free:

1. **Pull** the registry, with the manifest's predicate rules pushed into the registry's own query
   (the census: ~4,450 rows for freight v3, not 4.5 million).
2. **Refresh** the vertical's backlog and **take a batch**, best first (D12) — one commit.
3. **Look up** each batch candidate in every bound lookup source (QCMobile), and **join** the batch
   against every bound join source (Motus revocations).
4. **Record** each candidate, its identity cited to the registry and every source's raw record
   cited to that source.
5. **Apply the free predicates** (T7, ``app/qualification/``) to each candidate's records: the first
   rule to fire is recorded as a cited ``disqualification``, and ``classify_rollup`` skips the
   candidate. The whole batch is evaluated first; a rule that fires on nearly all of it is
   presumed wrong and fails the run with nothing recorded (``check_batch``). Before selection,
   registry ids the vertical already rejected are **suppressed**, so a batch handed back by a
   failed run never re-spends a slot on them. Before the pull, a predicate rule that could never
   be judged fairly (malformed, or reading a join source) refuses the run.

The stage fetches; qualification decides. It calls ``QualificationService`` rather than the
sourcing slice doing so, so ``app/sourcing/`` never imports ``app/qualification/``. A candidate is
never dropped for evidence it lacks: no QCMobile record means its QCMobile rules cannot judge it.

**A batch is never spent without its evidence unless someone said so** (PR #21 review, M1). A
candidate batched without its QCMobile record is never looked up again, so a source that cannot
run here — no webKey before the batch, a refused key during it — fails the run with
:class:`SourceUnavailableError`: before the pull, the backlog is untouched; mid-batch, the failed
run hands its batch back. ``allow_degraded`` (``lpe sourcing run --allow-degraded``) is the explicit
choice to go on without that source, finishing ``degraded``. A *sporadic* lookup failure (a 5xx
outlasting its retries) stays ``degraded`` either way: one blip should not throw away a batch.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.manifests.schemas import RuleKind
from app.qualification.exceptions import RuleNotEvaluableError
from app.qualification.records import records_from
from app.qualification.rules import check_shape, predicate_rules
from app.qualification.service import QualificationService
from app.shared.provenance import ProvenancedValue
from app.sourcing.exceptions import SourceAuthError, SourceUnavailableError
from app.sourcing.schemas import CandidateFields, PoolEntry, SourceRecord
from app.sourcing.service import SourcingService
from app.sourcing.sources import http
from app.sourcing.sources.base import AdapterFactory, PoolRecord, SourceEnv, bind_sources
from app.sourcing.sources.fmcsa import FMCSA_ADAPTERS
from app.sourcing.stages import PipelineStage
from app.tools.registry import StageContext, StageResult

logger = get_logger(__name__)

CURRENCY_WINDOW = timedelta(days=730)
"""D12's "current MCS-150 filing (within 2 years)". Days, not calendar years, so a run on
29 February needs no special case."""

_HTTP_TIMEOUT = httpx.Timeout(10.0, read=60.0)
"""A census page of a few thousand rows can take a while to serialise; a lookup should not."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SearchRegistryStage:
    """``search_registry``. Built with its adapter table, so a test can bind a stub registry."""

    def __init__(
        self,
        *,
        factories: Sequence[AdapterFactory] = FMCSA_ADAPTERS,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utcnow,
        batch_size: int | None = None,
        backoff_seconds: float = http.DEFAULT_BACKOFF_SECONDS,
        throttle: bool = True,
        allow_degraded: bool = False,
    ) -> None:
        self._factories = tuple(factories)
        self._transport = transport
        self._clock = clock
        self._batch_size = batch_size
        self._backoff_seconds = backoff_seconds
        self._throttle = throttle
        self._allow_degraded = allow_degraded

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.search_registry

    def configured(
        self, *, batch_size: int | None = None, allow_degraded: bool | None = None
    ) -> "SearchRegistryStage":
        """This stage with a per-run option changed and everything else kept as it was built."""
        return SearchRegistryStage(
            factories=self._factories,
            transport=self._transport,
            clock=self._clock,
            batch_size=batch_size if batch_size is not None else self._batch_size,
            backoff_seconds=self._backoff_seconds,
            throttle=self._throttle,
            allow_degraded=(allow_degraded if allow_degraded is not None else self._allow_degraded),
        )

    def _refuse(self, reasons: tuple[str, ...], *, when: str) -> SourceUnavailableError:
        logger.warning("tools.search_registry.run_refused", reasons=list(reasons), when=when)
        return SourceUnavailableError(
            f"{'; '.join(reasons)} — {when}. Fix it, or pass --allow-degraded to go on without "
            "that source; the batch is then used up without its evidence",
            reasons=reasons,
        )

    async def run(self, context: StageContext) -> StageResult:
        """Pull, refresh, take a batch, gather the free evidence, record (see the module doc)."""
        settings = get_settings()
        size = self._batch_size or settings.sourcing_batch_size
        service = SourcingService(context.session)
        qualification = QualificationService(context.session)
        reasons: list[str] = []

        async with httpx.AsyncClient(transport=self._transport, timeout=_HTTP_TIMEOUT) as client:
            env = SourceEnv(
                client=client,
                settings=settings,
                clock=self._clock,
                backoff_seconds=self._backoff_seconds,
                throttle=self._throttle,
            )
            bound = bind_sources(context.manifest, self._factories, env)
            if bound.degraded and not self._allow_degraded:
                raise self._refuse(bound.degraded, when="no batch was taken")
            reasons.extend(bound.degraded)

            _refuse_unevaluable_rules(context, tuple(join.name for join in bound.joins))
            rules = [cited.value for cited in context.manifest.body.disqualifier_rules]
            pull = await bound.discovery.pull(rules)
            _refuse_misrouted_rules(context, bound.discovery.name, pull.known_fields)
            suppressed = await qualification.suppressed_registry_ids(
                context.manifest.vertical, [record.registry_id for record in pull.records]
            )
            selection = await service.refresh_pool_and_select(
                context.run_id,
                pull.records,
                size=size,
                currency_cutoff=self._clock().date() - CURRENCY_WINDOW,
                exclude=suppressed,
            )
            batch = [_as_pool_record(entry) for entry in selection.selected]

            evidence: dict[str, list[ProvenancedValue[SourceRecord]]] = {
                record.registry_id: [] for record in batch
            }
            found = not_found = failed = 0
            for lookup in bound.lookups:
                for record in batch:
                    try:
                        cited = await lookup.lookup(record)
                    except SourceAuthError as exc:
                        reason = f"{lookup.name}: {exc.message}"
                        if not self._allow_degraded:
                            # The run fails, so its batch goes back to the pool for a run that can
                            # gather the evidence.
                            raise self._refuse(
                                (reason,), when="this run's batch goes back to the backlog"
                            ) from exc
                        # The key will not get better on the next record. Stop asking this source.
                        reasons.append(reason)
                        logger.warning(
                            "tools.search_registry.source_abandoned",
                            source=lookup.name,
                            run_id=str(context.run_id),
                        )
                        break
                    if cited is None:
                        failed += 1
                    elif cited.value.rows:
                        found += 1
                    else:
                        not_found += 1
                    if cited is not None:
                        evidence[record.registry_id].append(cited)
            if failed:
                reasons.append(f"{failed} lookup(s) failed; those candidates lack that record")

            matched = 0
            for join in bound.joins:
                joined = await join.join(batch)
                for registry_id, cited in joined.items():
                    if registry_id in evidence:
                        evidence[registry_id].append(cited)
                        matched += 1 if cited.value.rows else 0

        candidates = [_with_evidence(record, evidence[record.registry_id]) for record in batch]
        disqualified = 0
        if candidates:
            recorded = await service.record_candidates(
                context.run_id, candidates, stage=PipelineStage.search_registry
            )
            run = await service.get_run(context.run_id)
            disqualified = await qualification.disqualify_batch(
                run,
                context.manifest,
                [(candidate, records_from(candidate.fields)) for candidate in recorded],
            )

        counts = {
            "pool_seen": selection.seen,
            "pool_new": selection.new,
            "pool_available": selection.available,
            "pool_rules_pushed_down": len(pull.pushed_down),
            "pool_rules_not_pushed_down": len(pull.not_pushed_down),
            "batched": len(batch),
            "lookup_found": found,
            "lookup_not_found": not_found,
            "lookup_failed": failed,
            "join_matched": matched,
            "pool_suppressed": len(suppressed),
            "qualification_disqualified": disqualified,
        }
        logger.info(
            "tools.search_registry.stage_completed",
            run_id=str(context.run_id),
            unbound=list(bound.unbound),
            degraded=reasons,
            **counts,
        )
        return StageResult(counts=counts, degraded_reason="; ".join(reasons) or None)


def _refuse_unevaluable_rules(context: StageContext, joins: tuple[str, ...]) -> None:
    """Raise before the pull if a predicate rule could never be judged fairly here.

    * Its operator and value do not fit (``check_shape``): better found now than after the batch's
      free lookups are spent.
    * It reads a **join** source (the revocations). A join repeats: a carrier may have several
      rows, and only a one-row source is a record a predicate can read, so the verdict would turn
      on the row count. Reading those rows is a judgment rule's job.
    """
    body = context.manifest.body
    for rule in predicate_rules(body):
        check_shape(rule)
        source = body.source_for(rule)
        if source in joins:
            raise RuleNotEvaluableError(
                f"rule {rule.id!r} is a predicate on {source!r}, a join source whose rows repeat "
                "per business — make it a judgment rule, or read a one-row source. Refused before "
                "the pull",
                rule_id=rule.id,
            )


def _refuse_misrouted_rules(
    context: StageContext, discovery: str, known_fields: frozenset[str] | None
) -> None:
    """Raise before any batch is taken if a predicate rule reads the discovery source for a field
    that source never holds.

    A rule with no ``source`` reads the manifest's first declared source. A QCMobile field left
    that way (freight v6's ``brokerAuthorityStatus``) would be absent from every census record,
    and an absent field fires a negated operator — so every candidate would be disqualified, and
    suppressed for the vertical for good. Refusing costs one pull; that would cost the backlog.
    """
    if known_fields is None:
        return
    body = context.manifest.body
    for cited in body.disqualifier_rules:
        rule = cited.value
        if rule.kind is not RuleKind.predicate or rule.field is None:
            continue
        if body.source_for(rule) == discovery and rule.field not in known_fields:
            raise RuleNotEvaluableError(
                f"rule {rule.id!r} reads {rule.field!r} from {discovery!r}, which has no such "
                "field — set the rule's `source` to the source that holds it (a new manifest "
                "version). Refused before taking a batch",
                rule_id=rule.id,
            )


def _as_pool_record(entry: PoolEntry) -> PoolRecord:
    """A batch entry back as the shape lookups and joins are keyed on.

    The native id is the registry id after its namespace (``usdot:7989`` → ``7989``). Ordering keys
    are not needed past selection.
    """
    _, _, native = entry.registry_id.partition(":")
    return PoolRecord(
        registry_id=entry.registry_id,
        native_id=native or entry.registry_id,
        fields=entry.fields,
        currency_date=None,
        has_principal=False,
    )


def _with_evidence(
    record: PoolRecord, gathered: Sequence[ProvenancedValue[SourceRecord]]
) -> CandidateFields:
    """The registry's fields plus every source's record, sorted by source name — same input, same
    stored bytes, whatever order the sources answered in."""
    records = sorted(
        (*record.fields.source_records, *gathered), key=lambda cited: cited.value.source
    )
    return CandidateFields.model_validate({**dict(record.fields), "source_records": tuple(records)})


STAGE = SearchRegistryStage()
