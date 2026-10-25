"""Structural ports shared by fake and real adapters.

Streams emit exactly one terminal event and no events after it. Early EOF is failure.
Adapters normalize SDK failures, cooperate with cancellation/deadlines while awaiting
I/O, and release resources when a consumer closes its iterator. Task cancellation
(asyncio.CancelledError) propagates after cleanup; it is not an application cancel.
Timeout/cancel do not imply rollback: an uncertain external effect is outcome_unknown.
"""

import math
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol

from brainoid_contracts import UtcTimestamp

from brainoid_agent_core.types import (
    MemoryForget,
    MemoryForgetResult,
    MemoryRemember,
    MemoryRevise,
    MemorySearch,
    MemorySearchResult,
    MemoryWriteResult,
    ModelEvent,
    ModelRequest,
    RecognitionEvent,
    SpeechRecognition,
    SpeechSynthesis,
    SynthesisEvent,
    ToolExecution,
    ToolExecutionResult,
)


class Clock(Protocol):
    def now(self) -> UtcTimestamp:
        """Return an aware UTC instant for records, never for elapsed-time deadlines."""
        ...

    def monotonic(self) -> float:
        """Return seconds on this clock's monotonic timeline."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait a finite, nonnegative duration; fakes may advance logical time."""
        ...


class Cancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class CallControl:
    """Required call control; deadline is absolute on the adapter's injected Clock.

    None explicitly disables the deadline. If both conditions are visible before
    dispatch, cancellation wins. Do not serialize this live object to a provider.
    """

    cancellation: Cancellation
    deadline: float | None

    def __post_init__(self) -> None:
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("Deadline must be finite or None")


class ModelProvider(Protocol):
    def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]: ...


class MemoryStore(Protocol):
    """Recheck current tenant/actor/scope, source/version and deletion state on each call."""

    async def search(
        self, request: MemorySearch, *, control: CallControl
    ) -> MemorySearchResult: ...

    async def remember(
        self, request: MemoryRemember, *, control: CallControl
    ) -> MemoryWriteResult: ...

    async def revise(self, request: MemoryRevise, *, control: CallControl) -> MemoryWriteResult: ...

    async def forget(
        self, request: MemoryForget, *, control: CallControl
    ) -> MemoryForgetResult: ...


class ToolExecutor(Protocol):
    """Validate tool name/schema and recheck authority/confirmation before every effect.

    Model-supplied context, scope and confirmation must never override the envelope.
    Return outcome_unknown with durable correlation if an effect might have occurred.
    """

    async def execute(
        self, request: ToolExecution, *, control: CallControl
    ) -> ToolExecutionResult: ...


class SpeechProvider(Protocol):
    def recognize(
        self, request: SpeechRecognition, *, control: CallControl
    ) -> AsyncGenerator[RecognitionEvent]: ...

    def synthesize(
        self, request: SpeechSynthesis, *, control: CallControl
    ) -> AsyncGenerator[SynthesisEvent]: ...


__all__ = [
    "CallControl",
    "Cancellation",
    "Clock",
    "MemoryStore",
    "ModelProvider",
    "SpeechProvider",
    "ToolExecutor",
]
