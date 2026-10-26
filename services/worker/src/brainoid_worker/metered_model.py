"""Bound the request covered by a durable token reservation; preserve provider events."""

from collections.abc import AsyncGenerator

from brainoid_agent_core.ports import CallControl, ModelProvider
from brainoid_agent_core.types import Completed, Failed, ModelEvent, ModelRequest, Usage
from brainoid_usage import Amounts


class MeteredModel:
    def __init__(self, provider: ModelProvider, input_limit: int) -> None:
        self.provider, self.input_limit = provider, input_limit
        self.started = False
        self.completed = False
        self.observed = Amounts()

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        # Conservative local allowance uses serialized UTF-8 bytes, including
        # declarations and continuation. Billing/provider tokenizer is not inferred.
        if len(request.model_dump_json().encode()) > self.input_limit:
            yield Failed(code="rate_limited")
            return
        self.started = True
        stream = self.provider.stream(request, control=control)
        try:
            async for event in stream:
                if isinstance(event, Usage):
                    # Capture before yielding: Core may reject authority, hit an
                    # event/token limit, or cancel before it processes this event.
                    self.observed = Amounts(
                        input_tokens=max(self.observed.input_tokens, event.input_tokens or 0),
                        output_tokens=max(self.observed.output_tokens, event.output_tokens or 0),
                    )
                if isinstance(event, Completed):
                    self.completed = True
                yield event
        finally:
            await stream.aclose()
