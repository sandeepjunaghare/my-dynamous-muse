"""The ``classify_rollup`` judgment node — the second of the two places the run calls a model (D1).

**One call per candidate decides every judgment rule in the manifest and answers every qualifying
signal** (D12): "is this a national rollup with no local owner?", "double-brokering risk?", and the
1-5 signal scores, in one structured answer. ~$0.08 a call. Never one call per rule.

The boundary guarantees mirror the manifest-authoring agent (``app/manifests/agent.py``), whose
shape this copies deliberately:

* **Citations are held to what was read.** A fired rule survives only if its citation is a page a
  ``WebFetch`` verifiably fetched in this call (``app/shared/agent_reads.py``, fail-closed). A
  signal answer may also cite the candidate's own registry record, an already-cited fact — but a
  census row cannot show a rollup, so it never backs a disqualification. Google Maps pages are
  never admitted (D13). **An uncited verdict never disqualifies anyone** — it is dropped and
  reported.
* **It cannot touch the machine.** ``WebSearch`` and ``WebFetch`` only; no file, shell or MCP
  tools; no filesystem settings loaded; ``dontAsk`` refuses everything else.
* **It always records what the call cost** — on failure too, the moment the result arrives.
* **Places content is used and discarded** (D13). It reaches the prompt as context, from
  ``verify_business`` through the run's in-memory channel, and is never part of the answer or a
  citation.
* **A cited homepage and headcount come for free** (T6). The same reads may show the business's
  own homepage and a stated headcount; each is kept only when cited to a page the node read.
* **It never writes.** The stage decides what is recorded.
"""

from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, Message, ResultMessage, query
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.cost import BillableKind, RunCost, format_usd
from app.core.logging import get_logger
from app.manifests.schemas import ManifestBody, RuleKind
from app.qualification.exceptions import JudgmentNodeError
from app.qualification.prompts import SYSTEM_PROMPT, build_user_prompt
from app.qualification.schemas import (
    AdmittedJudgment,
    AdmittedRule,
    AdmittedSignal,
    Citation,
    HeadcountAnswer,
    JudgmentAnswer,
    OmittedAnswer,
)
from app.qualification.scoring import score
from app.shared.agent_reads import ReadTracker
from app.shared.page_reads import PageRead, normalize_url
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.schemas import (
    CandidateFields,
    HeadcountBand,
    PlacesEvidence,
    PriorityScore,
    is_google_maps,
)

logger = get_logger(__name__)

JUDGMENT_TOOLS = ("WebSearch", "WebFetch")
"""The node's entire tool surface: find the business, read about it."""

LOG_PREFIX = "qualification.judgment"

type JudgmentRunner = Callable[[str, ClaudeAgentOptions], AsyncIterator[Message]]
"""Anything turning a prompt and options into the SDK's message stream: the offline test seam."""


def sdk_runner(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The production runner — the Agent SDK's one-shot ``query``."""
    return query(prompt=prompt, options=options)


@dataclass(frozen=True)
class JudgmentRun:
    """One completed judgment call."""

    answer: JudgmentAnswer
    reads: tuple[PageRead, ...]
    turns: int
    cost_usd: Decimal | None


def build_options(settings: Settings) -> ClaudeAgentOptions:
    """The SDK options for one judgment call — isolated exactly as the authoring agent is.

    ``setting_sources=[]`` and ``strict_mcp_config`` keep the bundled CLI from loading this
    repository's own ``.claude/`` settings, hooks and MCP servers into the node.
    """
    env = (
        {}
        if settings.anthropic_api_key is None
        else {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    )
    return ClaudeAgentOptions(
        tools=list(JUDGMENT_TOOLS),
        allowed_tools=list(JUDGMENT_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        system_prompt=SYSTEM_PROMPT,
        model=settings.rollup_classifier_model,
        max_turns=settings.rollup_classifier_max_turns,
        max_budget_usd=float(settings.rollup_classifier_max_budget_usd),
        output_format={"type": "json_schema", "schema": JudgmentAnswer.model_json_schema()},
        env=env,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _record_cost(cost: RunCost, result: ResultMessage | None) -> Decimal | None:
    """Log the call's cost — unknown (``usd=None``) when no result came. ``count`` is turns."""
    if result is None:
        cost.record(BillableKind.anthropic_tokens, count=0, usd=None)
        return None
    usd = None if result.total_cost_usd is None else Decimal(str(result.total_cost_usd))
    cost.record(BillableKind.anthropic_tokens, count=result.num_turns, usd=usd)
    return usd


def _failed(result: ResultMessage, usd: Decimal | None) -> JudgmentNodeError:
    spent = "an unknown amount" if usd is None else format_usd(usd)
    detail = "; ".join(result.errors or []) or result.subtype
    logger.error(
        f"{LOG_PREFIX}.run_failed",
        reason=result.subtype,
        errors=result.errors,
        turns=result.num_turns,
        usd=None if usd is None else str(usd),
    )
    return JudgmentNodeError(
        f"the judgment node's run failed ({detail}) after {result.num_turns} turn(s), having "
        f"spent {spent}; nothing was recorded for this candidate",
        reason=result.subtype,
    )


async def run_judgment(
    candidate: CandidateFields,
    body: ManifestBody,
    *,
    cost: RunCost,
    places: PlacesEvidence | None = None,
    runner: JudgmentRunner | None = None,
    settings: Settings | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> JudgmentRun:
    """Ask the node about one candidate; return its raw answer and the pages it verifiably read.

    Raises :class:`JudgmentNodeError` when the call cannot produce an answer — the SDK failed, the
    run ended on a cap or an error, or the result carried no valid structured answer.
    """
    resolved = settings or get_settings()
    rules = [c.value for c in body.disqualifier_rules if c.value.kind is RuleKind.judgment]
    signals = [c.value for c in body.qualifying_signals]
    prompt = build_user_prompt(candidate, rules, signals, places)
    # Resolved at call time, so a test that replaces `sdk_runner` on this module is honoured.
    run = runner or sdk_runner
    tracker = ReadTracker(clock, log_prefix=LOG_PREFIX)
    result: ResultMessage | None = None
    usd: Decimal | None = None
    registry_id = candidate.registry_id.value

    logger.info(
        f"{LOG_PREFIX}.run_started",
        registry_id=registry_id,
        model=resolved.rollup_classifier_model,
        judgment_rules=[rule.id for rule in rules],
        signals=[signal.id for signal in signals],
    )
    try:
        async for message in run(prompt, build_options(resolved)):
            tracker.observe(message)
            if isinstance(message, ResultMessage) and result is None:
                # Recorded now: after an error result the SDK raises from this loop.
                result = message
                usd = _record_cost(cost, result)
    except ClaudeSDKError as exc:
        if result is not None and (result.is_error or result.subtype != "success"):
            raise _failed(result, usd) from exc
        if result is None:
            _record_cost(cost, None)
        logger.error(f"{LOG_PREFIX}.run_failed", reason="sdk_error", error=str(exc))
        raise JudgmentNodeError(
            f"the judgment node could not run: {exc}", reason="sdk_error"
        ) from exc

    if result is None:
        _record_cost(cost, None)
        logger.error(f"{LOG_PREFIX}.run_failed", reason="no_result")
        raise JudgmentNodeError("the judgment node ended without a result", reason="no_result")
    if result.is_error or result.subtype != "success":
        raise _failed(result, usd)
    if result.structured_output is None:
        logger.error(f"{LOG_PREFIX}.run_failed", reason="no_structured_output")
        raise JudgmentNodeError(
            "the judgment node finished without a structured answer", reason="no_structured_output"
        )
    try:
        answer = JudgmentAnswer.model_validate(result.structured_output)
    except ValidationError as exc:
        logger.error(f"{LOG_PREFIX}.run_failed", reason="invalid_structured_output")
        raise JudgmentNodeError(
            f"the judgment node's answer did not match the contract: {exc.error_count()} error(s)",
            reason="invalid_structured_output",
        ) from exc

    logger.info(
        f"{LOG_PREFIX}.run_completed",
        registry_id=registry_id,
        turns=result.num_turns,
        pages_read=len(tracker.reads),
        usd=None if usd is None else str(usd),
    )
    return JudgmentRun(
        answer=answer, reads=tuple(tracker.reads), turns=result.num_turns, cost_usd=usd
    )


def evidence_reads(candidate: CandidateFields) -> tuple[PageRead, ...]:
    """The candidate's own cited fields, as citable reads: already-provenanced registry facts.

    Their retrieval time is the field's own, so a verdict resting on the census row is dated when
    the census was read, not when the model looked at it.
    """
    cited = (
        candidate.registry_id,
        candidate.legal_name,
        candidate.dba_name,
        candidate.address,
        candidate.phone,
        candidate.website,
    )
    return tuple(
        PageRead(url=field.source_url, read_at=field.retrieved_at)
        for field in cited
        if field is not None
    )


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def listing_name(places: PlacesEvidence | None, candidate: CandidateFields) -> str | None:
    """The Places listing name as only Places knows it, for the gate to keep out of storage (D13).

    ``None`` when there is no listing name, or when it is contained in one of the candidate's own
    cited names: text repeating the registry's name is a cited registry fact, not Places content.
    """
    if places is None or places.display_name is None or not places.display_name.strip():
        return None
    listed = _folded(places.display_name)
    own = (field.value for field in (candidate.legal_name, candidate.dba_name) if field is not None)
    if any(listed in _folded(name) for name in own):
        return None
    return listed


def _index(reads: Iterable[PageRead]) -> dict[str, PageRead]:
    """The first read of each page, keyed for comparison; an unparseable URL backs nothing."""
    indexed: dict[str, PageRead] = {}
    for read in reads:
        try:
            indexed.setdefault(normalize_url(read.url), read)
        except ValueError:
            continue
    return indexed


class _Gate:
    """Holds each answer's citation to the reads; collects what it drops and why.

    Two kinds of citable page, deliberately not interchangeable (PR #18 review, M1):

    * **reads** — pages the node verifiably fetched in this call. These may back anything.
    * **evidence** — the candidate's own registry citations. A census row can support a signal
      answer, but it cannot show that a business is a national rollup; and a judgment that fires is
      a permanent suppression. So evidence never backs a fired judgment rule.

    A Google Maps page is never admitted: its content may not be stored (D13, review M2). Nor is
    stored text that repeats the Places listing name, which reached the prompt as context only
    (D13, PR #34 review M1).
    """

    def __init__(
        self,
        reads: Iterable[PageRead],
        evidence: Iterable[PageRead] = (),
        listed: str | None = None,
    ) -> None:
        self._reads = _index(reads)
        self._evidence = _index(evidence)
        self._listed = listed
        self.omitted: list[OmittedAnswer] = []

    def omit(self, label: str, reason: str) -> None:
        self.omitted.append(OmittedAnswer(label=label, reason=reason))
        logger.info(f"{LOG_PREFIX}.answer_omitted", answer=label, reason=reason)

    def read_for(
        self, label: str, citation: Citation | None, *, allow_evidence: bool
    ) -> PageRead | None:
        """The read a citation rests on, or ``None`` with the omission recorded."""
        if citation is None:
            self.omit(label, "no citation offered")
            return None
        try:
            key = normalize_url(citation.url)
        except ValueError:
            self.omit(label, f"citation URL {citation.url!r} is not parseable")
            return None
        read = self._reads.get(key)
        if read is None and key in self._evidence:
            if not allow_evidence:
                self.omit(
                    label,
                    f"cites the registry record {citation.url}, which cannot evidence a judgment; "
                    "a fired rule must cite a page the node read",
                )
                return None
            read = self._evidence[key]
        if read is None:
            self.omit(label, f"cites {citation.url}, which the node never successfully read")
            return None
        if is_google_maps(read.url):
            self.omit(label, f"cites Google Maps ({read.url}), whose content may not be stored")
            return None
        return read

    def admit(
        self, label: str, citation: Citation | None, text: str, *, allow_evidence: bool
    ) -> ProvenancedValue[str] | None:
        read = self.read_for(label, citation, allow_evidence=allow_evidence)
        if read is None or citation is None:
            return None
        if self._listed is not None and self._listed in _folded(f"{text} {citation.quote}"):
            # The name itself is never logged: it is Places content too.
            self.omit(label, "repeats the Places listing name, which may not be stored (D13)")
            return None
        quote = citation.quote.strip()
        return ProvenancedValue(
            value=f'{text.strip()} — "{quote}"' if quote else text.strip(),
            source_url=read.url,
            retrieved_at=read.read_at,
            retrieval_method=RetrievalMethod.llm_inference,
        )


def admit_judgment(
    answer: JudgmentAnswer,
    body: ManifestBody,
    *,
    reads: Sequence[PageRead],
    evidence: Sequence[PageRead] = (),
    listed: str | None = None,
) -> AdmittedJudgment:
    """Keep only the answers a citation holds up; compute the priority from what survives.

    A rule id that is not one of this manifest's *judgment* rules is dropped — the model cannot
    invent a disqualifier, nor re-decide a free predicate. A fired rule must cite a page the node
    read in this call; one citing nothing, an unread page, the registry record or Google Maps is
    dropped, so it never disqualifies. ``fired: false`` is not stored at all. ``listed`` is
    :func:`listing_name`'s result: an answer repeating it is dropped rather than stored.
    """
    gate = _Gate(reads, evidence, listed)
    judgment_ids = {
        c.value.id for c in body.disqualifier_rules if c.value.kind is RuleKind.judgment
    }
    axes = {c.value.id: c.value.feeds for c in body.qualifying_signals}

    fired: list[AdmittedRule] = []
    decided: set[str] = set()
    for rule in answer.rules:
        label = f"rules[{rule.rule_id}]"
        if rule.rule_id not in judgment_ids:
            gate.omit(label, "not a judgment rule of this manifest")
            continue
        if rule.rule_id in decided:
            gate.omit(label, "decided twice; the first answer stands")
            continue
        decided.add(rule.rule_id)
        if not rule.fired:
            continue
        reason = gate.admit(label, rule.citation, rule.reason, allow_evidence=False)
        if reason is not None:
            fired.append(AdmittedRule(rule_id=rule.rule_id, reason=reason))

    signals: list[AdmittedSignal] = []
    answered: set[str] = set()
    for item in answer.signals:
        label = f"signals[{item.signal_id}]"
        axis = axes.get(item.signal_id)
        if axis is None:
            gate.omit(label, "not a qualifying signal of this manifest")
            continue
        if item.signal_id in answered:
            gate.omit(label, "answered twice; the first answer stands")
            continue
        answered.add(item.signal_id)
        if not 1 <= item.axis_score <= 5:
            gate.omit(label, f"axis score {item.axis_score} is outside 1-5")
            continue
        cited_answer = gate.admit(label, item.citation, item.answer, allow_evidence=True)
        if cited_answer is not None:
            signals.append(
                AdmittedSignal(
                    signal_id=item.signal_id,
                    axis=axis,
                    axis_score=item.axis_score,
                    answer=cited_answer,
                )
            )

    website = _admit_website(gate, answer.website)
    headcount_band = _admit_headcount(gate, answer.headcount)
    return AdmittedJudgment(
        fired=tuple(fired),
        signals=tuple(signals),
        priority=_cited_priority(body, signals),
        omitted=tuple(gate.omitted),
        website=website,
        headcount_band=headcount_band,
    )


def _cited_to[T](value: T, read: PageRead) -> ProvenancedValue[T]:
    return ProvenancedValue(
        value=value,
        source_url=read.url,
        retrieved_at=read.read_at,
        retrieval_method=RetrievalMethod.llm_inference,
    )


def _admit_website(gate: _Gate, citation: Citation | None) -> ProvenancedValue[str] | None:
    """The homepage origin, kept only when the node read that homepage itself (OQ-1 b).

    A listing or about page is not the business's homepage, so its path must be the root. Never
    from the registry record or from Places evidence, which is context and never a read.
    """
    if citation is None:
        return None
    read = gate.read_for("website", citation, allow_evidence=False)
    if read is None:
        return None
    parts = urlsplit(read.url)
    if parts.path not in ("", "/") or not parts.hostname:
        gate.omit("website", f"cites {read.url}, which is not a homepage")
        return None
    port = f":{parts.port}" if parts.port else ""
    return _cited_to(f"{parts.scheme}://{parts.hostname}{port}", read)


def _admit_headcount(
    gate: _Gate, headcount: HeadcountAnswer | None
) -> ProvenancedValue[HeadcountBand] | None:
    """A stated headcount band, cited to the page stating it (OQ-3 a). Never estimated (M8)."""
    if headcount is None:
        return None
    try:
        band = HeadcountBand(low=headcount.low, high=headcount.high)
    except ValidationError:
        gate.omit("headcount", f"headcount band {headcount.low}-{headcount.high} is invalid")
        return None
    read = gate.read_for("headcount", headcount.citation, allow_evidence=False)
    return None if read is None else _cited_to(band, read)


def _cited_priority(
    body: ManifestBody, signals: Sequence[AdmittedSignal]
) -> ProvenancedValue[PriorityScore] | None:
    """The score, cited to the first (by URL) of the answers it rests on — deterministically."""
    priority = score(body, signals)
    if priority is None:
        return None
    anchor = min((signal.answer for signal in signals), key=lambda cited: cited.source_url)
    return ProvenancedValue(
        value=priority,
        source_url=anchor.source_url,
        retrieved_at=anchor.retrieved_at,
        retrieval_method=RetrievalMethod.llm_inference,
    )
