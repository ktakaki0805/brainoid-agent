"""Scripted store, tool and speech ports. No authorization decisions or real effects."""

from collections.abc import AsyncGenerator

from brainoid_agent_core.ports import CallControl, Clock
from brainoid_agent_core.types import (
    MemoryForget,
    MemoryForgetResult,
    MemoryRemember,
    MemoryRevise,
    MemorySearch,
    MemorySearchResult,
    MemoryWriteResult,
    RecognitionEvent,
    SpeechRecognition,
    SpeechSynthesis,
    SynthesisEvent,
    ToolExecution,
    ToolExecutionResult,
)

from fakes.runtime import Replay, Step


class FakeMemoryStore:
    """A configured response may intentionally violate the real store's access contract.

    Such a fixture is a faulty adapter candidate, not an authorized search result.
    This fake does not filter candidates, implement IAM, or persist mutations.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        searches: tuple[tuple[Step[MemorySearchResult], ...], ...] = (),
        remembers: tuple[tuple[Step[MemoryWriteResult], ...], ...] = (),
        revisions: tuple[tuple[Step[MemoryWriteResult], ...], ...] = (),
        forgets: tuple[tuple[Step[MemoryForgetResult], ...], ...] = (),
    ) -> None:
        self.searches = Replay[MemorySearch, MemorySearchResult](clock, searches)
        self.remembers = Replay[MemoryRemember, MemoryWriteResult](clock, remembers)
        self.revisions = Replay[MemoryRevise, MemoryWriteResult](clock, revisions)
        self.forgets = Replay[MemoryForget, MemoryForgetResult](clock, forgets)

    async def search(self, request: MemorySearch, *, control: CallControl) -> MemorySearchResult:
        return await self.searches.reply(request, control=control)

    async def remember(self, request: MemoryRemember, *, control: CallControl) -> MemoryWriteResult:
        return await self.remembers.reply(request, control=control)

    async def revise(self, request: MemoryRevise, *, control: CallControl) -> MemoryWriteResult:
        return await self.revisions.reply(request, control=control)

    async def forget(self, request: MemoryForget, *, control: CallControl) -> MemoryForgetResult:
        return await self.forgets.reply(request, control=control)


class FakeToolExecutor:
    def __init__(
        self, clock: Clock, scripts: tuple[tuple[Step[ToolExecutionResult], ...], ...]
    ) -> None:
        self.calls = Replay[ToolExecution, ToolExecutionResult](clock, scripts)

    async def execute(self, request: ToolExecution, *, control: CallControl) -> ToolExecutionResult:
        return await self.calls.reply(request, control=control)


class FakeSpeechProvider:
    def __init__(
        self,
        clock: Clock,
        *,
        recognition: tuple[tuple[Step[RecognitionEvent], ...], ...] = (),
        synthesis: tuple[tuple[Step[SynthesisEvent], ...], ...] = (),
    ) -> None:
        self.recognition = Replay[SpeechRecognition, RecognitionEvent](clock, recognition)
        self.synthesis = Replay[SpeechSynthesis, SynthesisEvent](clock, synthesis)

    def recognize(
        self, request: SpeechRecognition, *, control: CallControl
    ) -> AsyncGenerator[RecognitionEvent]:
        return self.recognition.stream(request, control=control)

    def synthesize(
        self, request: SpeechSynthesis, *, control: CallControl
    ) -> AsyncGenerator[SynthesisEvent]:
        return self.synthesis.stream(request, control=control)
