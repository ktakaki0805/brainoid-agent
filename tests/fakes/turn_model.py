"""Synthetic turn provider shared by fake and emulator acceptance tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
    ToolRequested,
)


class Model:
    def __init__(
        self,
        *,
        text: str = "Fixture answer",
        signed: bool = True,
        failure: bool = False,
        tool: bool = False,
    ) -> None:
        self.text, self.signed, self.failure, self.tool = text, signed, failure, tool
        self.requests: list[ModelRequest] = []
        self.hook: Callable[[], None] = lambda: None
        self.close_hook: Callable[[], None] = lambda: None
        self.finish_reason: str = "stop"

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append(request)
        try:
            self.hook()
            await asyncio.sleep(0)
            if self.failure:
                raise RuntimeError("PRIVATE_PROVIDER_ERROR_BODY")
            if self.text:
                yield TextDelta(text=self.text)
            if self.tool:
                yield ToolRequested(
                    call=ToolCall(call_key="call", name="unexecuted", arguments_json="{}")
                )
                yield Completed(finish_reason="tool_calls")
                return
            if self.signed:
                yield ModelResponse(
                    message=ModelMessage(
                        role="assistant",
                        parts=(TextPart(text=self.text),),
                        provider_state={"signature": "PRIVATE_SIGNATURE"},
                    )
                )
            yield Completed.model_validate({"finish_reason": self.finish_reason})
        finally:
            self.close_hook()
