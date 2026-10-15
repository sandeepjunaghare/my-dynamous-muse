"""The ``resolve_owner`` judgment node: the second judgment the run pays for (D1), after the census.

It runs only for a candidate the census names nobody citable for (A2: census first), and returns
one person and the page that names them. The boundary guarantees copy the ``classify_rollup`` node
(``app/qualification/judgment.py``) deliberately:

* **A name is held to what was read** (A10). It survives only if its citation is a page a
  ``WebFetch`` verifiably fetched in this call (``app/shared/agent_reads.py``, fail-closed), the
  quote on it contains the surname as a word, the name is a person and not a company (E18), and
  neither the title nor the quote names an agent (E10). There is no registry evidence to fall back
  on: the registry naming nobody is why the node runs. Google Maps pages are never admitted (D13).
  **Anything else is left absent and never repaired.**
* **No name is ever logged.** An omission logs its fixed reason only, never the model's free text,
  which may name the person it refused; a refused name may still be a real person's.
* **It cannot touch the machine.** ``WebSearch`` and ``WebFetch`` only; no file, shell or MCP
  tools; no filesystem settings loaded; ``dontAsk`` refuses everything else.
* **It always records what the call cost**, on failure too, the moment the result arrives.
* **It never writes.** The stage decides what is recorded.
"""

import re
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError, Message, ResultMessage, query
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.cost import BillableKind, RunCost, format_usd
from app.core.logging import get_logger
from app.shared.agent_reads import ReadTracker
from app.shared.page_reads import PageRead, normalize_url
from app.shared.provenance import ProvenancedValue, RetrievalMethod
from app.sourcing.exceptions import OwnerJudgmentError
from app.sourcing.owner.prompts import SYSTEM_PROMPT, build_user_prompt
from app.sourcing.owner.schemas import AdmittedOwner, OwnerAnswer
from app.sourcing.schemas import CandidateFields, is_google_maps, principal_or_none

logger = get_logger(__name__)

OWNER_TOOLS = ("WebSearch", "WebFetch")
"""The node's entire tool surface: find the business, read about it."""

LOG_PREFIX = "sourcing.owner"

AGENT_TITLES = (
    "registered agent",
    "process agent",
    "statutory agent",
    "agent for service of process",
)
"""Phrases that name a filing role, not an owner (E10: the 74-row file of BOC-3 process agents).
Matched as substrings of the title *and* the quote, so ``"Owner / Registered Agent"`` is refused
too: refusing a real owner costs less than sending a founder to a filing agent's door (PB-4)."""

NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
"""Generational suffixes, skipped when finding the surname the quote must contain."""

REASON_LIMIT = 200
"""How much of the model's own ``reason`` an omission keeps."""

type OwnerRunner = Callable[[str, ClaudeAgentOptions], AsyncIterator[Message]]
"""Anything turning a prompt and options into the SDK's message stream: the offline test seam."""


def sdk_runner(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The production runner — the Agent SDK's one-shot ``query``."""
    return query(prompt=prompt, options=options)


@dataclass(frozen=True)
class OwnerRun:
    """One completed owner call."""

    answer: OwnerAnswer
    reads: tuple[PageRead, ...]
    turns: int
    cost_usd: Decimal | None


def build_options(settings: Settings) -> ClaudeAgentOptions:
    """The SDK options for one owner call — isolated exactly as the ``classify_rollup`` node is.

    ``setting_sources=[]`` and ``strict_mcp_config`` keep the bundled CLI from loading this
    repository's own ``.claude/`` settings, hooks and MCP servers into the node.
    """
    env = (
        {}
        if settings.anthropic_api_key is None
        else {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    )
    return ClaudeAgentOptions(
        tools=list(OWNER_TOOLS),
        allowed_tools=list(OWNER_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        system_prompt=SYSTEM_PROMPT,
        model=settings.owner_resolver_model,
        max_turns=settings.owner_resolver_max_turns,
        max_budget_usd=float(settings.owner_resolver_max_budget_usd),
        output_format={"type": "json_schema", "schema": OwnerAnswer.model_json_schema()},
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


def _failed(result: ResultMessage, usd: Decimal | None) -> OwnerJudgmentError:
    spent = "an unknown amount" if usd is None else format_usd(usd)
    detail = "; ".join(result.errors or []) or result.subtype
    logger.error(
        f"{LOG_PREFIX}.run_failed",
        reason=result.subtype,
        errors=result.errors,
        turns=result.num_turns,
        usd=None if usd is None else str(usd),
    )
    return OwnerJudgmentError(
        f"the owner node's run failed ({detail}) after {result.num_turns} turn(s), having "
        f"spent {spent}; nothing was recorded for this candidate",
        reason=result.subtype,
    )


async def run_owner_judgment(
    fields: CandidateFields,
    *,
    cost: RunCost,
    runner: OwnerRunner | None = None,
    settings: Settings | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> OwnerRun:
    """Ask the node who owns one business; return its raw answer and the pages it verifiably read.

    Raises :class:`OwnerJudgmentError` when the call cannot produce an answer — the SDK failed, the
    run ended on a cap or an error, or the result carried no valid structured answer.
    """
    resolved = settings or get_settings()
    prompt = build_user_prompt(fields)
    # Resolved at call time, so a test that replaces `sdk_runner` on this module is honoured.
    run = runner or sdk_runner
    tracker = ReadTracker(clock, log_prefix=LOG_PREFIX)
    result: ResultMessage | None = None
    usd: Decimal | None = None
    registry_id = fields.registry_id.value

    logger.info(
        f"{LOG_PREFIX}.run_started", registry_id=registry_id, model=resolved.owner_resolver_model
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
        raise OwnerJudgmentError(
            f"the owner node could not run: {exc}", reason="sdk_error"
        ) from exc

    if result is None:
        _record_cost(cost, None)
        logger.error(f"{LOG_PREFIX}.run_failed", reason="no_result")
        raise OwnerJudgmentError("the owner node ended without a result", reason="no_result")
    if result.is_error or result.subtype != "success":
        raise _failed(result, usd)
    if result.structured_output is None:
        logger.error(f"{LOG_PREFIX}.run_failed", reason="no_structured_output")
        raise OwnerJudgmentError(
            "the owner node finished without a structured answer", reason="no_structured_output"
        )
    try:
        answer = OwnerAnswer.model_validate(result.structured_output)
    except ValidationError as exc:
        logger.error(f"{LOG_PREFIX}.run_failed", reason="invalid_structured_output")
        raise OwnerJudgmentError(
            f"the owner node's answer did not match the contract: {exc.error_count()} error(s)",
            reason="invalid_structured_output",
        ) from exc

    logger.info(
        f"{LOG_PREFIX}.run_completed",
        registry_id=registry_id,
        turns=result.num_turns,
        pages_read=len(tracker.reads),
        usd=None if usd is None else str(usd),
    )
    return OwnerRun(answer=answer, reads=tuple(tracker.reads), turns=result.num_turns, cost_usd=usd)


def _index(reads: Iterable[PageRead]) -> dict[str, PageRead]:
    """The first read of each page, keyed for comparison; an unparseable URL backs nothing.

    A copy of the ``classify_rollup`` gate's: a shared citation gate is a follow-up, and importing
    another slice's private helper would couple the two nodes' gates without deciding to.
    """
    indexed: dict[str, PageRead] = {}
    for read in reads:
        try:
            indexed.setdefault(normalize_url(read.url), read)
        except ValueError:
            continue
    return indexed


def _surname(name: str) -> str:
    """The surname, roughly: the last word that is not a suffix, lowercased, unpunctuated."""
    tokens = [token.strip(".,;:!?").lower() for token in name.split()]
    words = [token for token in tokens if token]
    named = [token for token in words if token not in NAME_SUFFIXES] or words
    return named[-1]


def _names(quote: str, surname: str) -> bool:
    """Whether the quote contains the surname as a whole word: "Cole" is not in "Nicole"."""
    return re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", quote.lower()) is not None


def _omit(reason: str, detail: str = "") -> AdmittedOwner:
    """Log the fixed ``reason`` only; ``detail`` is the model's own text and may name a person."""
    logger.info(f"{LOG_PREFIX}.answer_omitted", reason=reason)
    return AdmittedOwner(owner=None, omitted=f"{reason}: {detail}" if detail else reason)


def admit_owner(answer: OwnerAnswer, *, reads: Sequence[PageRead]) -> AdmittedOwner:
    """Keep the named person only if a page the node read names them (A10); else say why not.

    The checks run in order and the first that fails decides. No registry evidence is accepted:
    the census naming nobody citable is the reason this node was called.
    """
    # Trailing list punctuation is not part of a name; a final "." may be ("Jr.", an initial).
    name = (answer.name or "").strip().rstrip(",;:").strip()
    if not name.strip(".,;:!?"):
        return _omit("no name offered", answer.reason.strip()[:REASON_LIMIT])
    citation = answer.citation
    if citation is None:
        return _omit("no citation offered")
    try:
        key = normalize_url(citation.url)
    except ValueError:
        return _omit("citation URL is not parseable")
    read = _index(reads).get(key)
    if read is None:
        return _omit("cites a page the node never successfully read")
    if is_google_maps(read.url):
        return _omit("cites Google Maps, whose content may not be stored")
    principal = principal_or_none(name, answer.title)
    if principal is None:
        return _omit("names a company or nothing, not a person (E18)")
    if not _names(citation.quote, _surname(principal.name)):
        return _omit("the quote does not contain the name")
    roles = f"{principal.title or ''} {citation.quote}".lower()
    if any(agent in roles for agent in AGENT_TITLES):
        return _omit("an agent is not the owner")
    return AdmittedOwner(
        owner=ProvenancedValue(
            value=principal,
            source_url=read.url,
            retrieved_at=read.read_at,
            retrieval_method=RetrievalMethod.llm_inference,
        ),
        omitted=None,
    )
