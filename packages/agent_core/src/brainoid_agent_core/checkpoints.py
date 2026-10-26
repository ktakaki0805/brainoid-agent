"""Validate server-owned continuation pairs without persistence or provider dependencies."""

from brainoid_agent_core.context import require_json, snapshot_message
from brainoid_agent_core.types import ModelMessage


def continuation_messages(messages: tuple[ModelMessage, ...]) -> tuple[ModelMessage, ...]:
    if len(messages) % 2:
        raise ValueError("Incomplete continuation")
    result: list[ModelMessage] = []
    for index in range(0, len(messages), 2):
        response, reply = messages[index : index + 2]
        calls = [p for p in response.parts if p.type == "tool_call"]
        replies = [p for p in reply.parts if p.type == "tool_reply"]
        if (
            response.role != "assistant"
            or reply.role != "tool"
            or not calls
            or len(replies) != len(reply.parts)
            or [(c.call_key, c.name) for c in calls] != [(r.call_key, r.name) for r in replies]
            or len({c.call_key for c in calls}) != len(calls)
            or reply.provider_state is not None
        ):
            raise ValueError("Invalid continuation binding")
        for part in replies:
            require_json(part.result)
        result.extend((snapshot_message(response), reply.model_copy(deep=True)))
    return tuple(result)
