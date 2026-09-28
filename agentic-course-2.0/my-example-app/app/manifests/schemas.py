"""The vertical manifest schema — what differs between one vertical and the next, as data.

Everything the pipeline needs to know about a vertical lives here: which source is authoritative,
what disqualifies a candidate, what qualifies one, the ICP headcount band, and the vocabulary to
speak in. Five generic stages read this; none of them knows what "freight" means.

Two shape rules run through the whole file, and both come from ``ProvenancedValue``:

* **Every researched field is wrapped in a citation.** A manifest field nobody can cite cannot be
  represented, which is the same write-gate the prospect fields carry (E18 → M6/M8).
* **Everything inside a citation is immutable.** ``ProvenancedValue._assert_immutable`` rejects any
  ``list``, ``dict``, ``set`` or non-frozen model, so every collection below is a ``tuple`` and
  every leaf model sets ``frozen=True``. A citation for an editable value is not a citation.

Terms-of-use decisions deliberately sit *outside* the citations. A citation says when and how a
value was obtained; ``activate`` does not re-obtain the source declaration, it records a human's
decision about it. Were the decision stored inside the cited value, activation would have to build
a new ``ProvenancedValue`` with a fresh ``retrieved_at`` — and the manifest would then claim
FMCSA's base URL was "retrieved" at the moment someone typed ``--accept-terms``.
"""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.shared.provenance import ProvenancedValue

SLUG_PATTERN = r"^[a-z][a-z0-9_]*$"
"""Identifiers a person types on the CLI (``--accept-terms fmcsa,places``) — lowercase slugs."""


class SourceKind(StrEnum):
    """How a declared source is read.

    Deliberately parallel to :class:`~app.shared.provenance.RetrievalMethod`'s first three members:
    the stage that pulls a source records the matching retrieval method on every field it writes.
    """

    registry_api = "registry_api"
    """A keyed lookup against an authoritative registry (e.g. FMCSA QCMobile)."""

    bulk_file = "bulk_file"
    """A published bulk dataset (e.g. the FMCSA Company Census File)."""

    web_lookup = "web_lookup"
    """A paid or public per-request web/API lookup (e.g. Google Places)."""


class RuleKind(StrEnum):
    """Whether a disqualifier rule is mechanical or needs judgment."""

    predicate = "predicate"
    """Mechanical and free — a field, an operator and a value, evaluated by T7's engine."""

    judgment = "judgment"
    """Delegated to the ``classify_rollup`` Agent SDK node.

    "National rollup with no local owner" is the canonical case, and the whole reason that node
    exists: E7's rule lives in a founder's head, not in a field comparison.
    """


class RuleOperator(StrEnum):
    """The comparisons a predicate rule may make.

    Kept small on purpose. T7 owns the evaluator and will discover what the rules actually need
    against real candidates; adding a member then is a one-line change, while a rule DSL designed
    in advance against two hand-written examples fits neither vertical.
    """

    equals = "equals"
    not_equals = "not_equals"
    contains = "contains"
    in_set = "in_set"
    greater_than = "greater_than"
    less_than = "less_than"
    is_true = "is_true"


class ScoreAxis(StrEnum):
    """Which axis of the priority score a qualifying signal feeds (E9: 1-5 by 1-5 = 1-25)."""

    intensity = "intensity"
    """How painful the problem is for this business."""

    automatable = "automatable"
    """How tractable the problem is to automate."""


class TermsDecision(StrEnum):
    """A human's recorded decision about one source's terms of use.

    There is no ``undecided`` member: undecided is the *absence* of a record, so a ``None`` can
    never be mistaken for a decision somebody made.
    """

    accepted = "accepted"
    """A person read the terms and accepted them. Required before a manifest can go ACTIVE."""

    rejected = "rejected"
    """A person read the terms and refused them. The source stays declared but unusable."""


class ManifestStatus(StrEnum):
    """A manifest row's lifecycle position."""

    draft = "draft"
    """Proposed — by the authoring agent (T12) or by hand. Never returned by ``get_active``."""

    active = "active"
    """Accepted by a human on the CLI, with a terms decision recorded for every declared source."""

    superseded = "superseded"
    """Replaced by a later version that went active. Retained, never overwritten (AC6)."""


class ManifestSource(BaseModel):
    """One authoritative source a vertical's pipeline may read.

    Carries no terms-of-use field (see the module docstring) and no per-run call cap — the cap is
    ``settings.max_places_calls_per_run``, and one fact gets one home.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=SLUG_PATTERN, max_length=64)
    """Stable slug — typed by hand at ``--accept-terms``, so ``fmcsa``, not ``FMCSA (SAFER)``."""

    kind: SourceKind
    description: str
    base_url: str
    rate_limit_per_minute: int | None = Field(default=None, gt=0)
    """What the source publishes, when it publishes one. ``None`` means "not documented"."""


class DisqualifierRule(BaseModel):
    """A rule that removes a candidate from consideration.

    T2 defines the shape; **T7 builds the evaluator**. There is deliberately no ``matches()`` here.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=SLUG_PATTERN, max_length=64)
    kind: RuleKind
    description: str
    field: str | None = None
    operator: RuleOperator | None = None
    value: str | tuple[str, ...] | int | bool | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "DisqualifierRule":
        """A predicate needs something to compare; a judgment rule must not pretend to be one."""
        if self.kind is RuleKind.predicate and (self.field is None or self.operator is None):
            raise ValueError(
                f"rule {self.id!r}: a predicate rule needs both `field` and `operator`"
            )
        if self.kind is RuleKind.judgment and (self.field is not None or self.operator is not None):
            raise ValueError(
                f"rule {self.id!r}: a judgment rule is decided by the classify_rollup node, so it "
                "must carry neither `field` nor `operator`"
            )
        return self


class QualifyingSignal(BaseModel):
    """Something to find out about a candidate, and which score axis the answer feeds."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=SLUG_PATTERN, max_length=64)
    question: str
    """The literal thing to ask, e.g. "which TMS, and does it have an API"."""

    feeds: ScoreAxis


class IcpBand(BaseModel):
    """The headcount band that defines "the right size of business" for one vertical."""

    model_config = ConfigDict(frozen=True)

    headcount_min: int = Field(ge=1)
    headcount_max: int = Field(ge=1)
    requires_office_function: bool

    @model_validator(mode="after")
    def _band_is_ordered(self) -> "IcpBand":
        """An inverted band silently matches nothing, which reads as "the source is empty"."""
        if self.headcount_min > self.headcount_max:
            raise ValueError(
                f"icp_band: headcount_min ({self.headcount_min}) must not exceed "
                f"headcount_max ({self.headcount_max})"
            )
        return self


class Vocabulary(BaseModel):
    """The words this vertical uses about its own work — what outreach has to sound like."""

    model_config = ConfigDict(frozen=True)

    terms: tuple[str, ...]

    @field_validator("terms")
    @classmethod
    def _reject_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """An empty vocabulary is a manifest that has not been researched."""
        if not value:
            raise ValueError("vocabulary.terms must not be empty")
        return value


class SourceTerms(BaseModel):
    """A human's recorded decision about one declared source's terms of use.

    Not a citation, and not wrapped in one: this records what a person *decided*, not what a
    machine *retrieved*. ``decided_by`` and ``decided_at`` are its provenance.
    """

    model_config = ConfigDict(frozen=True)

    source_name: str = Field(pattern=SLUG_PATTERN, max_length=64)
    decision: TermsDecision
    license: str | None = None
    """The licence the source publishes under, when it names one (FMCSA is CC PDM 1.0)."""

    decided_by: str = Field(min_length=1, max_length=128)
    decided_at: datetime
    note: str | None = None

    @field_validator("decided_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Mirror ``ProvenancedValue.retrieved_at``: tz-aware in, UTC out.

        Two decisions recorded in different zones would otherwise compare in an undefined order,
        and "which decision is current" is exactly the question this field has to answer.
        """
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("decided_at must be timezone-aware")
        return value.astimezone(UTC)


class ManifestBody(BaseModel):
    """The cited content of one manifest version — everything stored in the JSONB ``body``.

    Identity and lifecycle (``vertical``, ``version``, ``status``) are real columns on the row, not
    fields here: they are what the database indexes and constrains. The body is read whole, and
    nothing queries inside it.
    """

    model_config = ConfigDict(frozen=True)

    sources: tuple[ProvenancedValue[ManifestSource], ...]
    disqualifier_rules: tuple[ProvenancedValue[DisqualifierRule], ...] = ()
    qualifying_signals: tuple[ProvenancedValue[QualifyingSignal], ...] = ()
    icp_band: ProvenancedValue[IcpBand]
    vocabulary: ProvenancedValue[Vocabulary]
    terms: tuple[SourceTerms, ...] = ()
    """Human decisions, one per source at most. Empty on a freshly authored DRAFT."""

    @model_validator(mode="after")
    def _sources_are_declared_and_unique(self) -> "ManifestBody":
        """A manifest with no source, or two sources of one name, cannot be sourced against."""
        if not self.sources:
            raise ValueError("a manifest must declare at least one source")

        names = [source.value.name for source in self.sources]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate source names: {duplicates}")
        return self

    @model_validator(mode="after")
    def _ids_are_unique(self) -> "ManifestBody":
        """Two rules or signals sharing an id make the second unaddressable by T7."""
        for label, ids in (
            ("disqualifier rule", [rule.value.id for rule in self.disqualifier_rules]),
            ("qualifying signal", [signal.value.id for signal in self.qualifying_signals]),
        ):
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            if duplicates:
                raise ValueError(f"duplicate {label} ids: {duplicates}")
        return self

    @model_validator(mode="after")
    def _terms_match_declared_sources(self) -> "ManifestBody":
        """No orphan decisions, and no source decided twice.

        An orphan is how a source gets dropped from a later version while its acceptance lingers,
        which would let a manifest look fully decided when it is not.
        """
        declared = {source.value.name for source in self.sources}
        decided = [entry.source_name for entry in self.terms]

        orphans = sorted(set(decided) - declared)
        if orphans:
            raise ValueError(f"terms recorded for undeclared source(s): {orphans}")

        duplicates = sorted({name for name in decided if decided.count(name) > 1})
        if duplicates:
            raise ValueError(f"more than one terms decision for source(s): {duplicates}")
        return self

    def source_names(self) -> tuple[str, ...]:
        """Every declared source name, in declaration order."""
        return tuple(source.value.name for source in self.sources)

    def undecided_sources(self) -> tuple[str, ...]:
        """Declared sources with no **accepted** terms decision — what blocks activation.

        A ``rejected`` decision counts as undecided for this purpose: the question was asked and
        the answer was no, so the manifest still may not go active while it declares that source.
        """
        accepted = {
            entry.source_name for entry in self.terms if entry.decision is TermsDecision.accepted
        }
        return tuple(name for name in self.source_names() if name not in accepted)


class ManifestResponse(BaseModel):
    """One manifest row as the API and the CLI render it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    vertical: str
    version: int
    status: ManifestStatus
    body: ManifestBody
    created_at: datetime
    activated_at: datetime | None
    activated_by: str | None
