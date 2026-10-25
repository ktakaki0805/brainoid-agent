"""Deterministic scripted I/O, with per-invocation snapshots and no wall-clock waits."""

import math
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from brainoid_agent_core.ports import CallControl, Clock
from brainoid_agent_core.types import Cancelled, Completed, Failed, Stopped, TimedOut
from brainoid_contracts import normalize_utc
from pydantic import BaseModel


class FakeClock:
    def __init__(self, start: datetime = datetime(2026, 10, 26, tzinfo=UTC)) -> None:
        self._start = normalize_utc(start)
        self._elapsed = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Fake time can advance only by a finite nonnegative duration")
        self._elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        self.sleeps.append(seconds)


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


@dataclass(frozen=True)
class Step[T: BaseModel]:
    value: T
    delay: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.delay) or self.delay < 0:
            raise ValueError("Script delay must be finite and nonnegative")


@dataclass
class Invocation[Request: BaseModel, Event: BaseModel]:
    request: Request
    deadline: float | None
    started_at: float
    events: list[Event | Stopped] = field(default_factory=list)
    finished_at: float | None = None
    closed: bool = False


class Replay[Request: BaseModel, Event: BaseModel]:
    """One script per call, consumed when iteration starts. Exhaustion is a test error.

    Delay advances the injected clock. Cancellation is checked before/after each
    delay and before each emission; deadline equality is already timed out.
    These scripts simulate I/O only, never authorization, persistence, or effects.
    """

    def __init__(self, clock: Clock, scripts: tuple[tuple[Step[Event], ...], ...]) -> None:
        self.clock = clock
        self._scripts = tuple(
            tuple(Step(step.value.model_copy(deep=True), step.delay) for step in script)
            for script in scripts
        )
        self.history: list[Invocation[Request, Event]] = []

    def _stopped(self, control: CallControl) -> Cancelled | TimedOut | None:
        if control.cancellation.cancelled:
            return Cancelled()
        if control.deadline is not None and self.clock.monotonic() >= control.deadline:
            return TimedOut()
        return None

    async def stream(
        self, request: Request, *, control: CallControl, unary: bool = False
    ) -> AsyncGenerator[Event | Stopped]:
        index = len(self.history)
        if index >= len(self._scripts):
            raise AssertionError("No fake script configured for this call")
        script = self._scripts[index]
        if unary and len(script) > 1:
            raise AssertionError("Unary fake requires at most one response")
        call = Invocation[Request, Event](
            request.model_copy(deep=True), control.deadline, self.clock.monotonic()
        )
        self.history.append(call)
        try:
            for position, step in enumerate(script):
                stopped = self._stopped(control)
                if stopped is None:
                    delay = step.delay
                    if control.deadline is not None:
                        delay = min(delay, max(0.0, control.deadline - self.clock.monotonic()))
                    if delay:
                        await self.clock.sleep(delay)
                    stopped = self._stopped(control)
                if stopped is not None:
                    call.events.append(stopped)
                    yield stopped
                    return
                terminal = unary or isinstance(step.value, (Completed, Cancelled, TimedOut, Failed))
                if terminal and position != len(script) - 1:
                    raise AssertionError("Fake script has events after its terminal response")
                # Return and history own separate JSON containers, as does the configured script.
                call.events.append(step.value.model_copy(deep=True))
                yield step.value.model_copy(deep=True)
                if terminal:
                    return
            # Empty or truncated streams are failures, never implicitly completed.
            final: Stopped = self._stopped(control) or Failed(
                code="stream_interrupted", retryable=True
            )
            call.events.append(final)
            yield final
        finally:
            call.closed = True
            call.finished_at = self.clock.monotonic()

    async def reply(self, request: Request, *, control: CallControl) -> Event | Stopped:
        stream = self.stream(request, control=control, unary=True)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()
