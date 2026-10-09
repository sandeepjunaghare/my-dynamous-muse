"""Replays a recorded agent transcript through the SDK's own message types — no model, no network.

Fixtures are JSON shaped like the SDK's stream (assistant ``tool_use`` -> user ``tool_result`` ->
... -> ``result``) and are parsed here into the real ``claude_agent_sdk`` dataclasses, so the code
under test sees exactly the objects ``query()`` would yield.
"""

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ContentBlock,
    Message,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel, Field, JsonValue

FIXTURES = Path(__file__).parent / "fixtures"
FREIGHT_TRANSCRIPT = FIXTURES / "freight_proposal_transcript.json"


class _Text(BaseModel):
    type: Literal["text"]
    text: str


class _ToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, JsonValue]


class _ToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | None = None
    is_error: bool | None = None


_Block = Annotated[_Text | _ToolUse | _ToolResult, Field(discriminator="type")]


class _Assistant(BaseModel):
    type: Literal["assistant"]
    model: str
    content: list[_Block]


class _User(BaseModel):
    type: Literal["user"]
    content: list[_Block]


class _Result(BaseModel):
    type: Literal["result"]
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: float | None = None
    structured_output: JsonValue = None
    errors: list[str] | None = None


class _Transcript(BaseModel):
    messages: list[Annotated[_Assistant | _User | _Result, Field(discriminator="type")]]


def _block(block: _Text | _ToolUse | _ToolResult) -> ContentBlock:
    if isinstance(block, _Text):
        return TextBlock(text=block.text)
    if isinstance(block, _ToolUse):
        return ToolUseBlock(id=block.id, name=block.name, input=dict(block.input))
    return ToolResultBlock(
        tool_use_id=block.tool_use_id, content=block.content, is_error=block.is_error
    )


def load_transcript(path: Path = FREIGHT_TRANSCRIPT) -> list[Message]:
    """Parse a fixture into the SDK message objects it records."""
    transcript = _Transcript.model_validate(json.loads(path.read_text(encoding="utf-8")))
    messages: list[Message] = []
    for record in transcript.messages:
        if isinstance(record, _Assistant):
            messages.append(
                AssistantMessage(content=[_block(b) for b in record.content], model=record.model)
            )
        elif isinstance(record, _User):
            messages.append(UserMessage(content=[_block(b) for b in record.content]))
        else:
            messages.append(
                ResultMessage(
                    subtype=record.subtype,
                    duration_ms=record.duration_ms,
                    duration_api_ms=record.duration_api_ms,
                    is_error=record.is_error,
                    num_turns=record.num_turns,
                    session_id=record.session_id,
                    total_cost_usd=record.total_cost_usd,
                    structured_output=record.structured_output,
                    errors=record.errors,
                )
            )
    return messages


def load_structured_output(path: Path = FREIGHT_TRANSCRIPT) -> JsonValue:
    """Just the recorded proposal, for tests of the gate that need no transcript."""
    for message in load_transcript(path):
        if isinstance(message, ResultMessage):
            output: JsonValue = message.structured_output
            return output
    raise AssertionError(f"{path.name} records no result")


def with_result(
    structured_output: JsonValue, *, subtype: str = "success", is_error: bool = False
) -> list[Message]:
    """The recorded freight transcript with its final result altered."""
    messages = load_transcript()
    result = messages[-1]
    assert isinstance(result, ResultMessage)
    changed = replace(
        result, subtype=subtype, is_error=is_error, structured_output=structured_output
    )
    return [*messages[:-1], changed]


class Replay:
    """An ``AgentRunner`` that yields recorded messages and remembers what it was asked."""

    def __init__(self, messages: Sequence[Message]) -> None:
        self._messages = list(messages)
        self.prompts: list[str] = []
        self.options: list[ClaudeAgentOptions] = []

    async def __call__(self, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Message]:
        self.prompts.append(prompt)
        self.options.append(options)
        for message in self._messages:
            yield message

    def as_query(self) -> "_QueryStandIn":
        """The same replay, shaped like ``claude_agent_sdk.query`` — for patching the module."""
        return _QueryStandIn(self)


class _QueryStandIn:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay

    def __call__(
        self, *, prompt: str, options: ClaudeAgentOptions, transport: object = None
    ) -> AsyncIterator[Message]:
        return self._replay(prompt, options)
