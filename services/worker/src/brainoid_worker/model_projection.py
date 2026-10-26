"""Model replies are projections of full durable results, never authority records."""

from brainoid_agent_core.context import memory_projection
from brainoid_agent_core.types import MemoryRecord, ToolCall, ToolReply, ToolResult
from brainoid_agent_core.types.common import JsonObject
from brainoid_memory.retrieval import SearchTrace


def tool_reply(call: ToolCall, result: ToolResult) -> ToolReply:
    payload = result.result
    if call.name in ("memory.search", "memory.remember", "memory.revise"):
        raw = payload.get("memories", [])
        if not isinstance(raw, list):
            raise ValueError("Invalid internal memory result")
        projected: JsonObject = {
            "memories": [memory_projection(MemoryRecord.model_validate(record)) for record in raw]
        }
        if "trace" in payload:
            projected["trace"] = SearchTrace.model_validate(payload["trace"]).model_dump(
                mode="json"
            )
        payload = projected
    return ToolReply(
        call_key=call.call_key,
        name=call.name,
        result={"status": result.status, "result": payload, "error_code": result.error_code},
        is_error=result.status == "failed",
    )
