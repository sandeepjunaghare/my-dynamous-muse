"""The sourcing shapes: a brief, a run's outcome and totals, and a candidate with every field cited.

The rule running through :class:`CandidateFields` is the provenance write-gate, at rest. Each field
is a ``ProvenancedValue[T]`` or it is ``None`` — there is no third state holding a value nobody can
cite. ``None`` is what "unprovenanced" means here: the workbench may hold a candidate with absent
fields (T6 fills owner and headcount long after sourcing), and ``is_promotable`` refuses each one
until a citation exists. An uncited raw value would be the exact thing the primitive was built to
make unrepresentable.

**D13: Google Places is a check, never a source.** Its terms forbid storing Places content, so the
only thing a candidate keeps from Places is the place ID, in ``business_check``. Every other field
refuses a Google Maps citation, so copying Places content into a candidate fails validation rather
than reaching HubSpot.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.cost import BillableKind, RunCost
from app.manifests.schemas import SLUG_PATTERN, IcpBand
from app.shared.provenance import ProvenancedValue


class RunStatus(StrEnum):
    """Where a sourcing run is in its lifecycle."""

    running = "running"
    """Open. Candidates may be written into it; it has no finish time."""

    completed = "completed"
    """Finished normally."""

    degraded = "degraded"
    """Finished, but short of what it set out to do — e.g. the Places circuit breaker tripped (T6).

    Not a failure: the run keeps everything it already verified and says what it dropped.
    """

    failed = "failed"
    """Stopped by an error. Its counts and cost describe how far it got."""


class RunOutcome(StrEnum):
    """The terminal subset of :class:`RunStatus` — what a run may be *finished as*.

    A separate type so that finishing a run "as running" is a type error, not a runtime check.
    """

    completed = RunStatus.completed.value
    degraded = RunStatus.degraded.value
    failed = RunStatus.failed.value


REGISTRY_ID_MAX_LENGTH = 128
"""The ``candidate.registry_id`` column's width, enforced first by :class:`CandidateFields`."""

StageName = Annotated[str, Field(pattern=SLUG_PATTERN, max_length=64)]
"""A count's key — the pipeline stage that produced it, e.g. ``sourced`` or ``disqualified``."""

StageCount = Annotated[int, Field(ge=0)]


class SourcingBrief(BaseModel):
    """What a founder asks for: a vertical, an ICP band and a geography (PRD §6 step 1).

    The brief records what was *asked*. Reconciling its ICP band with the manifest's is the
    pipeline's business (T5/T7), not this shape's.
    """

    model_config = ConfigDict(frozen=True)

    vertical: str = Field(pattern=SLUG_PATTERN, max_length=64)
    geography: str = Field(pattern=SLUG_PATTERN, max_length=64)
    """A slug such as ``dfw``. Deliberately not an enum: hard-coding the one metro would be a branch
    on data, and multi-metro being a non-goal is a product decision, not a type."""

    icp_band: IcpBand


class PostalAddress(BaseModel):
    """A street address, cited as one unit — a source states an address, not four separate facts.

    Frozen because ``ProvenancedValue`` refuses to cite a value that could be edited afterwards.
    """

    model_config = ConfigDict(frozen=True)

    street: str = Field(min_length=1)
    city: str = Field(min_length=1)
    state: str = Field(min_length=1)
    postal_code: str = Field(min_length=1)


class PlaceCheck(BaseModel):
    """What a Google Places check keeps: the place ID, and nothing else (D13, option A).

    The terms let a place ID be stored indefinitely and bar storing the content behind it. Its
    citation's ``retrieved_at`` is when the check ran. "Verified" means Places found this business
    from the registry's name and address; it is not a claim that the two addresses match.
    """

    model_config = ConfigDict(frozen=True)

    place_id: str = Field(min_length=1)

    @field_validator("place_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("place_id must not be blank")
        return value.strip()


type SourceValue = str | int
"""One value as a source stated it: its text, or an integer the adapter cast (census numerics)."""


class SourceRow(BaseModel):
    """One record a source returned — a census row, a QCMobile carrier, a revocation — as pairs.

    Pairs rather than a dict because a citation may only hold immutable values, and **sorted by
    key** so the same record always serialises the same way: same input, same stored bytes.
    """

    model_config = ConfigDict(frozen=True)

    values: tuple[tuple[str, SourceValue], ...]

    @field_validator("values")
    @classmethod
    def _sorted_and_unique(
        cls, value: tuple[tuple[str, SourceValue], ...]
    ) -> tuple[tuple[str, SourceValue], ...]:
        keys = [key for key, _ in value]
        if any(not key.strip() for key in keys):
            raise ValueError("a source row key must not be blank")
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"duplicate source row keys: {duplicates}")
        return tuple(sorted(value, key=lambda pair: pair[0]))

    def get(self, key: str) -> SourceValue | None:
        """The value stated for ``key``, or ``None`` when the source did not state one."""
        for name, value in self.values:
            if name == key:
                return value
        return None


class SourceRecord(BaseModel):
    """What one declared source said about one candidate, as zero or more rows.

    ``source`` is the **manifest's** source name, so a rule's field can be traced to the source it
    came from (T7). ``rows == ()`` means *looked, found nothing* — no revocations, an unknown USDOT
    — which is a different fact from a record being absent altogether (never looked, or the lookup
    failed). An evaluator must not read the second as the first.
    """

    model_config = ConfigDict(frozen=True)

    source: str = Field(pattern=SLUG_PATTERN, max_length=64)
    rows: tuple[SourceRow, ...]


_GOOGLE_MAPS_HOSTS = frozenset(
    {"places.googleapis.com", "maps.googleapis.com", "maps.google.com", "maps.app.goo.gl"}
)


def _is_google_maps(url: str) -> bool:
    """Whether ``url`` points at Google Maps Platform content.

    Google *search* is not Maps: a website found by a web search may well be cited to google.com.
    Only Maps hosts, and the ``/maps`` paths of google.com and goo.gl, count.

    A tripwire for our own adapters, so it is lenient about form: a URL without a scheme is parsed
    as a host, and the host's case, port, userinfo and trailing dot are ignored. It is a deny-list
    of known Maps hosts, so a Maps hostname not on it would pass.
    """
    stripped = url.strip()
    parts = urlsplit(stripped if "//" in stripped else f"//{stripped}")
    host = (parts.hostname or "").rstrip(".")
    path = parts.path.lower()
    if host in _GOOGLE_MAPS_HOSTS:
        return True
    is_google = host == "google.com" or host.endswith(".google.com")
    return (is_google or host == "goo.gl") and (path == "/maps" or path.startswith("/maps/"))


class CandidateFields(BaseModel):
    """A sourced business's identity, every field carrying its own provenance.

    Stored whole as the ``candidate.fields`` JSONB. Only ``registry_id`` is required: a candidate
    exists *because* a registry named it, so its id is the one field that cannot be absent. Every
    other field may be ``None`` — storable, and not promotable (see the module docstring).

    Enrichment fields (owner, headcount band, years in business) are T6's to add here. Because the
    column is JSONB, adding a field is a schema change with no migration, and rows written before it
    existed load with it absent — which is honestly what they are.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    registry_id: ProvenancedValue[str]
    """The authoritative registry's own identifier for this business (e.g. a USDOT number)."""

    legal_name: ProvenancedValue[str] | None = None
    dba_name: ProvenancedValue[str] | None = None
    address: ProvenancedValue[PostalAddress] | None = None
    phone: ProvenancedValue[str] | None = None
    website: ProvenancedValue[str] | None = None
    business_check: ProvenancedValue[PlaceCheck] | None = None
    """The Google Places check of this record (T6): the place ID, cited to Places. Absent means
    never checked, or checked and not found."""

    source_records: tuple[ProvenancedValue[SourceRecord], ...] = ()
    """Every source's raw record for this candidate, each with its own citation (T5).

    The census row, the QCMobile carrier, the revocation rows. This is where T7's evaluator finds a
    rule's field, and where T6 finds ``company_officer_1`` and the email domain. One record per
    source. The typed fields above are what a person reads; these are what the rules read.
    """

    @field_validator("source_records")
    @classmethod
    def _one_record_per_source(
        cls, value: tuple[ProvenancedValue[SourceRecord], ...]
    ) -> tuple[ProvenancedValue[SourceRecord], ...]:
        """Two records for one source would make "what did fmcsa_qcmobile say?" ambiguous."""
        names = [record.value.source for record in value]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"more than one source record for: {duplicates}")
        return value

    @field_validator("registry_id")
    @classmethod
    def _normalise_registry_id(cls, value: ProvenancedValue[str]) -> ProvenancedValue[str]:
        """Strip the id, and refuse one that is blank or longer than its column.

        It is half the upsert key, so ``" 555 "`` and ``"555"`` must be the same key or one carrier
        becomes two rows. A blank id would collapse every blank record into one row. And an id the
        ``String(128)`` column cannot hold must fail *here*, as a ``ValidationError`` before any
        write, not as a ``DBAPIError`` that aborts the rest of an atomic batch.
        """
        stripped = value.value.strip()
        if not stripped:
            raise ValueError("registry_id must not be blank")
        if len(stripped) > REGISTRY_ID_MAX_LENGTH:
            raise ValueError(f"registry_id must be at most {REGISTRY_ID_MAX_LENGTH} characters")
        if stripped == value.value:
            return value
        # The citation is unchanged — the same source said the same thing, minus its padding.
        return value.model_copy(update={"value": stripped})

    @model_validator(mode="after")
    def _no_places_content(self) -> Self:
        """D13: only ``business_check`` may cite Google Maps; any other field doing so is copied
        Places content, which the Maps Platform Terms §3.2.3(a) forbid us to store."""
        for name in type(self).model_fields:
            if name == "business_check":
                continue
            for cited in self._citations(name):
                if _is_google_maps(cited.source_url):
                    raise ValueError(
                        f"{name} is cited to Google Maps ({cited.source_url}); Places content "
                        "cannot be stored (D13). Keep only the place ID, in business_check"
                    )
        return self

    def _citations(self, name: str) -> tuple[ProvenancedValue[object], ...]:
        """Every citation a field holds: none, one, or one per element of a tuple field."""
        held: object = getattr(self, name)
        if isinstance(held, ProvenancedValue):
            return (cast(ProvenancedValue[object], held),)
        if isinstance(held, tuple):
            return tuple(
                cast(ProvenancedValue[object], item)
                for item in cast(tuple[object, ...], held)
                if isinstance(item, ProvenancedValue)
            )
        return ()

    def to_stored(self) -> dict[str, object]:
        """The JSONB form: absent fields are absent keys — ``None`` *and* an empty tuple.

        The candidate upsert merges by key, so a key present means "this write states this field".
        ``exclude_none`` drops ``None``, but an empty ``source_records`` would still be written as
        ``[]`` — a key every stage's write carries, stating nothing.
        """
        dumped = self.model_dump(mode="json", exclude_none=True)
        return {name: value for name, value in dumped.items() if value != []}

    def has_verified_address(self) -> bool:
        """Whether this candidate has an address **and** a business check that found it.

        The rule T8 clusters on: a candidate without one is excluded, never geocoded from a guess.
        The address is the registry's copy either way (D13); verification is the separate fact
        that Places found the business, not a different address.
        """
        return self.address is not None and self.business_check is not None

    def unprovenanced_fields(self) -> tuple[str, ...]:
        """The fields with no citation, in declaration order.

        What T9's gate names when it refuses a write. Which of these are *required* for promotion is
        T9's decision, not this model's. ``source_records`` is never listed: a tuple is never
        ``None``, and it is evidence for the rules rather than a field a person reads.
        """
        return tuple(name for name in type(self).model_fields if getattr(self, name) is None)


class RunCostSummary(BaseModel):
    """What a run spent, per billable kind — the persisted form of a :class:`RunCost`.

    USD stays ``Decimal`` end to end; in JSONB it is a string, which is what keeps it exact.
    """

    calls: dict[BillableKind, int] = Field(default_factory=dict[BillableKind, int])
    usd: dict[BillableKind, Decimal] = Field(default_factory=dict[BillableKind, Decimal])

    @classmethod
    def from_run_cost(cls, cost: RunCost) -> Self:
        """Snapshot a run's accumulator. Every kind is present, so the stored shape is stable."""
        return cls(
            calls={kind: cost.total_calls(kind) for kind in BillableKind},
            usd={kind: cost.total_usd(kind) for kind in BillableKind},
        )

    def total_usd(self) -> Decimal:
        """Dollars across every kind. Computed, not stored — one fact, one home."""
        return sum(self.usd.values(), Decimal("0"))


class RunTotals(BaseModel):
    """What ``finish_run`` writes: validated stage counts and the cost snapshot."""

    counts: dict[StageName, StageCount]
    cost: RunCostSummary


class SourcingRunResponse(BaseModel):
    """One ``sourcing_run`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    manifest_id: UUID
    vertical: str
    geography: str
    icp_band: IcpBand
    status: RunStatus
    status_detail: str | None
    started_at: datetime
    finished_at: datetime | None
    counts: dict[str, int]
    cost: RunCostSummary


class CandidateResponse(BaseModel):
    """One ``candidate`` row, its fields validated back into citations."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    registry_id: str
    fields: CandidateFields
    created_at: datetime
    updated_at: datetime


class PoolEntry(BaseModel):
    """One backlog entry a run took into its batch: its key and the registry's cited fields."""

    model_config = ConfigDict(frozen=True)

    registry_id: str
    fields: CandidateFields


class PoolSelection(BaseModel):
    """What refreshing the backlog and taking a batch came to, for one run (D12)."""

    model_config = ConfigDict(frozen=True)

    seen: int = Field(ge=0)
    """Records the registry returned this run."""

    new: int = Field(ge=0)
    """Of those, entries the pool had never held."""

    available: int = Field(ge=0)
    """Entries this run could take, before it took any."""

    selected: tuple[PoolEntry, ...]
    """The batch, best first."""
