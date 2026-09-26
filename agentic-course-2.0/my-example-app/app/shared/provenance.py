"""The provenance primitive — the write-gate expressed as a type.

Every prospect field carries the URL it came from, when it was retrieved, and how. A field nobody
can cite is exactly the field that should never have been written (E18: 14 of 34 fire-vertical
contacts carry a company name in the first-name field). Making provenance a required part of the
type means the failure mode moves from "wrong data in HubSpot" to "code that does not type-check".

Lives in ``shared/`` on day one rather than in a slice because it already has three known
consumers — sourcing, qualification and promotion — which is the three-feature rule's bar.

This module imports nothing from ``app.core``; it is pure domain vocabulary.
"""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


class RetrievalMethod(StrEnum):
    """How a value was obtained.

    A ``StrEnum`` rather than a bare string so a typo cannot pass silently — the members are part
    of the contract, not a documentation convention.
    """

    registry_api = "registry_api"
    """A keyed lookup against an authoritative registry (e.g. FMCSA QCMobile)."""

    bulk_file = "bulk_file"
    """A row from a published bulk dataset (e.g. the FMCSA Company Census File)."""

    web_lookup = "web_lookup"
    """A paid or public per-request web/API lookup (e.g. Google Places)."""

    llm_inference = "llm_inference"
    """A model's judgment at one of the two judgment nodes, citing what it read."""

    manual_hubspot_entry = "manual_hubspot_entry"
    """Hand-typed into HubSpot by a person, adopted as-is.

    T13 depends on this member existing. Adopted records are not exempt from the gate — they carry
    honest provenance saying the data was hand-typed and is not independently verifiable. Marking
    data unverifiable is as much this primitive's job as certifying it, so do not remove this
    member as unused.
    """


class ProvenancedValue[T](BaseModel):
    """A value that knows where it came from.

    All three provenance fields are required: constructing one without any of them raises
    ``ValidationError``, so an unprovenanced value cannot exist as a ``ProvenancedValue`` at all.

    The model is **frozen**. A citation is a fact about how a value was obtained, so re-citing it
    means obtaining it again — a caller needing a new citation constructs a new value rather than
    mutating one.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    """The value itself. Validated against ``T``, so ``ProvenancedValue[int]`` rejects a string."""

    source_url: str
    """Where the value came from. Never blank — a blank source is no source."""

    retrieved_at: datetime
    """When it was retrieved. Timezone-aware, normalised to UTC."""

    retrieval_method: RetrievalMethod
    """How it was retrieved."""

    @field_validator("source_url")
    @classmethod
    def _reject_blank_source(cls, value: str) -> str:
        """Reject an empty or whitespace-only source URL."""
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "source_url must not be blank — a field without a source is not provenanced"
            )
        return stripped

    @field_validator("retrieved_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Reject naive datetimes and normalise everything else to UTC.

        Without this, timestamps from two sources compare in an undefined order and the system
        silently mis-orders which citation is newer.
        """
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(UTC)


def is_promotable[T](value: ProvenancedValue[T] | None) -> bool:
    """Whether a field may be written to HubSpot.

    **The gate governs prospect field writes, not task creation.** A candidate with zero citable
    fields still gets its cadence tasks — T13 adopts exactly such records, and a gate that refused
    them would refuse the 22 prospects the whole adoption ticket exists to rescue. What the gate
    refuses is writing a *prospect field* nobody can cite.

    The predicate is deliberately thin: by the time a ``ProvenancedValue`` exists, construction has
    already enforced that its source, timestamp and method are present and well-formed. All that is
    left to ask is whether there is a provenanced value at all.
    """
    return value is not None
