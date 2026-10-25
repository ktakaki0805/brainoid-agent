"""Model scripts preserve untrusted or malformed tool arguments without executing them."""

from collections.abc import AsyncGenerator

from brainoid_agent_core.ports import CallControl, Clock
from brainoid_agent_core.types import ModelEvent, ModelRequest

from fakes.runtime import Replay, Step


class FakeModelProvider:
    def __init__(self, clock: Clock, scripts: tuple[tuple[Step[ModelEvent], ...], ...]) -> None:
        self.calls = Replay[ModelRequest, ModelEvent](clock, scripts)

    def stream(self, request: ModelRequest, *, control: CallControl) -> AsyncGenerator[ModelEvent]:
        return self.calls.stream(request, control=control)
