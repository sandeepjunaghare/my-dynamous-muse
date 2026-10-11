"""The authoring agent's output contract, and the citation gate between it and a DRAFT row.

The agent returns an :class:`AgentProposal` — structured output whose every researched field
carries an optional :class:`Citation`. :func:`build_draft` then decides, field by field, what may
be written:

* A field survives **only if its citation points at a page the agent successfully fetched in this
  run.** A URL the model merely remembers, or saw as a search-result snippet, is not a read, and a
  citation to it would be fabricated provenance — E18 again, through the tool built to stop it.
* A field that fails that test, or whose shape the manifest schema refuses, is **left absent** and
  reported as an :class:`OmittedField`, exactly as the sourcing pipeline leaves an uncitable field
  absent. Nothing is repaired or guessed.
* If a field ``ManifestBody`` cannot exist without (a source, the ICP band, the vocabulary) does
  not survive, there is no manifest to write, and :class:`ManifestProposalIncompleteError` says so.

Terms of use are deliberately outside this contract. The agent may say *where* a source's terms are
published; it has no field in which to say whether they are acceptable. That decision is made by a
person at ``lpe manifest activate`` and nowhere else.

Pure on purpose: no SDK import, no I/O. The agent boundary lives in ``agent.py``.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.logging import get_logger
from app.manifests.exceptions import ManifestProposalIncompleteError
from app.manifests.schemas import (
    SLUG_MAX_LENGTH,
    SLUG_PATTERN,
    DisqualifierRule,
    IcpBand,
    ManifestBody,
    ManifestSource,
    QualifyingSignal,
    RuleKind,
    RuleOperator,
    ScoreAxis,
    SourceKind,
    Vocabulary,
)
from app.shared.page_reads import PageRead, normalize_url
from app.shared.provenance import ProvenancedValue, RetrievalMethod

logger = get_logger(__name__)


# --- The agent's structured output ---------------------------------------------------------------
#
# Lax on purpose: identifiers are plain strings here and are validated against the real manifest
# schema in `build_draft`, so one malformed rule is one omitted field rather than a whole run lost.


class Citation(BaseModel):
    """Where the agent read the evidence for one field."""

    url: str
    """The page the evidence is on. Must be a page fetched with WebFetch during this run."""

    quote: str
    """The passage on that page that supports the field — shown to the reviewer, not persisted."""


class ProposedSource(BaseModel):
    """A proposed authoritative source."""

    name: str
    kind: SourceKind
    description: str
    base_url: str
    rate_limit_per_minute: int | None = None
    terms_of_use_url: str | None = None
    """Where the source publishes its terms, if the agent found it. A pointer, never a verdict."""

    citation: Citation | None = None


class ProposedRule(BaseModel):
    """A proposed disqualifier rule."""

    id: str
    kind: RuleKind
    description: str
    field: str | None = None
    operator: RuleOperator | None = None
    value: str | list[str] | int | bool | None = None
    citation: Citation | None = None


class ProposedSignal(BaseModel):
    """A proposed qualifying signal."""

    id: str
    question: str
    feeds: ScoreAxis
    citation: Citation | None = None


class ProposedIcpBand(BaseModel):
    """A proposed ICP headcount band."""

    headcount_min: int
    headcount_max: int
    requires_office_function: bool
    citation: Citation | None = None


class ProposedVocabulary(BaseModel):
    """Proposed vertical vocabulary."""

    terms: list[str]
    citation: Citation | None = None


class AgentProposal(BaseModel):
    """Everything the agent proposes for one vertical; its JSON schema is the SDK output format."""

    model_config = ConfigDict(title="ManifestProposal")

    vertical: str
    """A lowercase slug naming the vertical, e.g. ``collision_centers``."""

    sources: list[ProposedSource]
    disqualifier_rules: list[ProposedRule] = []
    qualifying_signals: list[ProposedSignal] = []
    icp_band: ProposedIcpBand | None = None
    vocabulary: ProposedVocabulary | None = None


# --- What the run read, and what the gate produced ----------------------------------------------


@dataclass(frozen=True)
class OmittedField:
    """A proposed field that was left out of the draft, and why."""

    label: str
    reason: str


@dataclass(frozen=True)
class CitedField:
    """A field that made it into the draft, with the passage the agent quoted for it."""

    label: str
    url: str
    quote: str


@dataclass(frozen=True)
class TermsQuestion:
    """The terms-of-use question for one declared source — raised here, answered at ``activate``."""

    source_name: str
    terms_url: str | None


@dataclass(frozen=True)
class DraftProposal:
    """A body ready for ``create_draft``, plus everything the reviewer needs to judge it."""

    vertical: str
    body: ManifestBody
    cited: tuple[CitedField, ...]
    omitted: tuple[OmittedField, ...]
    terms_questions: tuple[TermsQuestion, ...]


class _Gate:
    """Applies the citation rule to one proposal, collecting what it keeps and what it drops."""

    def __init__(self, reads: Iterable[PageRead]) -> None:
        # The first successful read of a page is the retrieval being cited.
        self._reads: dict[str, PageRead] = {}
        for read in reads:
            try:
                key = normalize_url(read.url)
            except ValueError:
                # Unreachable through WebFetch, which refuses a URL it cannot parse — but a read
                # that cannot be compared can never back a citation, so skip it rather than crash.
                logger.warning(
                    "manifests.proposal.read_skipped", url=read.url, reason="unparseable"
                )
                continue
            self._reads.setdefault(key, read)
        self.cited: list[CitedField] = []
        self.omitted: list[OmittedField] = []

    def omit(self, label: str, reason: str) -> None:
        self.omitted.append(OmittedField(label=label, reason=reason))
        logger.info("manifests.proposal.field_omitted", field=label, reason=reason)

    def admit[T](
        self, label: str, citation: Citation | None, build: Callable[[], T]
    ) -> ProvenancedValue[T] | None:
        """Wrap ``build()`` in a citation to a page that was read — or record why it cannot be."""
        if citation is None:
            self.omit(label, "no citation offered")
            return None

        try:
            key = normalize_url(citation.url)
        except ValueError:
            # One malformed, model-supplied URL costs one field — never the paid-for proposal.
            self.omit(label, f"citation URL {citation.url!r} is not parseable")
            return None
        read = self._reads.get(key)
        if read is None:
            self.omit(label, f"cites {citation.url}, which the agent never successfully fetched")
            return None

        try:
            value = build()
        except ValidationError as exc:
            message = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            self.omit(label, f"refused by the manifest schema: {message}")
            return None

        self.cited.append(CitedField(label=label, url=read.url, quote=citation.quote))
        return ProvenancedValue(
            value=value,
            source_url=read.url,
            retrieved_at=read.read_at,
            retrieval_method=RetrievalMethod.llm_inference,
        )


def _rule_value(
    value: str | list[str] | int | bool | None,
) -> str | tuple[str, ...] | int | bool | None:
    """JSON has lists; a citation may only hold tuples."""
    return tuple(value) if isinstance(value, list) else value


def build_draft(
    proposal: AgentProposal,
    reads: Iterable[PageRead],
    *,
    vertical_override: str | None = None,
) -> DraftProposal:
    """Turn the agent's proposal into a DRAFT body in which every field is cited or absent.

    ``terms`` is always empty: a freshly authored draft has no decisions, and the agent is never the
    one to make them.
    """
    gate = _Gate(reads)

    vertical = vertical_override or proposal.vertical
    if re.fullmatch(SLUG_PATTERN, vertical) is None or len(vertical) > SLUG_MAX_LENGTH:
        logger.warning("manifests.proposal.draft_rejected", reason="invalid_vertical")
        raise ManifestProposalIncompleteError(
            f"the agent proposed {vertical!r} as the vertical name, which is not a lowercase slug "
            f"of at most {SLUG_MAX_LENGTH} characters — re-run with --vertical <slug>",
            missing_fields=("vertical",),
        )

    sources: list[ProvenancedValue[ManifestSource]] = []
    terms_questions: list[TermsQuestion] = []
    for proposed in proposal.sources:
        label = f"sources[{proposed.name}]"
        if proposed.name in {kept.value.name for kept in sources}:
            gate.omit(label, "duplicate source name")
            continue
        cited_source = gate.admit(
            label,
            proposed.citation,
            partial(
                ManifestSource,
                name=proposed.name,
                kind=proposed.kind,
                description=proposed.description,
                base_url=proposed.base_url,
                rate_limit_per_minute=proposed.rate_limit_per_minute,
            ),
        )
        if cited_source is not None:
            sources.append(cited_source)
            terms_questions.append(
                TermsQuestion(source_name=proposed.name, terms_url=proposed.terms_of_use_url)
            )

    rules: list[ProvenancedValue[DisqualifierRule]] = []
    for proposed_rule in proposal.disqualifier_rules:
        label = f"disqualifier_rules[{proposed_rule.id}]"
        if proposed_rule.id in {kept.value.id for kept in rules}:
            gate.omit(label, "duplicate rule id")
            continue
        cited_rule = gate.admit(
            label,
            proposed_rule.citation,
            partial(
                DisqualifierRule,
                id=proposed_rule.id,
                kind=proposed_rule.kind,
                description=proposed_rule.description,
                field=proposed_rule.field,
                operator=proposed_rule.operator,
                value=_rule_value(proposed_rule.value),
            ),
        )
        if cited_rule is not None:
            rules.append(cited_rule)

    signals: list[ProvenancedValue[QualifyingSignal]] = []
    for proposed_signal in proposal.qualifying_signals:
        label = f"qualifying_signals[{proposed_signal.id}]"
        if proposed_signal.id in {kept.value.id for kept in signals}:
            gate.omit(label, "duplicate signal id")
            continue
        cited_signal = gate.admit(
            label,
            proposed_signal.citation,
            partial(
                QualifyingSignal,
                id=proposed_signal.id,
                question=proposed_signal.question,
                feeds=proposed_signal.feeds,
            ),
        )
        if cited_signal is not None:
            signals.append(cited_signal)

    icp_band: ProvenancedValue[IcpBand] | None = None
    if proposal.icp_band is None:
        gate.omit("icp_band", "not proposed")
    else:
        band = proposal.icp_band
        icp_band = gate.admit(
            "icp_band",
            band.citation,
            partial(
                IcpBand,
                headcount_min=band.headcount_min,
                headcount_max=band.headcount_max,
                requires_office_function=band.requires_office_function,
            ),
        )

    vocabulary: ProvenancedValue[Vocabulary] | None = None
    if proposal.vocabulary is None:
        gate.omit("vocabulary", "not proposed")
    else:
        words = proposal.vocabulary
        vocabulary = gate.admit(
            "vocabulary", words.citation, partial(Vocabulary, terms=tuple(words.terms))
        )

    missing = tuple(
        name
        for name, present in (
            ("sources", bool(sources)),
            ("icp_band", icp_band is not None),
            ("vocabulary", vocabulary is not None),
        )
        if not present
    )
    if missing or icp_band is None or vocabulary is None:
        reasons = "; ".join(f"{entry.label}: {entry.reason}" for entry in gate.omitted)
        logger.warning(
            "manifests.proposal.draft_rejected",
            reason="missing_required_fields",
            missing_fields=list(missing),
            omitted_fields=len(gate.omitted),
        )
        raise ManifestProposalIncompleteError(
            f"no draft written — the agent could not cite {', '.join(missing)}, which every "
            f"manifest requires ({reasons})",
            missing_fields=missing,
        )

    body = ManifestBody(
        sources=tuple(sources),
        disqualifier_rules=tuple(rules),
        qualifying_signals=tuple(signals),
        icp_band=icp_band,
        vocabulary=vocabulary,
    )
    logger.info(
        "manifests.proposal.draft_built",
        vertical=vertical,
        cited_fields=len(gate.cited),
        omitted_fields=len(gate.omitted),
        sources=list(body.source_names()),
    )
    return DraftProposal(
        vertical=vertical,
        body=body,
        cited=tuple(gate.cited),
        omitted=tuple(gate.omitted),
        terms_questions=tuple(terms_questions),
    )
