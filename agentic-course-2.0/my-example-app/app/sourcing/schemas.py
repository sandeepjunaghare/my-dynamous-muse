"""The sourcing shapes: a brief, a run's outcome and totals, and a candidate with every field cited.

The rule running through :class:`CandidateFields` is the provenance write-gate, at rest. Each field
is a ``ProvenancedValue[T]`` or it is ``None`` — there is no third state holding a value nobody can
cite. ``None`` is what "unprovenanced" means here: the workbench may hold a candidate with absent
fields (T6 fills owner and headcount long after sourcing), and ``is_promotable`` refuses each one
until a citation exists. An uncited raw value would be the exact thing the primitive was built to
make unrepresentable.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("registry_id")
    @classmethod
    def _reject_blank_registry_id(cls, value: ProvenancedValue[str]) -> ProvenancedValue[str]:
        """A blank id cannot key an upsert, and would collapse every blank record into one row."""
        if not value.value.strip():
            raise ValueError("registry_id must not be blank")
        return value

    def unprovenanced_fields(self) -> tuple[str, ...]:
        """The fields with no citation, in declaration order.

        What T9's gate names when it refuses a write. Which of these are *required* for promotion is
        T9's decision, not this model's.
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
