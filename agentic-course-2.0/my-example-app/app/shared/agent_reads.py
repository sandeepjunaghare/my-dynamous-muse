"""Which ``WebFetch`` calls in an Agent SDK run verifiably succeeded — fail-closed.

Shared by the manifest-authoring agent (T12) and the ``classify_rollup`` judgment node (T7); T6's
``resolve_owner`` is the third consumer. One copy on purpose: this is the logic standing between a
model's claim and a stored citation, and two copies of it would drift.

**It records what was read, not what was claimed.** A page counts as read only when a ``WebFetch``
for it verifiably succeeded: no tool error, a 2xx status in the CLI's structured result, and that
result reporting the host that was asked for. The CLI returns an HTTP 403/404 or a cross-host
redirect as an *ordinary* result, so the absence of ``is_error`` proves nothing. When the evidence
is missing or ambiguous the fetch is not a read — a dropped field is safe, a false citation is not.
Reads are stamped when this process observed the result.

Imports the SDK's message types only; it never starts a run.
"""

from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.logging import get_logger
from app.shared.page_reads import PageRead

logger = get_logger(__name__)

FETCH_TOOL = "WebFetch"

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


def unread_reason(
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


class ReadTracker:
    """Pairs each ``WebFetch`` call with its result to learn which pages were actually read.

    ``log_prefix`` is the consumer's ``domain.component``, so each agent's fetch events stay under
    its own name (``manifests.agent.fetch_failed``, ``qualification.judgment.fetch_failed``).
    """

    def __init__(self, clock: Callable[[], datetime], *, log_prefix: str) -> None:
        self._clock = clock
        self._log_prefix = log_prefix
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
                reason = unread_reason(
                    block, message.tool_use_result, url, attributable=len(results) == 1
                )
                if reason is not None:
                    logger.info(f"{self._log_prefix}.fetch_failed", url=url, reason=reason)
                    continue
                self.reads.append(PageRead(url=url, read_at=self._clock()))
                logger.info(f"{self._log_prefix}.fetch_succeeded", url=url)
