"""One bounded text turn. No HTTP, persistence, device SDK or tool execution."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated

from pydantic import Field

from brainoid_agent_core.context import (
    ContextAuthorizer,
    ContextInput,
    build_context,
    require_json,
    snapshot_message,
)
from brainoid_agent_core.ports import CallControl, Clock, MemoryStore, ModelProvider
from brainoid_agent_core.types import (
    Cancelled,
    Completed,
    Failed,
    MemoryMatches,
    MemoryRecord,
    MemorySearch,
    ModelEvent,
    ModelMessage,
    ModelResponse,
    TextDelta,
    TimedOut,
    ToolCall,
    ToolDefinition,
    ToolRequested,
    Usage,
)
from brainoid_agent_core.types.common import CoreValue, Stopped, TerminalEvent

# A close has its own short deadline. Cancellation may enter an adapter's finally
# block, so task collection permits two bounded phases, sending cancel again in
# between. All durations use the same injected monotonic Clock as the turn.
_CLEANUP_GRACE_SECONDS = 0.25


class RunBudget(CoreValue):
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30
    max_output_tokens: Annotated[int, Field(strict=True, gt=0, le=65536)] = 1024
    max_output_characters: Annotated[int, Field(strict=True, gt=0, le=262144)] = 16000
    max_model_events: Annotated[int, Field(strict=True, gt=0, le=100000)] = 10000
    memory_limit: Annotated[int, Field(strict=True, ge=1, le=40)] = 10


class TextRunResult(CoreValue):
    text: str = Field(default="", repr=False)
    terminal: TerminalEvent
    usage: Usage = Field(default_factory=Usage)
    # Internal continuation only; serialization must not expose signatures/candidates.
    response: ModelMessage | None = Field(default=None, repr=False, exclude=True)
    tool_calls: tuple[ToolCall, ...] = Field(default=(), repr=False, exclude=True)


class _Stopped(Exception):
    def __init__(self, event: Stopped) -> None:
        super().__init__("Core stopped")
        self.event = event


class TextAgentCore:
    def __init__(
        self,
        model: ModelProvider,
        memory: MemoryStore,
        authorizer: ContextAuthorizer,
        *,
        clock: Clock,
    ) -> None:
        self._model, self._memory, self._authorizer, self._clock = model, memory, authorizer, clock

    def _stopped(self, control: CallControl) -> Cancelled | TimedOut | None:
        if control.cancellation.cancelled:
            return Cancelled()
        if control.deadline is not None and self._clock.monotonic() >= control.deadline:
            return TimedOut()
        return None

    @staticmethod
    def _consume(task: asyncio.Task[None]) -> None:
        # Tasks return None and raw errors stay in _wait's closure. Retrieve even
        # an unexpected late exception without logging its text or task payload.
        if not task.cancelled():
            task.exception()

    async def _collect(self, *tasks: asyncio.Task[None]) -> None:
        """Cancel and collect cooperative adapters without an unbounded gather.

        One cancel can merely enter a blocking finally. A second cancel interrupts
        that cleanup. Adapters that suppress repeated cancellation violate the port
        contract: stop waiting after two grace periods and consume any late error.
        """
        interrupted = False
        for _ in range(2):
            for task in tasks:
                if not task.done():
                    task.cancel()
            deadline = self._clock.monotonic() + _CLEANUP_GRACE_SECONDS
            while True:
                try:
                    # Let immediately cooperative cancellations finish without
                    # advancing a logical clock or waiting out the whole grace.
                    await asyncio.sleep(0)
                    if all(task.done() for task in tasks):
                        for task in tasks:
                            self._consume(task)
                        if interrupted:
                            raise asyncio.CancelledError
                        return
                    remaining = deadline - self._clock.monotonic()
                    if remaining <= 0:
                        break
                    await self._clock.sleep(min(0.05, remaining))
                except asyncio.CancelledError:
                    # Caller cancellation also requires bounded child cleanup.
                    # Remember it and propagate after collection, never serialize it.
                    interrupted = True
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if all(task.done() for task in tasks):
                        for task in tasks:
                            self._consume(task)
                        raise
        for task in tasks:
            if task.done():
                self._consume(task)
            else:
                task.add_done_callback(self._consume)
        if interrupted:
            raise asyncio.CancelledError
        raise _Stopped(Failed(code="unavailable"))

    async def _close(self, stream: AsyncGenerator[ModelEvent], control: CallControl) -> None:
        deadline = self._clock.monotonic() + _CLEANUP_GRACE_SECONDS
        close_control = CallControl(
            control.cancellation,
            min(deadline, control.deadline) if control.deadline is not None else deadline,
        )
        await self._wait(stream.aclose(), close_control)

    async def _wait[T](self, work: Awaitable[T], control: CallControl) -> T:
        # A completed asyncio task may appear in debug logs. Keep payloads and raw
        # exception objects in a closure; its task always returns only None.
        outcome: list[T | Exception] = []

        async def read() -> None:
            try:
                outcome.append(await work)
            except Exception as error:
                outcome.append(error)

        async def watch() -> None:
            while self._stopped(control) is None:
                assert control.deadline is not None
                await self._clock.sleep(
                    min(0.05, max(0.0, control.deadline - self._clock.monotonic()))
                )
                await asyncio.sleep(0)

        pending, watcher = asyncio.create_task(read()), asyncio.create_task(watch())
        try:
            await asyncio.wait((pending, watcher), return_when=asyncio.FIRST_COMPLETED)
            if stopped := self._stopped(control):
                raise _Stopped(stopped)
            pending.result()
            result = outcome[0]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            await self._collect(pending, watcher)

    async def _check(
        self,
        request: ContextInput,
        records: tuple[MemoryRecord, ...],
        stamp: str | None,
        control: CallControl,
    ) -> str:
        result = await self._wait(
            self._authorizer.check(request, records, previous=stamp, control=control), control
        )
        if not isinstance(result, str):
            raise _Stopped(result)
        if not result or (stamp is not None and result != stamp):
            raise _Stopped(Failed(code="permission_denied"))
        return result

    async def run(
        self,
        request: ContextInput,
        *,
        control: CallControl,
        budget: RunBudget | None = None,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        continuation: tuple[ModelMessage, ...] = (),
        tools: tuple[ToolDefinition, ...] = (),
    ) -> TextRunResult:
        budget = budget or RunBudget()
        deadline = self._clock.monotonic() + budget.timeout_seconds
        control = CallControl(
            control.cancellation,
            min(deadline, control.deadline) if control.deadline is not None else deadline,
        )
        text = ""
        usage = Usage()
        try:
            if stopped := self._stopped(control):
                raise _Stopped(stopped)
            if request.agent.version != request.expected_agent_version:
                raise _Stopped(Failed(code="conflict"))
            if request.context.tenant_id != request.agent.tenant_id or (
                request.context.tenant_id != request.conversation.tenant_id
            ):
                raise _Stopped(Failed(code="permission_denied"))
            for message in request.conversation.messages:
                snapshot_message(message)
            request = request.model_copy(deep=True)
            stamp = await self._check(request, (), None, control)
            found = MemoryMatches(records=())
            if request.memory_scopes:
                searched = await self._wait(
                    self._memory.search(
                        MemorySearch(
                            context=request.context,
                            scopes=request.memory_scopes,
                            query=request.user_text,
                            limit=budget.memory_limit,
                        ),
                        control=control,
                    ),
                    control,
                )
                if not isinstance(searched, MemoryMatches):
                    raise _Stopped(searched)
                found = searched
            if len(found.records) > budget.memory_limit:
                raise _Stopped(Failed(code="invalid_arguments"))
            for record in found.records:
                if (
                    record.tenant_id != request.context.tenant_id
                    or record.content.scope not in request.memory_scopes
                    or record.status != "confirmed"
                    or not record.content.source_refs
                    or record.content.valid_from > self._clock.now()
                    or (
                        record.content.valid_to is not None
                        and record.content.valid_to <= self._clock.now()
                    )
                ):
                    raise _Stopped(Failed(code="permission_denied"))
                require_json(record.content.qualifiers)
            records = tuple(record.model_copy(deep=True) for record in found.records)
            await self._check(request, records, stamp, control)
            model_request = build_context(
                request, records, max_output_tokens=budget.max_output_tokens
            )

            from brainoid_agent_core.checkpoints import continuation_messages

            model_request = model_request.model_copy(
                update={
                    "messages": (*model_request.messages, *continuation_messages(continuation)),
                    "tools": tools,
                }
            )

            async def generate() -> TextRunResult:
                nonlocal text, usage
                snapshot = None
                calls = []
                count = 0
                stream = self._model.stream(model_request, control=control)
                caller_cancelled = False
                try:
                    while True:
                        event = await self._wait(anext(stream), control)
                        await self._check(request, records, stamp, control)
                        count += 1
                        if count > budget.max_model_events:
                            return TextRunResult(
                                text=text, usage=usage, terminal=Completed(finish_reason="length")
                            )
                        if isinstance(event, TextDelta):
                            available = budget.max_output_characters - len(text)
                            accepted = event.text[:available]
                            text += accepted
                            if accepted and on_text is not None:
                                await self._wait(on_text(accepted), control)
                            if len(event.text) > available:
                                return TextRunResult(
                                    text=text,
                                    usage=usage,
                                    terminal=Completed(finish_reason="length"),
                                )
                        elif isinstance(event, Usage):
                            usage = Usage(
                                **{
                                    field: getattr(event, field)
                                    if getattr(event, field) is not None
                                    else getattr(usage, field)
                                    for field in (
                                        "input_tokens",
                                        "output_tokens",
                                        "cached_input_tokens",
                                    )
                                }
                            )
                            if (
                                usage.output_tokens is not None
                                and usage.output_tokens > budget.max_output_tokens
                            ):
                                return TextRunResult(
                                    text=text,
                                    usage=usage,
                                    terminal=Completed(finish_reason="length"),
                                )
                        elif isinstance(event, ToolRequested):
                            calls.append(event.call)
                        elif isinstance(event, ModelResponse):
                            if snapshot is not None or event.message.role != "assistant":
                                raise _Stopped(Failed(code="invalid_arguments"))
                            snapshot = snapshot_message(event.message)
                        elif isinstance(event, Completed):
                            if event.finish_reason in ("stop", "tool_calls"):
                                if bool(calls) != (event.finish_reason == "tool_calls"):
                                    raise _Stopped(Failed(code="invalid_arguments"))
                                if snapshot is not None:
                                    visible = "".join(
                                        p.text for p in snapshot.parts if p.type == "text"
                                    )
                                    snapshot_calls = [
                                        p for p in snapshot.parts if p.type == "tool_call"
                                    ]
                                    if visible != text or snapshot_calls != calls:
                                        raise _Stopped(Failed(code="invalid_arguments"))
                                await self._check(request, records, stamp, control)
                            else:
                                snapshot = None
                            return TextRunResult(
                                text=text,
                                usage=usage,
                                terminal=event,
                                response=snapshot,
                                tool_calls=tuple(calls),
                            )
                        else:
                            raise _Stopped(event)
                except asyncio.CancelledError:
                    caller_cancelled = True
                    raise
                finally:
                    try:
                        await self._close(stream, control)
                    except Exception:
                        # Once the caller cancelled, cleanup failures must not
                        # replace that CancelledError with a business result.
                        if not caller_cancelled:
                            raise

            result = await generate()
            # aclose() is an await point too: release cannot authorize stale output.
            await self._check(request, records, stamp, control)
            return result
        except _Stopped as error:
            # Revoked authority never releases buffered text or continuation state.
            return TextRunResult(terminal=error.event, usage=usage)
        except StopAsyncIteration:
            return TextRunResult(terminal=Failed(code="stream_interrupted"), usage=usage)
        except Exception:
            return TextRunResult(terminal=Failed(code="invalid_arguments"), usage=usage)
