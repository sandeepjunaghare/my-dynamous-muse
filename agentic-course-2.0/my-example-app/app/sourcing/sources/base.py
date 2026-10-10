"""What a source adapter is, and how a manifest's declared sources are bound to adapters.

**Three roles, one per way a source is read** (T5):

* a **discovery** source enumerates the pool — the one registry a manifest names (the FMCSA
  census). Exactly one per manifest; Places is never one (D13 rule 3).
* a **lookup** source is asked about one record at a time (QCMobile, keyed on the USDOT number).
* a **join** source is asked about the whole batch at once (the revocations dataset).

**Binding is by the source's ``base_url``, never by vertical and never by source name.** A source
name is a per-manifest slug — freight v1 called FMCSA ``fmcsa``, v3 calls it
``fmcsa_company_census`` — while the dataset URL is the source's identity. A declared source no
adapter matches is *unbound*: logged and skipped. That is how a manifest can declare Google Places
and have it never discover anything, and how the frozen legacy revocations file stays unused.

The adapter table is passed in, so a test binds a stub to the fire manifest's registry URL and the
same stage sources fire with no code change (M9).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.manifests.schemas import DisqualifierRule, ManifestResponse, ManifestSource
from app.shared.provenance import ProvenancedValue
from app.sourcing.exceptions import NoRegistrySourceError
from app.sourcing.schemas import CandidateFields, SourceRecord
from app.sourcing.sources import http

logger = get_logger(__name__)

_SOCRATA_PATH = re.compile(r"^/(?:d|resource|api/views)/([a-z0-9]{4}-[a-z0-9]{4})(?:\.json)?/?$")


@dataclass(frozen=True, kw_only=True)
class PoolRecord:
    """One record a discovery source returned, ready for the pool.

    ``currency_date`` and ``has_principal`` are the generic batch-ordering keys (D12), named for
    what they mean rather than for any one registry's columns: when the record was last refreshed
    by its owner, and whether it names a principal. Each discovery adapter maps its own fields
    onto them (FMCSA: ``mcs150_date``, ``company_officer_1``).
    """

    registry_id: str
    """Namespaced, e.g. ``usdot:7989`` — the pool's and the candidate's key."""

    native_id: str
    """The source's own key, unprefixed (``7989``) — what a lookup or join is keyed on."""

    fields: CandidateFields
    currency_date: date | None
    has_principal: bool


@dataclass(frozen=True, kw_only=True)
class DiscoveryPull:
    """A discovery source's answer: the records, and which rules it applied at the source."""

    records: tuple[PoolRecord, ...]
    """Deduplicated by ``registry_id`` and sorted by it, so the same pull always reads the same."""

    pushed_down: tuple[str, ...] = ()
    """Ids of the manifest rules applied in the source's own query."""

    not_pushed_down: tuple[str, ...] = ()
    """Predicate rules on fields this source holds that it could not express — left for T7."""


class DiscoverySource(Protocol):
    """Enumerates the pool from the manifest's one registry."""

    @property
    def name(self) -> str:
        """The manifest source name this adapter is bound to."""
        ...

    async def pull(self, rules: Sequence[DisqualifierRule]) -> DiscoveryPull:
        """Every record that survives the rules this source can apply itself."""
        ...


class LookupSource(Protocol):
    """Answers for one record at a time."""

    @property
    def name(self) -> str:
        """The manifest source name this adapter is bound to."""
        ...

    async def lookup(self, record: PoolRecord) -> ProvenancedValue[SourceRecord] | None:
        """The source's record for ``record``.

        ``rows=()`` means the source does not know it. ``None`` means the lookup failed — the
        caller counts it and the candidate goes on without this source's record.
        """
        ...


class JoinSource(Protocol):
    """Answers for a whole batch at once."""

    @property
    def name(self) -> str:
        """The manifest source name this adapter is bound to."""
        ...

    async def join(
        self, records: Sequence[PoolRecord]
    ) -> Mapping[str, ProvenancedValue[SourceRecord]]:
        """One record per requested ``registry_id`` — ``rows=()`` where nothing matched."""
        ...


@dataclass(frozen=True, kw_only=True)
class SourceEnv:
    """What every adapter is built with: one HTTP client for the run, settings, and a clock.

    The clock stamps every citation's ``retrieved_at``, so a test that pins it gets byte-identical
    candidates from identical input. ``backoff_seconds`` and ``throttle`` exist for the same
    reason: a test must not sleep through retries and pacing to prove them.
    """

    client: httpx.AsyncClient
    settings: Settings
    clock: Callable[[], datetime]
    backoff_seconds: float = http.DEFAULT_BACKOFF_SECONDS
    throttle: bool = True


type SourceMatcher = Callable[[ManifestSource], bool]
type Unavailability = Callable[[Settings], str | None]


def _always_available(settings: Settings) -> str | None:
    return None


@dataclass(frozen=True, kw_only=True)
class DiscoveryFactory:
    """Builds a discovery adapter for a declared source it recognises."""

    matches: SourceMatcher
    build: Callable[[str, SourceEnv], DiscoverySource]
    unavailable: Unavailability = _always_available
    """A reason this adapter cannot run in this environment (a missing key), or ``None``."""


@dataclass(frozen=True, kw_only=True)
class LookupFactory:
    """Builds a lookup adapter for a declared source it recognises."""

    matches: SourceMatcher
    build: Callable[[str, SourceEnv], LookupSource]
    unavailable: Unavailability = _always_available


@dataclass(frozen=True, kw_only=True)
class JoinFactory:
    """Builds a join adapter for a declared source it recognises."""

    matches: SourceMatcher
    build: Callable[[str, SourceEnv], JoinSource]
    unavailable: Unavailability = _always_available


type AdapterFactory = DiscoveryFactory | LookupFactory | JoinFactory


@dataclass(frozen=True, kw_only=True)
class BoundSources:
    """A manifest's declared sources, each bound to an adapter or set aside with a reason."""

    discovery: DiscoverySource
    lookups: tuple[LookupSource, ...] = ()
    joins: tuple[JoinSource, ...] = ()
    unbound: tuple[str, ...] = ()
    """Declared sources no adapter recognises. Not an error: Places, say, is never a discovery
    source, and a source this stage has no use for is simply not read."""

    degraded: tuple[str, ...] = field(default=())
    """``"<source>: <reason>"`` for each recognised source that cannot run here (a missing key).
    The run still sources, short of that source's evidence, and finishes ``degraded``."""


def normalise_dataset_url(url: str) -> str:
    """A comparable identity for a source URL: ``host/<id>`` for Socrata, else ``host/path``.

    A Socrata dataset is the same dataset whether a manifest cites its landing page (``/d/<id>``),
    its API (``/resource/<id>.json``) or its metadata (``/api/views/<id>``). The host's case and a
    trailing slash never matter.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").rstrip(".").lower()
    path = parts.path
    socrata = _SOCRATA_PATH.match(path.lower())
    if socrata is not None:
        return f"{host}/{socrata.group(1)}"
    return f"{host}{path.rstrip('/')}"


def dataset_matcher(identity: str) -> SourceMatcher:
    """Match a declared source whose ``base_url`` normalises to exactly ``identity``."""

    def matches(source: ManifestSource) -> bool:
        return normalise_dataset_url(source.base_url) == identity

    return matches


def prefix_matcher(prefix: str) -> SourceMatcher:
    """Match a declared source whose normalised ``base_url`` is ``prefix`` or sits under it."""

    def matches(source: ManifestSource) -> bool:
        identity = normalise_dataset_url(source.base_url)
        return identity == prefix or identity.startswith(f"{prefix}/")

    return matches


def bind_sources(
    manifest: ManifestResponse,
    factories: Sequence[AdapterFactory],
    env: SourceEnv,
) -> BoundSources:
    """Bind every declared source, in declaration order, to the first factory that matches it.

    Raises :class:`NoRegistrySourceError` unless exactly one discovery source binds — a recognised
    discovery source that is merely *unavailable* still counts against that, because a run without
    its registry has nothing to source from.
    """
    discovery: list[DiscoverySource] = []
    lookups: list[LookupSource] = []
    joins: list[JoinSource] = []
    unbound: list[str] = []
    degraded: list[str] = []

    for cited in manifest.body.sources:
        source = cited.value
        factory = next((candidate for candidate in factories if candidate.matches(source)), None)
        if factory is None:
            unbound.append(source.name)
            logger.info(
                "sourcing.sources.source_unbound",
                source=source.name,
                base_url=source.base_url,
            )
            continue

        reason = factory.unavailable(env.settings)
        if reason is not None:
            degraded.append(f"{source.name}: {reason}")
            logger.warning("sourcing.sources.source_unavailable", source=source.name, reason=reason)
            continue

        match factory:
            case DiscoveryFactory():
                discovery.append(factory.build(source.name, env))
            case LookupFactory():
                lookups.append(factory.build(source.name, env))
            case JoinFactory():
                joins.append(factory.build(source.name, env))
        logger.info(
            "sourcing.sources.source_bound", source=source.name, role=type(factory).__name__
        )

    if len(discovery) != 1:
        bound = tuple(adapter.name for adapter in discovery)
        logger.warning(
            "sourcing.sources.discovery_unresolved",
            manifest_id=str(manifest.id),
            bound=list(bound),
            degraded=degraded,
        )
        raise NoRegistrySourceError(
            f"manifest {manifest.id} ({manifest.vertical} v{manifest.version}) must bind exactly "
            f"one discovery source, and binds {len(discovery)}"
            + (f" ({', '.join(bound)})" if bound else "")
            + (f"; unavailable: {'; '.join(degraded)}" if degraded else "")
            + " — declare exactly one registry this stage has an adapter for",
            manifest_id=str(manifest.id),
            bound=bound,
        )

    return BoundSources(
        discovery=discovery[0],
        lookups=tuple(lookups),
        joins=tuple(joins),
        unbound=tuple(unbound),
        degraded=tuple(degraded),
    )
