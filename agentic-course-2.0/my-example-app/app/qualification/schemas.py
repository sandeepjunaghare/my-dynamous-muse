"""The qualification shapes: what the evaluator reads, what it decides, and what the judgment node
may say and have kept.

Three groups, three different trust levels:

* **The record contract** (:data:`RecordSet`). Raw source fields as text, keyed by the
  manifest's declared source name. This is what T5's adapters emit and what the dry-run builds
  from a CSV. The values are deliberately uncast: casting is a rule's business (``power_units`` is
  text in the census and only a numeric compare needs it as a number).
* **The judgment node's output** (:class:`JudgmentAnswer`). Lax and untrusted, like the authoring
  agent's proposal: model output validated once, then held to the reads by ``admit_judgment``.
* **What survives** (:class:`AdmittedJudgment`). Only answers whose citation points at a page the
  node verifiably fetched, or at the candidate's own already-cited evidence.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.manifests.schemas import RuleKind, ScoreAxis
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import PriorityScore

type SourceRecord = Mapping[str, str | None]
"""One source's raw fields for one business, as the source published them: text or absent."""

type RecordSet = Mapping[str, SourceRecord]
"""Declared source name → that source's record. The contract T5's adapters emit."""


class OutcomeKind(StrEnum):
    """What one predicate rule concluded about one record."""

    fired = "fired"
    """The rule matched: the record is disqualified by it."""

    passed = "passed"
    """The rule was evaluated and did not match."""

    not_evaluable = "not_evaluable"
    """The rule could not judge this record. **Never** a disqualification."""


@dataclass(frozen=True, kw_only=True)
class RuleOutcome:
    """One predicate rule's verdict on one record, with what it saw."""

    rule_id: str
    source: str
    field: str
    kind: OutcomeKind
    observed: str | None = None
    """The field's text as read — what a person checks a disqualification against."""

    reason: str | None = None
    """Why the rule could not judge (``field_missing``, ``not_numeric``, …). Set only then."""


@dataclass(frozen=True)
class PredicateResult:
    """Every predicate rule's outcome for one record, in manifest declaration order."""

    outcomes: tuple[RuleOutcome, ...]

    def first_fired(self) -> RuleOutcome | None:
        """The first rule, in declaration order, that disqualifies the record."""
        return next((o for o in self.outcomes if o.kind is OutcomeKind.fired), None)

    def not_evaluable(self) -> tuple[RuleOutcome, ...]:
        return tuple(o for o in self.outcomes if o.kind is OutcomeKind.not_evaluable)


class PlacesEvidence(BaseModel):
    """What T6's Places check saw, handed to the judgment node within the run (D13).

    In memory only: the Maps Platform Terms bar storing it. It is context for the model and is
    never a citation. ``None`` until T6 builds the channel that carries it here.
    """

    model_config = ConfigDict(frozen=True)

    display_name: str | None = None
    business_status: str | None = None
    types: tuple[str, ...] = ()


# --- The judgment node's structured output (untrusted) -------------------------------------------
#
# Lax on purpose, like `app/manifests/proposal.py`: an out-of-range score or an unknown rule id
# costs that one answer in `admit_judgment`, never the whole paid-for call.


class Citation(BaseModel):
    """The page an answer rests on, and the words on it that carry the answer."""

    url: str
    quote: str


class RuleAnswer(BaseModel):
    """The node's decision on one judgment rule."""

    rule_id: str
    fired: bool
    reason: str
    citation: Citation | None = None


class SignalAnswer(BaseModel):
    """The node's answer to one qualifying signal, scored 1-5 on the axis the signal feeds."""

    signal_id: str
    answer: str
    axis_score: int
    citation: Citation | None = None


class JudgmentAnswer(BaseModel):
    """One call's verdict on every judgment rule and every qualifying signal (D12)."""

    model_config = ConfigDict(extra="ignore")

    rules: list[RuleAnswer] = []
    signals: list[SignalAnswer] = []


# --- What survives the citation gate --------------------------------------------------------------


@dataclass(frozen=True)
class AdmittedRule:
    """A judgment rule that fired, with the cited reason."""

    rule_id: str
    reason: ProvenancedValue[str]


@dataclass(frozen=True)
class AdmittedSignal:
    """A qualifying-signal answer kept because its citation held."""

    signal_id: str
    axis: ScoreAxis
    axis_score: int
    answer: ProvenancedValue[str]


@dataclass(frozen=True)
class OmittedAnswer:
    """An answer the gate dropped, and why — reported, never repaired."""

    label: str
    reason: str


@dataclass(frozen=True)
class AdmittedJudgment:
    """Everything the gate kept from one call, plus what it dropped."""

    fired: tuple[AdmittedRule, ...]
    signals: tuple[AdmittedSignal, ...]
    priority: ProvenancedValue[PriorityScore] | None
    omitted: tuple[OmittedAnswer, ...]


class DisqualificationResponse(BaseModel):
    """One ``disqualification`` row."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    run_id: UUID
    manifest_id: UUID
    vertical: str
    registry_id: str
    rule_id: str
    rule_kind: RuleKind
    source_url: str
    retrieval_method: RetrievalMethod
    evidence: str | None
    disqualified_at: datetime
