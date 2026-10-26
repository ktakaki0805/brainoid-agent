"""One durable model invocation, followed by a current-authority DB-only commit."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from threading import BoundedSemaphore, Thread
from typing import Annotated, Literal
from uuid import UUID, uuid5

from brainoid_agent_core.context import ContextInput
from brainoid_agent_core.core import RunBudget, TextAgentCore, TextRunResult
from brainoid_agent_core.ports import CallControl, Clock, ModelProvider
from brainoid_agent_core.types import (
    Completed,
    Failed,
    MemoryForget,
    MemoryForgetResult,
    MemoryMatches,
    MemoryRecord,
    MemoryRemember,
    MemoryRevise,
    MemorySearch,
    MemorySearchResult,
    MemoryWriteResult,
    ModelMessage,
    TextPart,
    ToolExecution,
    ToolReply,
    ToolResult,
)
from brainoid_agent_core.types.common import Stopped
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.models import Record
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs import events
from brainoid_runs.models import Message
from brainoid_runs.state import RunState, conversation_path, run_path
from brainoid_tools import Executor, Registry, ResultBinding
from brainoid_tools.executor import Authorizer as ToolAuthorizer
from pydantic import Field, JsonValue

from brainoid_worker.entry import Execution, Worker
from brainoid_worker.result_reuse import ResultReuse
from brainoid_worker.resume_run import Confirmations, RunToolAuthority
from brainoid_worker.run_event_writer import RunEventWriter, WriteBridge


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class NoCancellation:
    cancelled = False


class LiveCancellation:
    def __init__(self, handler: "RunTurn", job: Job) -> None:
        self.handler, self.job = handler, job
        self.cancelled = False
        self.error: Exception | None = None

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            try:
                await self.handler.reader.read(
                    lambda: self.handler.jobs.store.run(
                        lambda tx: self.handler.state.check(tx, self.job)
                    )
                )
            except (PolicyUnavailable, PersistenceUnavailable):
                self.error = PersistenceUnavailable()
                self.cancelled = True
                return
            except PolicyDenied:
                self.cancelled = True
                return


class EmptyMemory:
    """RUN-04 performs no retrieval. Unexpected candidates are denied by the authorizer."""

    async def search(self, request: MemorySearch, *, control: CallControl) -> MemorySearchResult:
        return MemoryMatches(records=())

    async def remember(self, request: MemoryRemember, *, control: CallControl) -> MemoryWriteResult:
        return Failed(code="permission_denied")

    async def revise(self, request: MemoryRevise, *, control: CallControl) -> MemoryWriteResult:
        return Failed(code="permission_denied")

    async def forget(self, request: MemoryForget, *, control: CallControl) -> MemoryForgetResult:
        return Failed(code="permission_denied")


class Outcome(Record):
    status: Literal["completed", "failed", "tools"]
    text: Annotated[str, Field(max_length=8000, repr=False)] = ""
    error_code: str | None = None
    # This DTO is stored only in the private checkpoint and never returned by HTTP.
    response: ModelMessage | None = Field(default=None, repr=False)


class ReadBridge:
    """Bounded read-only I/O; a cancelled await never waits for executor shutdown.

    Python cannot kill a blocking SDK call. At most four outstanding daemon reads
    remain until the SDK returns; saturation fails closed without growing a queue.
    Callbacks MUST be read-only. No provider or commit is ever delegated here.
    """

    def __init__(self) -> None:
        self.slots = BoundedSemaphore(4)

    async def read[T](self, work: Callable[[], T]) -> T:
        if not self.slots.acquire(blocking=False):
            raise PersistenceUnavailable()
        future: Future[T] = Future()

        def invoke() -> None:
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = work()
                    except Exception as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
            finally:
                self.slots.release()

        Thread(target=invoke, daemon=True, name="run-authority-reader").start()
        return await asyncio.wrap_future(future)


class Authorizer:
    def __init__(self, jobs: Jobs, state: RunState, job: Job, reader: ReadBridge) -> None:
        self.jobs, self.state, self.job = jobs, state, job
        self.reader = reader
        self.unavailable: AuthorizationUnavailable | PersistenceUnavailable | None = None

    async def check(
        self,
        request: ContextInput,
        memories: tuple[MemoryRecord, ...],
        *,
        previous: str | None,
        control: CallControl,
    ) -> str | Stopped:
        if memories:
            # Real retrieval/source expansion is MEM-04. An injected/broken adapter
            # cannot smuggle candidates through an implementation with no reader.
            return Failed(code="permission_denied")
        try:
            _, _, expected, receipt = await self.reader.read(
                lambda: self.jobs.store.run(lambda tx: self.state.check(tx, self.job))
            )
            if expected != request or (previous is not None and previous != receipt):
                return Failed(code="permission_denied")
            return receipt
        except PolicyUnavailable:
            self.unavailable = AuthorizationUnavailable()
            return Failed(code="unavailable")
        except PersistenceUnavailable as error:
            self.unavailable = error
            return Failed(code="unavailable")
        except PolicyDenied:
            return Failed(code="permission_denied")
        except asyncio.CancelledError:
            # Core's finite wait expired/cancelled while authority was unavailable.
            # Keep that category outside Core's normalized public result.
            self.unavailable = PersistenceUnavailable()
            raise


class RunTurn:
    def __init__(
        self,
        jobs: Jobs,
        model: ModelProvider,
        *,
        credential_epoch: ResourceId | None,
        clock: Clock | None = None,
        budget: RunBudget | None = None,
        tool_registry: Registry | None = None,
        tool_authorizer: ToolAuthorizer | None = None,
    ) -> None:
        self.jobs, self.model, self.clock = jobs, model, clock or SystemClock()
        if (tool_registry is None) != (tool_authorizer is None):
            raise ValueError("Both tool registry and business authorizer are required")
        self.tool_registry, self.tool_authorizer = tool_registry, tool_authorizer
        self.reader = ReadBridge()
        self.writer = WriteBridge()
        self.budget = (budget or RunBudget()).model_copy(
            update={
                "max_output_characters": min(8000, (budget or RunBudget()).max_output_characters)
            }
        )
        self.result_reuse = ResultReuse(tool_registry, tool_authorizer)
        self.state = RunState(
            jobs.ledger,
            now=jobs.now,
            credential_epoch=credential_epoch,
            allow_untracked_bootstrap=jobs.allow_untracked,
            check_results=self.result_reuse.check,
        )

    def _start(self, execution: Execution) -> tuple[Job, ContextInput, Document]:
        def start(tx: Transaction) -> tuple[Job, ContextInput, Document]:
            job = self.jobs._live(tx, execution.lease)
            if job is None:
                raise LeaseLost()
            run, _, request, _ = self.state.check(tx, job)
            if run["status"] == "queued":
                events.append(
                    tx,
                    run,
                    events.Started(
                        run_id=job.operation_id,
                        seq=run.get("last_event_seq", 0) + 1,
                        occurred_at=self.jobs.now(),
                    ),
                )
                tx.set(
                    run_path(job.tenant_id, job.operation_id),
                    {**run, "status": "running", "version": run["version"] + 1},
                )
            return job, request, run

        try:
            job, request, run = self.jobs.store.run(start)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None

        return job, request, run

    def run(self, execution: Execution) -> JsonValue:
        try:
            return self._run(execution)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None

    def _run(self, execution: Execution) -> JsonValue:
        job, request, run = self._start(execution)
        while True:
            execution.check(renew=True)
            if run.get("confirmation_decision") == "outcome_unknown":
                return Outcome(status="failed", error_code="outcome_unknown").model_dump(
                    mode="json"
                )
            if run.get("confirmation_decision") in ("declined", "expired"):
                reason = run["confirmation_decision"]
                text = run.get("streamed_text", "") + (
                    "確認が拒否されたため、確認待ちの操作は実行しませんでした。"
                    if reason == "declined"
                    else "確認期限が過ぎたため、確認待ちの操作は実行しませんでした。"
                )
                return Outcome(status="completed", text=text).model_dump(mode="json")
            step = run.get("model_step", 0)
            if step >= 8:
                return Outcome(status="failed", error_code="tool_limit").model_dump(mode="json")
            outcome = Outcome.model_validate(self._generate(execution, job, request, run))
            if outcome.status != "tools":
                return outcome.model_copy(
                    update={"text": run.get("completed_text", "") + outcome.text}
                ).model_dump(mode="json")
            if (
                self.tool_registry is None
                or self.tool_authorizer is None
                or outcome.response is None
            ):
                return Outcome(status="failed", error_code="tools_not_supported").model_dump(
                    mode="json"
                )
            calls = [p for p in outcome.response.parts if p.type == "tool_call"]
            if not 1 <= len(calls) <= 8 or len({p.call_key for p in calls}) != len(calls):
                return Outcome(status="failed", error_code="invalid_arguments").model_dump(
                    mode="json"
                )
            requests = [
                ToolExecution(
                    context=request.context,
                    tool_call_id=ResourceId(
                        str(uuid5(UUID(job.operation_id), f"tool:{step}:{index}"))
                    ),
                    idempotency_key=f"{job.operation_id}:{step}:{index}",
                    call=call,
                )
                for index, call in enumerate(calls)
            ]

            def bind(tx: Transaction, requests: list[ToolExecution] = requests) -> None:
                current, _, _, _ = self.state.check(tx, job)
                ids = list(
                    dict.fromkeys(
                        [*current.get("tool_call_ids", []), *(r.tool_call_id for r in requests)]
                    )
                )
                tx.set(run_path(job.tenant_id, job.operation_id), {**current, "tool_call_ids": ids})

            self.jobs.store.run(bind)
            tool_authority = RunToolAuthority(self.state, job, self.tool_authorizer)
            executor = Executor(
                self.jobs.store,
                self.tool_registry,
                tool_authority,
                self.clock,
                infrastructure_errors=(
                    PolicyUnavailable,
                    PersistenceUnavailable,
                    AuthorizationUnavailable,
                ),
            )
            results: list[ToolResult] = []
            for tool_request in requests:
                execution.check(renew=True)

                # Executor owns the durable start/result marker for this external effect.
                async def execute_tool(
                    tool_request: ToolExecution = tool_request, executor: Executor = executor
                ) -> object:
                    cancellation = LiveCancellation(self, job)
                    watcher = asyncio.create_task(cancellation.watch())
                    try:
                        return await executor.execute(
                            tool_request,
                            control=CallControl(
                                cancellation, self.clock.monotonic() + self.budget.timeout_seconds
                            ),
                        )
                    finally:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
                        if cancellation.error is not None:
                            raise cancellation.error

                result = asyncio.run(execute_tool())
                if tool_authority.unavailable is not None:
                    raise tool_authority.unavailable
                if not isinstance(result, ToolResult):
                    return Outcome(status="failed", error_code="tool_rejected").model_dump(
                        mode="json"
                    )
                if result.status == "waiting_confirmation":
                    Confirmations(self.jobs, self.state).suspend(execution, tool_request, result)
                if result.status == "outcome_unknown":
                    return Outcome(status="failed", error_code="outcome_unknown").model_dump(
                        mode="json"
                    )
                results.append(result)

            def consume(
                tx: Transaction,
                requests: list[ToolExecution] = requests,
                results: list[ToolResult] = results,
                step: int = step,
                outcome: Outcome = outcome,
            ) -> Document:
                assert outcome.response is not None
                current, _, _, _ = self.state.check(tx, job)
                if current.get("model_step", 0) != step:
                    raise PolicyDenied()
                for tool_request, result in zip(requests, results, strict=True):
                    saved = tx.get(
                        f"tenants/{job.tenant_id}/tool_executions/{tool_request.tool_call_id}"
                    )
                    if saved is None or saved.get("result") != result.model_dump(mode="json"):
                        raise PolicyDenied()
                replies = ModelMessage(
                    role="tool",
                    parts=tuple(
                        ToolReply(
                            call_key=r.call.call_key,
                            name=r.call.name,
                            result={
                                "status": value.status,
                                "result": value.result,
                                "error_code": value.error_code,
                            },
                            is_error=value.status == "failed",
                        )
                        for r, value in zip(requests, results, strict=True)
                    ),
                )
                full_text = current.get("completed_text", "") + outcome.text
                prefix = current.get("streamed_text", "")
                if not full_text.startswith(prefix):
                    raise PolicyDenied()
                updated = {
                    **current,
                    "model_step": step + 1,
                    "tool_bindings": [
                        *current.get("tool_bindings", []),
                        *(
                            ResultBinding(request=request, result=result).model_dump(mode="json")
                            for request, result in zip(requests, results, strict=True)
                        ),
                    ],
                    "completed_text": current.get("completed_text", "") + outcome.text,
                    "continuation": [
                        *current.get("continuation", []),
                        outcome.response.model_dump(mode="json"),
                        replies.model_dump(mode="json"),
                    ],
                }
                self.result_reuse.current(tx, job, updated)
                events.append_text(tx, updated, full_text[len(prefix) :], self.jobs.now())
                updated.pop("waiting_proposal_id", None)
                updated.pop("confirmation_decision", None)
                tx.set(run_path(job.tenant_id, job.operation_id), updated)

                return updated

            run = self.jobs.store.run(consume)

    def _generate(
        self, execution: Execution, job: Job, request: ContextInput, run: Document
    ) -> JsonValue:
        def invoke(key: str) -> JsonValue:
            authorizer = Authorizer(self.jobs, self.state, job, self.reader)
            core = TextAgentCore(self.model, EmptyMemory(), authorizer, clock=self.clock)
            writer = RunEventWriter(self.jobs, self.state, job, self.clock, self.writer)
            writer.persisted = run.get("completed_text", "")
            cancellation = LiveCancellation(self, job)

            async def generate() -> TextRunResult:
                writer.start()
                watcher = asyncio.create_task(cancellation.watch())
                try:
                    return await core.run(
                        request,
                        control=CallControl(cancellation, None),
                        budget=self.budget.model_copy(
                            update={
                                "max_output_characters": max(
                                    1,
                                    self.budget.max_output_characters
                                    - len(run.get("completed_text", ""))
                                    - (100 if self.tool_registry is not None else 0),
                                )
                            }
                        ),
                        on_text=writer.add,
                        continuation=tuple(
                            ModelMessage.model_validate(m) for m in run.get("continuation", [])
                        ),
                        tools=()
                        if self.tool_registry is None
                        else self.tool_registry.definitions(request.agent.allowed_tools),
                    )
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                    await writer.close()

            result = asyncio.run(generate())
            if cancellation.error is not None:
                raise cancellation.error
            if isinstance(writer.error, (AuthorizationUnavailable, PersistenceUnavailable)):
                raise writer.error
            if writer.error is not None:
                raise PolicyDenied()
            # Core's public errors deliberately normalize exceptions. Preserve the
            # infrastructure category outside Core so Worker can defer/reconcile it.
            if authorizer.unavailable is not None:
                raise authorizer.unavailable
            if isinstance(result.terminal, Completed):
                if result.terminal.finish_reason == "tool_calls":
                    outcome = Outcome(
                        status="tools",
                        text=result.text,
                        response=(
                            result.response
                            or ModelMessage(
                                role="assistant",
                                parts=(
                                    *((TextPart(text=result.text),) if result.text else ()),
                                    *result.tool_calls,
                                ),
                            )
                        ),
                    )
                elif result.terminal.finish_reason == "content_filter" or not result.text:
                    outcome = Outcome(
                        status="failed",
                        error_code=(
                            "content_filter"
                            if result.terminal.finish_reason == "content_filter"
                            else "empty_response"
                        ),
                    )
                else:
                    outcome = Outcome(
                        status="completed", text=result.text, response=result.response
                    )
            else:
                outcome = Outcome(
                    status="failed",
                    error_code=(
                        result.terminal.code
                        if isinstance(result.terminal, Failed)
                        else result.terminal.type
                    ),
                )
            # TextRunResult excludes response from serialization; copy the supplied
            # full ModelMessage explicitly, without rebuilding its provider state.
            return outcome.model_dump(mode="json")

        step = run.get("model_step", 0)
        return execution.effect("model" if step == 0 else f"model.{step}", invoke)

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        outcome = Outcome.model_validate(result)
        try:
            run, parent, _, _ = self.state.check(tx, job)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        message_id = ResourceId(str(uuid5(UUID(str(job.operation_id)), "final-message")))
        message_path = (
            conversation_path(job.tenant_id, job.grant.ref.id) + "/messages/" + message_id
        )
        existing = tx.get(message_path)
        if existing is not None:
            raise PolicyDenied()
        updated = {**run, "status": outcome.status, "version": run["version"] + 1}
        changes = {
            **parent,
            "active_run_id": None,
            "version": parent["version"] + 1,
            "history_revision": parent.get("history_revision", 0) + 1,
            "updated_at": self.jobs.now().isoformat(),
        }
        if outcome.status == "completed":
            prefix = run.get("streamed_text", "")
            if not outcome.text.startswith(prefix):
                raise PolicyDenied()
            events.append_text(tx, updated, outcome.text[len(prefix) :], self.jobs.now())
            events.append(
                tx,
                updated,
                events.Completed(
                    run_id=job.operation_id,
                    seq=updated.get("last_event_seq", 0) + 1,
                    occurred_at=self.jobs.now(),
                    payload=events.CompletedPayload(message_id=message_id),
                ),
            )
            message = Message(
                id=message_id,
                role="assistant",
                text=outcome.text,
                run_id=job.operation_id,
                created_at=self.jobs.now(),
            )
            updated["final_message_id"] = message_id
            changes["next_sequence"] = parent["next_sequence"] + 1
            tx.create(
                message_path,
                {
                    **message.model_dump(mode="json"),
                    "tenant_id": job.tenant_id,
                    "conversation_id": job.grant.ref.id,
                    "version": 1,
                    "deletion_epoch": 0,
                    "deleted": False,
                    "suppress_reuse": False,
                    "sequence": parent["next_sequence"],
                    "tool_bindings": run.get("tool_bindings", []),
                    "model_message": None
                    if run.get("continuation") or outcome.response is None
                    else outcome.response.model_dump(mode="json"),
                    "model_messages": None
                    if not run.get("continuation")
                    else [
                        *run["continuation"],
                        (
                            outcome.response
                            or ModelMessage(
                                role="assistant",
                                parts=(
                                    TextPart(
                                        text=outcome.text[len(run.get("completed_text", "")) :]
                                    ),
                                ),
                            )
                        ).model_dump(mode="json"),
                    ],
                },
            )
        else:
            updated["error_code"] = outcome.error_code or "execution_failed"
            events.append(
                tx,
                updated,
                events.Failed(
                    run_id=job.operation_id,
                    seq=updated.get("last_event_seq", 0) + 1,
                    occurred_at=self.jobs.now(),
                    payload=events.FailedPayload(error_code=updated["error_code"]),
                ),
            )
        tx.set(run_path(job.tenant_id, job.operation_id), updated)
        tx.set(conversation_path(job.tenant_id, job.grant.ref.id), changes)


def text_worker(
    jobs: Jobs,
    model: ModelProvider,
    *,
    credential_epoch: ResourceId | None,
    clock: Clock | None = None,
) -> Worker:
    """Explicit local composition; production injects its verified Tasks caller."""
    from brainoid_worker.local import LocalCaller

    return Worker(
        jobs,
        LocalCaller(),
        {"run_turn": RunTurn(jobs, model, credential_epoch=credential_epoch, clock=clock)},
    )
