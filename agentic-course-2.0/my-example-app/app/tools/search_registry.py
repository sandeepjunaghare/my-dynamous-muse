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

It decides nothing. ``allowToOperate`` and the revocations are *evidence*, stored for T7's rule
evaluator; no candidate is dropped after the pool. Lookups that fail, or a source that cannot run
here (no webKey), leave the candidate without that source's record and the run ``degraded``.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.shared.provenance import ProvenancedValue
from app.sourcing.exceptions import SourceAuthError
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
    ) -> None:
        self._factories = tuple(factories)
        self._transport = transport
        self._clock = clock
        self._batch_size = batch_size
        self._backoff_seconds = backoff_seconds
        self._throttle = throttle

    @property
    def stage(self) -> PipelineStage:
        return PipelineStage.search_registry

    def with_batch_size(self, batch_size: int) -> "SearchRegistryStage":
        """This stage, configured identically, taking ``batch_size`` candidates a run."""
        return SearchRegistryStage(
            factories=self._factories,
            transport=self._transport,
            clock=self._clock,
            batch_size=batch_size,
            backoff_seconds=self._backoff_seconds,
            throttle=self._throttle,
        )

    async def run(self, context: StageContext) -> StageResult:
        """Pull, refresh, take a batch, gather the free evidence, record (see the module doc)."""
        settings = get_settings()
        size = self._batch_size or settings.sourcing_batch_size
        service = SourcingService(context.session)
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
            reasons.extend(bound.degraded)

            rules = [cited.value for cited in context.manifest.body.disqualifier_rules]
            pull = await bound.discovery.pull(rules)
            selection = await service.refresh_pool_and_select(
                context.run_id,
                pull.records,
                size=size,
                currency_cutoff=self._clock().date() - CURRENCY_WINDOW,
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
                        # The key will not get better on the next record. Stop asking this source.
                        reasons.append(f"{lookup.name}: {exc.message}")
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
        if candidates:
            await service.record_candidates(
                context.run_id, candidates, stage=PipelineStage.search_registry
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
        }
        logger.info(
            "tools.search_registry.stage_completed",
            run_id=str(context.run_id),
            unbound=list(bound.unbound),
            degraded=reasons,
            **counts,
        )
        return StageResult(counts=counts, degraded_reason="; ".join(reasons) or None)


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
