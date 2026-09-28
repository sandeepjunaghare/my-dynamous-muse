"""The provenance primitive — the write-gate expressed as a type.

Every prospect field carries the URL it came from, when it was retrieved, and how. A field nobody
can cite is exactly the field that should never have been written (E18: 14 of 34 fire-vertical
contacts carry a company name in the first-name field). Making provenance a required part of the
type means the failure mode moves from "wrong data in HubSpot" to "code that does not type-check".

Lives in ``shared/`` on day one rather than in a slice because it already has three known
consumers — sourcing, qualification and promotion — which is the three-feature rule's bar.

This module imports nothing from ``app.core``; it is pure domain vocabulary.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_IMMUTABLE_SCALARS = (str, bytes, bool, int, float, Decimal, datetime, date, UUID, Enum, type(None))


def _assert_immutable(value: object, path: str, seen: set[int]) -> None:
    """Raise unless ``value`` cannot be edited after it has been cited.

    ``frozen=True`` on the model only stops its own fields being reassigned. It says nothing about a
    mutable ``value``, so without this a caller could construct a ProvenancedValue, have
    :func:`is_promotable` approve it, and then change the very thing that was cited — which is E18
    happening again through the primitive built to prevent it.
    """
    if isinstance(value, _IMMUTABLE_SCALARS):
        return

    # A cycle means we have already vouched for this object further up the walk.
    if id(value) in seen:
        return
    seen.add(id(value))

    if isinstance(value, tuple | frozenset):
        # Narrowing an `object` to `tuple`/`frozenset` leaves the element type unknown, which
        # Pyright strict rejects. The cast states what is already true — any tuple or frozenset is
        # an iterable of objects — without reaching for a suppression or for `Any`.
        for index, item in enumerate(cast(Iterable[object], value)):
            _assert_immutable(item, f"{path}[{index}]", seen)
        return

    if isinstance(value, BaseModel):
        if not value.model_config.get("frozen", False):
            raise ValueError(
                f"{path}: {type(value).__name__} must set model_config = ConfigDict(frozen=True) "
                "to be provenanced — a citation for an editable value is not a citation"
            )
        for field_name in type(value).model_fields:
            _assert_immutable(getattr(value, field_name), f"{path}.{field_name}", seen)
        return

    raise ValueError(
        f"{path}: {type(value).__name__} is mutable and cannot be provenanced — "
        "use an immutable equivalent (tuple, frozenset, or a frozen model)"
    )


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

    manual_research = "manual_research"
    """A person read a public source and recorded the finding by hand.

    What the seeded freight and fire manifests carry: a human read FMCSA's and the Texas Fire
    Marshal's documentation and wrote the row. T12's authoring agent cites ``llm_inference``
    instead. ``web_lookup`` would be the small lie this primitive exists to prevent — it is defined
    as a *per-request* lookup, which a hand-written row is not.
    """


class ProvenancedValue[T](BaseModel):
    """A value that knows where it came from.

    All three provenance fields are required: constructing one without any of them raises
    ``ValidationError``, so an unprovenanced value cannot exist as a ``ProvenancedValue`` at all.

    The model is **frozen**, and so must ``value`` be. A citation is a fact about how a value was
    obtained, so re-citing it means obtaining it again — a caller needing a new citation constructs
    a new value rather than mutating one.

    Pydantic's ``frozen=True`` only stops *this* model's fields being reassigned; it would happily
    hold a mutable ``T`` whose contents could change after the gate approved them. So ``T`` is
    checked at construction: scalars, tuples and frozensets are fine, and a nested ``BaseModel``
    must itself declare ``frozen=True``. A list, dict, set or unfrozen model is rejected outright
    with a message naming the offending field.
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

    @model_validator(mode="after")
    def _require_immutable_value(self) -> "ProvenancedValue[T]":
        """Reject a ``value`` that could be edited after this citation was made."""
        _assert_immutable(self.value, "value", set())
        return self


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
