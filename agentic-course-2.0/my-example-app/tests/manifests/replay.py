"""Replays a recorded CLI stream through the Agent SDK's real ``query()`` — no model, no network.

Fixtures are the raw stream-json the bundled ``claude`` CLI prints, one object per stdout line. A
fake :class:`~claude_agent_sdk.Transport` stands in for the subprocess: it answers the SDK's
``initialize`` control request, streams the recording, and — when the recording ends on an error
result — then fails with the same ``ProcessError`` that ``SubprocessCLITransport`` raises when the
CLI exits non-zero. So the SDK's own message parser builds every message (including
``UserMessage.tool_use_result``), and the SDK's own reader turns that exit into ``ResultError``.
The code under test sees what ``query()`` would yield *and* raise.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, Message, ProcessError, Transport, query
from pydantic import BaseModel, JsonValue

FIXTURES = Path(__file__).parent / "fixtures"
FREIGHT_TRANSCRIPT = FIXTURES / "freight_proposal_transcript.json"

type StreamLine = dict[str, JsonValue]
"""One stdout line from the CLI, already JSON-decoded."""


class _Recording(BaseModel):
    stream: list[StreamLine]


def load_transcript(path: Path = FREIGHT_TRANSCRIPT) -> list[StreamLine]:
    """The recorded stream, line by line."""
    return _Recording.model_validate(json.loads(path.read_text(encoding="utf-8"))).stream


def _result_line(stream: Sequence[StreamLine]) -> StreamLine:
    for line in reversed(stream):
        if line.get("type") == "result":
            return line
    raise AssertionError("the recording has no result line")


def load_structured_output(path: Path = FREIGHT_TRANSCRIPT) -> JsonValue:
    """Just the recorded proposal, for tests of the gate that need no transcript."""
    return _result_line(load_transcript(path)).get("structured_output")


def with_result(
    structured_output: JsonValue,
    *,
    subtype: str = "success",
    is_error: bool = False,
    errors: list[JsonValue] | None = None,
    total_cost_usd: float | None = 0.8421,
    num_turns: int = 9,
) -> list[StreamLine]:
    """The recorded freight stream with its final result line altered."""
    stream = load_transcript()
    result = dict(_result_line(stream))
    result.update(
        subtype=subtype,
        is_error=is_error,
        structured_output=structured_output,
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
    )
    if errors is not None:
        result["errors"] = errors
    return [*stream[:-1], result]


# --- Building streams by hand, in the CLI's shapes -----------------------------------------------


def web_fetch_call(tool_use_id: str, url: str) -> StreamLine:
    """An assistant turn asking for one ``WebFetch``."""
    return {
        "type": "assistant",
        "message": {
            "model": "claude-opus-5-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "WebFetch",
                    "input": {"url": url, "prompt": "What does this page say?"},
                }
            ],
        },
        "parent_tool_use_id": None,
    }


def web_fetch_result(
    tool_use_id: str,
    url: str,
    *,
    code: int = 200,
    code_text: str = "OK",
    text: str = "The page text, as the fetch model summarised it.",
    reported_url: str | None = None,
) -> StreamLine:
    """A ``WebFetch`` result exactly as the bundled CLI (2.1.294) reports it.

    HTTP errors and cross-host redirects are *not* tool errors: the CLI returns them as ordinary
    results — no ``is_error`` — and only ``tool_use_result.code`` tells them apart from a read.
    """
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": text}],
        },
        "parent_tool_use_id": None,
        "tool_use_result": {
            "bytes": len(text),
            "code": code,
            "codeText": code_text,
            "result": text,
            "durationMs": 640,
            "url": reported_url or url,
        },
    }


def http_error_text(code: int, code_text: str) -> str:
    """The CLI's text for a fetch that got an HTTP error status."""
    return (
        f"The server returned HTTP {code} {code_text}.\n\nThe response body was not retrieved. "
        "If this URL requires authentication, use an authenticated tool (e.g. `gh` for GitHub, or "
        "an MCP-provided fetch tool) instead of WebFetch."
    )


def redirect_text(original: str, target: str, code: int = 301) -> str:
    """The CLI's text for a redirect to another host, which it does not follow."""
    return (
        "REDIRECT DETECTED: The URL redirects to a location that was not fetched automatically.\n\n"
        f"    Original URL: {original}\n"
        "    Redirect URL (from the server's Location header — server-supplied, not verified): "
        f"{target}\n"
        f"    Status: {code} Moved Permanently\n"
    )


def result_line(
    structured_output: JsonValue,
    *,
    subtype: str = "success",
    is_error: bool = False,
    total_cost_usd: float | None = 0.1,
    num_turns: int = 3,
) -> StreamLine:
    """A terminal ``result`` line."""
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "is_error": is_error,
        "num_turns": num_turns,
        "session_id": "replay-session",
        "total_cost_usd": total_cost_usd,
        "structured_output": structured_output,
    }


# --- The fake CLI ---------------------------------------------------------------------------------


class _RecordedCLI(Transport):
    """Plays a recording back to the SDK the way the CLI subprocess would."""

    def __init__(self, stream: Sequence[StreamLine]) -> None:
        self._stream = list(stream)
        self._outbox: asyncio.Queue[StreamLine | None] = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def write(self, data: str) -> None:
        for raw in data.splitlines():
            if not raw.strip():
                continue
            sent = _Recording.model_validate({"stream": [json.loads(raw)]}).stream[0]
            if sent.get("type") == "control_request":
                await self._outbox.put(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": sent.get("request_id"),
                            "response": {},
                        },
                    }
                )
            elif sent.get("type") == "user":
                for line in self._stream:
                    await self._outbox.put(line)
                await self._outbox.put(None)

    async def read_messages(self) -> AsyncIterator[StreamLine]:
        while (line := await self._outbox.get()) is not None:
            yield line
        last = self._stream[-1] if self._stream else None
        if last is not None and last.get("type") == "result" and last.get("is_error") is True:
            # The CLI exits 1 after an error result; this is what the subprocess transport raises.
            raise ProcessError("Command failed with exit code 1", exit_code=1)

    async def close(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    async def end_input(self) -> None:
        return None


class Replay:
    """An ``AgentRunner`` that drives ``query()`` over a recording and remembers its calls."""

    def __init__(self, stream: Sequence[StreamLine]) -> None:
        self._stream = list(stream)
        self.prompts: list[str] = []
        self.options: list[ClaudeAgentOptions] = []

    def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompts.append(prompt)
        self.options.append(options)
        return query(prompt=prompt, options=options, transport=_RecordedCLI(self._stream))

    def as_query(self) -> "_QueryStandIn":
        """The same replay, shaped like ``claude_agent_sdk.query`` — for patching the module."""
        return _QueryStandIn(self)


class _QueryStandIn:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay

    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions, transport: Transport | None = None
    ) -> AsyncIterator[Message]:
        return self._replay(prompt, options)
