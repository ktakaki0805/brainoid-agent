"""Provider events with a precise pre-delivery hook for quota/authority races."""

from collections.abc import AsyncGenerator, Callable

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ModelEvent, ModelRequest


class UsageModel:
    def __init__(
        self,
        events: tuple[ModelEvent, ...],
        *,
        before_event: Callable[[ModelEvent], None] = lambda _: None,
    ) -> None:
        self.events, self.before_event = events, before_event
        self.calls = 0

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.calls += 1
        for event in self.events:
            self.before_event(event)
            yield event
