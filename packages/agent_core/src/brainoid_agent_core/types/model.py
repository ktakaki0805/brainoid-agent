"""Model messages and events, including untrusted tool-call candidates."""

from typing import Annotated, Literal

from pydantic import Field

from brainoid_agent_core.types.common import (
    CoreValue,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    TerminalEvent,
)


class TextPart(CoreValue):
    type: Literal["text"] = "text"
    text: str


class ToolCall(CoreValue):
    """Model-local correlation key, not a durable tool_call_id or authority to execute.

    Keep raw JSON (including broken JSON) until ToolExecutor validates the candidate.
    Adapter SDK call objects must never appear in this value.
    """

    type: Literal["tool_call"] = "tool_call"
    call_key: NonEmptyText
    name: NonEmptyText
    arguments_json: str


class ToolReply(CoreValue):
    type: Literal["tool_reply"] = "tool_reply"
    call_key: NonEmptyText
    name: NonEmptyText
    result: JsonObject
    is_error: bool = False


class ModelMessage(CoreValue):
    role: Literal["system", "user", "assistant", "tool"]
    parts: tuple[Annotated[TextPart | ToolCall | ToolReply, Field(discriminator="type")], ...]


class ToolDefinition(CoreValue):
    """Application-selected declaration; the schema is data, not an SDK Schema object."""

    name: NonEmptyText
    description: str
    parameters: JsonObject


class ModelRequest(CoreValue):
    """Contains only approved model context, never TenantContext or credentials."""

    messages: tuple[ModelMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()
    response_schema: JsonObject | None = None
    max_output_tokens: Annotated[int, Field(strict=True, gt=0)] = 1024


class TextDelta(CoreValue):
    type: Literal["text_delta"] = "text_delta"
    text: NonEmptyText


class ToolRequested(CoreValue):
    type: Literal["tool_requested"] = "tool_requested"
    call: ToolCall


class Usage(CoreValue):
    """Cumulative totals for this invocation, not incremental token deltas.

    None means the provider did not report that count; it does not mean zero.
    """

    type: Literal["usage"] = "usage"
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    cached_input_tokens: NonNegativeInt | None = None


type ModelEvent = Annotated[
    TextDelta | ToolRequested | Usage | TerminalEvent, Field(discriminator="type")
]
