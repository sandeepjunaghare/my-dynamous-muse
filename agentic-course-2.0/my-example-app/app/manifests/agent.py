"""The manifest-authoring agent — the one place an agent drives control flow at run time (D3).

The weekly run is a deterministic pipeline. Authoring a manifest is not: *"what is the authoritative
registry for collision centers, and what disqualifies one?"* is open-ended research performed once
per vertical. So this module runs a Claude Agent SDK loop over a brief, with exactly two tools —
``WebSearch`` and ``WebFetch`` — and returns the agent's structured proposal together with the list
of pages it actually read. ``proposal.build_draft`` then holds every field to those reads.

What this module guarantees at the boundary:

* **It records what was read, not what was claimed.** A page counts as read only when a
  ``WebFetch`` for it verifiably succeeded: no tool error, a 2xx status in the CLI's structured
  result, and that result reporting the host that was asked for. The CLI returns an HTTP 403/404
  or a cross-host redirect as an *ordinary* result, so the absence of ``is_error`` proves
  nothing. When the evidence is missing or ambiguous the fetch is not a read — a dropped field is
  safe, a false citation is not. Reads are stamped when this process observed the result.
* **It cannot touch the machine.** No file, shell or MCP tools; no filesystem settings loaded;
  ``dontAsk`` refuses anything not pre-approved. The agent researches the web and answers.
* **It always logs what the run cost** — on failure too, because a failed run still spent money.
  The cost is recorded the moment the result arrives, before the SDK raises ``ResultError`` for
  a run that ended on a cap or an error.
* **It never writes.** The caller decides whether a proposal becomes a DRAFT row.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    Message,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import Settings, get_settings
from app.core.cost import BillableKind, RunCost, format_usd
from app.core.logging import get_logger
from app.manifests.exceptions import ManifestAgentError
from app.manifests.prompts import SYSTEM_PROMPT, build_user_prompt
from app.manifests.proposal import AgentProposal, PageRead

logger = get_logger(__name__)

RESEARCH_TOOLS = ("WebSearch", "WebFetch")
"""The agent's entire tool surface. Reading the web is the job; nothing else is needed."""

FETCH_TOOL = "WebFetch"

type AgentRunner = Callable[[str, ClaudeAgentOptions], AsyncIterator[Message]]
"""Anything that turns a prompt and options into the SDK's message stream.

The seam that keeps tests offline: production uses :func:`sdk_runner`; the suite replays a recorded
transcript through the same type.
"""


def sdk_runner(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
    """The production runner — the Agent SDK's one-shot ``query``."""
    return query(prompt=prompt, options=options)


@dataclass(frozen=True)
class AgentRun:
    """One completed research run."""

    proposal: AgentProposal
    reads: tuple[PageRead, ...]
    turns: int
    cost_usd: Decimal | None


def build_options(settings: Settings) -> ClaudeAgentOptions:
    """The SDK options for one research run.

    ``setting_sources=[]`` and ``strict_mcp_config`` matter as much as the tool list: without them
    the bundled CLI would load whatever ``.claude/`` settings, hooks and MCP servers sit in the
    working directory — this repository's own AI layer — and the research agent would inherit them.
    """
    env = (
        {}
        if settings.anthropic_api_key is None
        else {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    )
    return ClaudeAgentOptions(
        tools=list(RESEARCH_TOOLS),
        allowed_tools=list(RESEARCH_TOOLS),
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        system_prompt=SYSTEM_PROMPT,
        model=settings.manifest_agent_model,
        effort="high",
        max_turns=settings.manifest_agent_max_turns,
        max_budget_usd=float(settings.manifest_agent_max_budget_usd),
        output_format={"type": "json_schema", "schema": AgentProposal.model_json_schema()},
        env=env,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


REDIRECT_MARKER = "REDIRECT DETECTED"
"""How the CLI's ``WebFetch`` starts the text of a redirect it did not follow."""


def _host(url: str) -> str | None:
    """The lowercased host of ``url``, or ``None`` when it has none or does not parse."""
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return host or None


def _result_text(block: ToolResultBlock) -> str:
    if isinstance(block.content, str):
        return block.content
    if block.content is None:
        return ""
    return " ".join(str(part.get("text", "")) for part in block.content)


class _FetchOutcome(BaseModel):
    """The fields of the CLI's ``WebFetch`` output (``UserMessage.tool_use_result``) a read needs.

    Strict, so a status that is not a real integer or a URL that is not a string is unrecognised
    rather than coerced.
    """

    model_config = ConfigDict(strict=True, extra="ignore")

    code: int
    url: str


def _unread_reason(
    block: ToolResultBlock, structured: object, requested_url: str, *, attributable: bool
) -> str | None:
    """Why a ``WebFetch`` result is not proof of a read — or ``None`` when it is.

    Fails closed: every check demands positive evidence of success, so a shape this code does not
    recognise is treated as a failed fetch, never as a read.
    """
    if block.is_error:
        return "tool_error"
    if not attributable:
        # The structured result belongs to the message, not to a block: with several results in
        # one message there is no telling whose status it is.
        return "result_not_attributable"
    if structured is None:
        return "no_structured_result"
    try:
        outcome = _FetchOutcome.model_validate(structured)
    except ValidationError:
        return "unrecognised_structured_result"
    if not 200 <= outcome.code < 300:
        return f"http_{outcome.code}"
    requested_host = _host(requested_url)
    if requested_host is None or _host(outcome.url) != requested_host:
        return "host_mismatch"
    if _result_text(block).lstrip().startswith(REDIRECT_MARKER):
        return "redirect"
    return None


class _ReadTracker:
    """Pairs each ``WebFetch`` call with its result to learn which pages were actually read."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._pending: dict[str, str] = {}
        self.reads: list[PageRead] = []

    def observe(self, message: Message) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock) and block.name == FETCH_TOOL:
                    url = block.input.get("url")
                    if isinstance(url, str) and url.strip():
                        self._pending[block.id] = url
        elif isinstance(message, UserMessage) and not isinstance(message.content, str):
            results = [block for block in message.content if isinstance(block, ToolResultBlock)]
            for block in results:
                url = self._pending.pop(block.tool_use_id, None)
                if url is None:
                    continue
                reason = _unread_reason(
                    block, message.tool_use_result, url, attributable=len(results) == 1
                )
                if reason is not None:
                    logger.info("manifests.agent.fetch_failed", url=url, reason=reason)
                    continue
                self.reads.append(PageRead(url=url, read_at=self._clock()))
                logger.info("manifests.agent.fetch_succeeded", url=url)


def _record_cost(cost: RunCost, result: ResultMessage | None) -> Decimal | None:
    """Log the run's cost through :class:`RunCost` — unknown (``usd=None``) when no result came.

    ``count`` is the run's turn count: the SDK reports cost per run, not per token or per call.
    """
    if result is None:
        cost.record(BillableKind.anthropic_tokens, count=0, usd=None)
        return None
    usd = None if result.total_cost_usd is None else Decimal(str(result.total_cost_usd))
    cost.record(BillableKind.anthropic_tokens, count=result.num_turns, usd=usd)
    return usd


def _usd_text(usd: Decimal | None) -> str:
    return "an unknown amount" if usd is None else format_usd(usd)


def _failure_message(result: ResultMessage, usd: Decimal | None, settings: Settings) -> str:
    """One line that says why the run stopped, what it cost, and what to change."""
    spent = f"after {result.num_turns} turn(s), having spent {_usd_text(usd)}"
    if result.subtype == "error_max_turns":
        return (
            f"the authoring agent hit its turn cap ({settings.manifest_agent_max_turns} turns) "
            f"{spent}; nothing was written — narrow the brief, or raise "
            "MANIFEST_AGENT_MAX_TURNS"
        )
    if result.subtype == "error_max_budget_usd":
        cap = format_usd(settings.manifest_agent_max_budget_usd)
        return (
            f"the authoring agent hit its budget cap ({cap}) "
            f"{spent}; nothing was written — narrow the brief, or raise "
            "MANIFEST_AGENT_MAX_BUDGET_USD"
        )
    detail = "; ".join(result.errors or []) or result.subtype
    return f"the authoring agent's run failed ({detail}) {spent}; nothing was written"


def _run_failed(
    result: ResultMessage, usd: Decimal | None, settings: Settings
) -> ManifestAgentError:
    logger.error(
        "manifests.agent.run_failed",
        reason=result.subtype,
        errors=result.errors,
        turns=result.num_turns,
        usd=None if usd is None else str(usd),
    )
    return ManifestAgentError(_failure_message(result, usd, settings), reason=result.subtype)


async def run_agent(
    brief: str,
    *,
    cost: RunCost,
    vertical: str | None = None,
    runner: AgentRunner | None = None,
    settings: Settings | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> AgentRun:
    """Research ``brief`` and return the agent's proposal plus the pages it read.

    Raises :class:`ManifestAgentError` when the run cannot produce a proposal — the SDK failed, the
    run ended on a cap or an error, or the result carried no valid structured output.
    """
    resolved_settings = settings or get_settings()
    options = build_options(resolved_settings)
    # Resolved at call time, so a test that replaces `query` on this module is honoured too.
    run = runner or sdk_runner
    tracker = _ReadTracker(clock)
    result: ResultMessage | None = None
    usd: Decimal | None = None

    logger.info(
        "manifests.agent.run_started",
        model=resolved_settings.manifest_agent_model,
        max_turns=resolved_settings.manifest_agent_max_turns,
        max_budget_usd=str(resolved_settings.manifest_agent_max_budget_usd),
    )

    try:
        async for message in run(build_user_prompt(brief, vertical), options):
            tracker.observe(message)
            if isinstance(message, ResultMessage) and result is None:
                # Recorded now: after an error result the CLI exits non-zero and the SDK raises
                # `ResultError` from this loop, so nothing after it would run.
                result = message
                usd = _record_cost(cost, result)
    except ClaudeSDKError as exc:
        if result is not None and (result.is_error or result.subtype != "success"):
            # The expected exit after an error result (a cap, an API failure): report the result.
            raise _run_failed(result, usd, resolved_settings) from exc
        if result is None:
            _record_cost(cost, None)
        logger.error(
            "manifests.agent.run_failed",
            reason="sdk_error",
            error=str(exc),
            usd=None if usd is None else str(usd),
        )
        raise ManifestAgentError(
            f"the authoring agent could not run: {exc}", reason="sdk_error"
        ) from exc

    if result is None:
        _record_cost(cost, None)
        logger.error("manifests.agent.run_failed", reason="no_result", usd=None)
        raise ManifestAgentError("the authoring agent ended without a result", reason="no_result")

    if result.is_error or result.subtype != "success":
        raise _run_failed(result, usd, resolved_settings)

    if result.structured_output is None:
        logger.error(
            "manifests.agent.run_failed",
            reason="no_structured_output",
            turns=result.num_turns,
            usd=None if usd is None else str(usd),
        )
        raise ManifestAgentError(
            "the authoring agent finished without a structured proposal; nothing was written",
            reason="no_structured_output",
        )

    try:
        proposal = AgentProposal.model_validate(result.structured_output)
    except ValidationError as exc:
        logger.error(
            "manifests.agent.run_failed",
            reason="invalid_structured_output",
            turns=result.num_turns,
            usd=None if usd is None else str(usd),
        )
        raise ManifestAgentError(
            f"the authoring agent's proposal did not match the contract: {exc.error_count()} "
            "error(s); nothing was written",
            reason="invalid_structured_output",
        ) from exc

    logger.info(
        "manifests.agent.run_completed",
        model=resolved_settings.manifest_agent_model,
        turns=result.num_turns,
        pages_read=len(tracker.reads),
        usd=None if usd is None else str(usd),
        run_total_usd=str(cost.total_usd()),
    )
    return AgentRun(
        proposal=proposal,
        reads=tuple(tracker.reads),
        turns=result.num_turns,
        cost_usd=usd,
    )
