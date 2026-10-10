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

SLUG_MAX_LENGTH = 64
"""The longest slug the ``vertical_manifest.vertical`` column (``String(64)``) can hold."""


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
    not_contains = "not_contains"
    """Exclude by absence. A code field that combines values (FMCSA's ``carship``: ``C;B``) can
    otherwise only be negated by listing every combination — which misses the ones nobody listed."""
    in_set = "in_set"
    not_in_set = "not_in_set"
    """Exclude everything outside a named set — "not in the eleven DFW counties". ``in_set`` alone
    would remove the very candidates the set names."""
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
    source: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=64)
    """Which declared source a predicate's ``field`` is read from. ``None`` means the manifest's
    first declared source — which is what every row written before this field existed meant.

    Needed because one vertical reads several sources: FMCSA's ``allowToOperate`` is a QCMobile
    field, not a census column, and a join (the revocations dataset) arrives as a field on its own
    declared source rather than as an operator the rule language would need to grow."""

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "DisqualifierRule":
        """A predicate needs something to compare; a judgment rule must not pretend to be one."""
        if self.kind is RuleKind.predicate and (self.field is None or self.operator is None):
            raise ValueError(
                f"rule {self.id!r}: a predicate rule needs both `field` and `operator`"
            )
        if self.kind is RuleKind.judgment and (
            self.field is not None or self.operator is not None or self.source is not None
        ):
            raise ValueError(
                f"rule {self.id!r}: a judgment rule is decided by the classify_rollup node, so it "
                "must carry neither `field`, `operator` nor `source`"
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
    def _rule_sources_are_declared(self) -> "ManifestBody":
        """A predicate reading a source the manifest does not declare can never be evaluated."""
        declared = set(self.source_names())
        unknown = sorted(
            {
                rule.value.source
                for rule in self.disqualifier_rules
                if rule.value.source is not None and rule.value.source not in declared
            }
        )
        if unknown:
            raise ValueError(f"disqualifier rules read undeclared source(s): {unknown}")
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

    def source_for(self, rule: DisqualifierRule) -> str:
        """The declared source a rule's field is read from: its own, or the first declared."""
        return rule.source if rule.source is not None else self.sources[0].value.name

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


class DryRunFlag(StrEnum):
    """Something a dry-run found suspicious about one rule, read by a person before ``activate``.

    Each one is a failure the hand check caught on freight v1 or v2, made mechanical.
    """

    matches_nothing = "matches_nothing"
    """The rule fires on no row at all, even standalone — v1's ``'asset_based'`` against A/B/C."""

    excludes_nothing = "excludes_nothing"
    """The rule fires on some rows, but every one was already removed by an earlier rule."""

    excludes_everything = "excludes_everything"
    """Nothing survives this rule. A filter that empties the pool is almost never intended."""

    unknown_field = "unknown_field"
    """The rule's field is not a column of the source file — it can never be evaluated."""

    value_not_seen = "value_not_seen"
    """An ``equals``/``in_set``/``contains`` literal never appears in the data (the v1 check)."""

    not_evaluable_offline = "not_evaluable_offline"
    """The rule reads a source with no file in this dry-run (a per-record API, e.g. QCMobile)."""

    malformed = "malformed"
    """Operator and value do not fit (``in_set`` with a scalar, a numeric compare with text)."""


class DryRunSample(BaseModel):
    """One row, reduced to its identifier and the fields the rules read."""

    model_config = ConfigDict(frozen=True)

    row_id: str
    fields: tuple[tuple[str, str], ...]


class DryRunStep(BaseModel):
    """One predicate rule's effect on its source's pool, in declaration order."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    source: str
    pool_before: int = Field(ge=0)
    removed: int = Field(ge=0)
    """Rows this rule removed at its position — the funnel."""

    matched_standalone: int = Field(ge=0)
    """Rows this rule fires on regardless of earlier rules — what tells dead from redundant."""

    not_evaluable: int = Field(ge=0)
    """Rows the rule could not judge (blank field, text where a number belongs). Never removed."""

    flags: tuple[DryRunFlag, ...] = ()
    removed_samples: tuple[DryRunSample, ...] = ()


class DryRunSourceFile(BaseModel):
    """One local extract a dry-run read, and the pool it started and ended with.

    ``file_name`` is a basename, never a path: the record outlives the machine it was made on.
    """

    model_config = ConfigDict(frozen=True)

    source_name: str = Field(pattern=SLUG_PATTERN, max_length=64)
    file_name: str
    sha256: str
    rows: int = Field(ge=0)
    pool_end: int = Field(ge=0)
    row_id_column: str
    passing_samples: tuple[DryRunSample, ...] = ()


class DryRunReport(BaseModel):
    """What ``lpe manifest dry-run`` found: the funnel per rule, from free rules and real data."""

    model_config = ConfigDict(frozen=True)

    manifest_id: UUID
    files: tuple[DryRunSourceFile, ...]
    steps: tuple[DryRunStep, ...]

    def flagged(self) -> tuple[str, ...]:
        """Every ``rule: flag`` pair, in step order — the summary a person must read."""
        return tuple(f"{step.rule_id}: {flag.value}" for step in self.steps for flag in step.flags)


class DryRunResponse(BaseModel):
    """One ``manifest_dry_run`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    manifest_id: UUID
    ran_at: datetime
    ran_by: str
    report: DryRunReport
