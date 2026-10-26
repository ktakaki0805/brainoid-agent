"""One durable model invocation, followed by a current-authority DB-only commit."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from threading import BoundedSemaphore, Thread
from typing import Annotated, Literal
from uuid import UUID, uuid5

from brainoid_agent_core.context import ContextInput, ConversationSummary, SummarySource
from brainoid_agent_core.core import RunBudget, TextAgentCore, TextRunResult
from brainoid_agent_core.ports import CallControl, Clock, ModelProvider
from brainoid_agent_core.types import (
    Completed,
    Failed,
    MemoryRecord,
    ModelMessage,
    TextPart,
    ToolExecution,
    ToolResult,
)
from brainoid_agent_core.types.common import Stopped
from brainoid_contracts import ResourceId
from brainoid_contracts.guards import GuardedTransactions, ReleaseGuard, release_guard
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import operation_path
from brainoid_memory.vector_store import VectorSearch
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.models import Record
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs import events
from brainoid_runs.models import Message
from brainoid_runs.state import RunState, conversation_path, run_path
from brainoid_tools import Executor, Registry, ResultBinding
from brainoid_tools.executor import Authorizer as ToolAuthorizer
from brainoid_tools.memory import register_memory
from brainoid_tools.notes import register_notes
from brainoid_tools.reminders import register_reminders
from brainoid_tools.tasks import register_tasks
from brainoid_usage import Amounts, QuotaExceeded, Settlement, UsageLedger
from pydantic import Field, JsonValue

from brainoid_worker.entry import Execution, Worker
from brainoid_worker.memory import BoundMemory, MemoryRuntime, model_record
from brainoid_worker.metered_model import MeteredModel
from brainoid_worker.model_projection import tool_reply
from brainoid_worker.result_reuse import ResultReuse
from brainoid_worker.resume_run import Confirmations, RunToolAuthority
from brainoid_worker.run_event_writer import RunEventWriter, WriteBridge
from brainoid_worker.usage_reconciler import UsageReconciler


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
    def __init__(
        self, jobs: Jobs, state: RunState, job: Job, reader: ReadBridge, memory: MemoryRuntime
    ) -> None:
        self.jobs, self.state, self.job = jobs, state, job
        self.reader = reader
        self.memory = memory
        self.unavailable: AuthorizationUnavailable | PersistenceUnavailable | None = None
        self._release: tuple[str, ReleaseGuard] | None = None

    def release(self, receipt: str) -> Stopped | None:
        if self._release is None or self._release[0] != receipt:
            return Failed(code="permission_denied")
        try:
            self._release[1].finish()
        except PolicyDenied:
            self._release = None
            return Failed(code="permission_denied")
        return None

    def release_current(self) -> Stopped | None:
        """Final provider boundary uses the same capability retained for this stage."""
        if self._release is None:
            return Failed(code="permission_denied")
        return self.release(self._release[0])

    async def check(
        self,
        request: ContextInput,
        memories: tuple[MemoryRecord, ...],
        *,
        previous: str | None,
        control: CallControl,
    ) -> str | Stopped:
        self._release = None
        try:

            def read(
                tx: Transaction,
            ) -> tuple[ContextInput, str, ReleaseGuard]:
                _, _, expected, stamp = self.state.check(tx, self.job)
                access = self.memory.open(tx, self.job, control)
                current = tuple(access.get(record.memory_id, for_model=True) for record in memories)
                if tuple(model_record(record) for record in current) != memories:
                    raise PolicyDenied()
                access.finish_records(current)
                return expected, stamp, release_guard(tx)

            expected, receipt, guard = await self.reader.read(lambda: self.jobs.store.run(read))
            guard.finish()
            if expected != request or (previous is not None and previous != receipt):
                return Failed(code="permission_denied")
            self._release = (receipt, guard)
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
            # A live revocation can cancel this read while the watcher is stopping
            # Core. That is not evidence of a storage outage. Infrastructure watcher
            # errors still propagate via LiveCancellation.error; an unresponsive
            # read that exceeds Core's deadline remains unavailable.
            if not control.cancellation.cancelled:
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
        vector_search: VectorSearch | None = None,
        deletion_ledger: DeletionLedger | None = None,
    ) -> None:
        # Every run/tool/stream/final transaction shares guards, including the outer
        # Jobs.complete callback and read responses delayed after the callback ends.
        if not isinstance(jobs.store, GuardedTransactions):
            jobs.store = GuardedTransactions(jobs.store)
        self.jobs, self.model, self.clock = jobs, model, clock or SystemClock()
        if (tool_registry is None) != (tool_authorizer is None):
            raise ValueError("Both tool registry and business authorizer are required")
        self.tool_registry, self.tool_authorizer = tool_registry, tool_authorizer
        self.usage = UsageLedger(jobs.store, now=jobs.now)
        self.reader = ReadBridge()
        self.writer = WriteBridge()
        self.budget = (budget or RunBudget()).model_copy(
            update={
                "max_output_characters": min(8000, (budget or RunBudget()).max_output_characters)
            }
        )
        self.memory = MemoryRuntime(
            self, vector_search=vector_search, deletion_ledger=deletion_ledger
        )
        self.tool_registry = tool_registry.copy() if tool_registry is not None else Registry()
        register_memory(self.tool_registry, self.memory)
        register_notes(self.tool_registry, self.memory.notes)
        register_tasks(self.tool_registry, self.memory.tasks)
        register_reminders(self.tool_registry, self.memory.reminders)
        self.tool_authorizer = tool_authorizer or self.memory
        self.execution_authorizer = self.memory
        self.result_reuse = ResultReuse(self.tool_registry, self.execution_authorizer)
        self.state = RunState(
            jobs.ledger,
            now=jobs.now,
            credential_epoch=credential_epoch,
            allow_untracked_bootstrap=jobs.allow_untracked,
            check_results=self._check_results,
            load_summary=self._load_summary,
        )

    def _load_summary(self, tx: Transaction, job: Job, before: int) -> ConversationSummary | None:
        # RunState has already authorized this canonical parent in the same TX.
        # No summary means no summary-specific caller/lease or dependency read set.
        run = tx.get(run_path(job.tenant_id, job.operation_id))
        if run is not None and run.get("reminder_update") is not None:
            self.memory.reminders.terminal_update(tx, job, run)
            return None
        if run is not None and run.get("task_update") is not None:
            self.memory.tasks.terminal_update(tx, job, run)
            return None
        if run is not None and run.get("note_update") is not None:
            self.memory.notes.terminal_update(tx, job, run)
            return None
        if not tx.collection(conversation_path(job.tenant_id, job.grant.ref.id) + "/summaries"):
            return None
        access = self.memory.open(tx, job)
        summaries = access._repo.summaries(tx, job.grant.context)
        record = summaries.latest(job.grant.ref.id, before=before)
        if record is None:
            return None
        return ConversationSummary(
            id=record.id,
            text=record.text,
            source_refs=tuple(
                SummarySource(
                    resource_id=source.resource_id,
                    version=source.version,
                    deletion_epoch=source.deletion_epoch,
                )
                for source in record.sources.messages
                if source.included
            ),
        )

    def _check_results(
        self, tx: Transaction, job: Job, run: Document, history: list[Document]
    ) -> str:
        if run.get("reminder_update") is not None:
            return self.memory.reminders.terminal_update(tx, job, run)
        if run.get("task_update") is not None:
            return self.memory.tasks.terminal_update(tx, job, run)
        if run.get("note_update") is not None:
            return self.memory.notes.terminal_update(tx, job, run)
        result = self.result_reuse.check(tx, job, run, history)
        self.memory.check_sources(tx, job, run, history)
        return result

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

    def _forget_outcome(self, job: Job) -> JsonValue | None:
        def read(tx: Transaction) -> JsonValue | None:
            run, _, _, _ = self.state.check(tx, job)
            identity = run.get("forget_operation_id")
            if identity is None:
                return None
            proof = tx.get(f"tenants/{job.tenant_id}/memory_forget_effects/{identity}")
            if (
                proof is None
                or proof.get("operation_id") != identity
                or proof.get("run_id") != job.operation_id
            ):
                raise PolicyDenied()
            tool_id = proof.get("tool_call_id")
            if tool_id not in run.get("tool_call_ids", []):
                raise PolicyDenied()
            saved = tx.get(f"tenants/{job.tenant_id}/tool_executions/{tool_id}")
            if (
                saved is None
                or saved.get("name") not in ("memory.forget", "notes.delete", "tasks.delete")
                or saved.get("effect_id") != identity
                or saved["authority"]["run_id"] != job.operation_id
            ):
                raise PolicyDenied()
            access = self.memory.open(tx, job)
            self.memory.forgetting().operation(
                access, tx.get(operation_path(job.tenant_id, ResourceId(identity)))
            )
            return Outcome(
                status="completed",
                text=(
                    "ノートの削除を受け付けました。消去処理を待っています。"
                    if saved.get("name") == "notes.delete"
                    else "タスクの削除を受け付けました。消去処理を待っています。"
                    if saved.get("name") == "tasks.delete"
                    else run.get("streamed_text", "")
                    + "忘却を受け付けました。対象記憶と元出典のモデル再利用を停止し、消去処理を待っています。別の出典や新しい発言から同じ内容を知る可能性は残ります。"
                ),
            ).model_dump(mode="json")

        return self.jobs.store.run(read)

    def _direct_update_outcome(self, job: Job) -> JsonValue | None:
        def read(tx: Transaction) -> JsonValue | None:
            run, _, _, _ = self.state.check(tx, job)
            runtime = (
                self.memory.reminders
                if run.get("reminder_update") is not None
                else self.memory.tasks
                if run.get("task_update") is not None
                else self.memory.notes
            )
            if run.get(runtime.update_marker) is None:
                return None
            runtime.terminal_update(tx, job, run)
            return Outcome(status="completed", text=runtime.UPDATE_ACK).model_dump(mode="json")

        return self.jobs.store.run(read)

    def _run(self, execution: Execution) -> JsonValue:
        job, request, run = self._start(execution)
        while True:
            execution.check(renew=True)
            if any(
                run.get(marker) is not None
                for marker in ("note_update", "task_update", "reminder_update")
            ):
                if (updated := self._direct_update_outcome(job)) is not None:
                    return updated
            if run.get("forget_operation_id") is not None:
                if (forgotten := self._forget_outcome(job)) is not None:
                    return forgotten
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
            tool_authority = RunToolAuthority(self.state, job, self.execution_authorizer)
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
                if tool_request.call.name in (
                    "notes.update",
                    "tasks.update",
                    "reminders.update",
                    "reminders.snooze",
                ):
                    if (updated := self._direct_update_outcome(job)) is not None:
                        return updated
                if tool_request.call.name in ("memory.forget", "notes.delete", "tasks.delete"):
                    if (forgotten := self._forget_outcome(job)) is not None:
                        return forgotten
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
                        tool_reply(r.call, value)
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
        step = run.get("model_step", 0)
        usage_key = "model" if step == 0 else f"model.{step}"

        def invoke(key: str) -> JsonValue:
            def reserve(tx: Transaction) -> int:
                if self.jobs._live(tx, execution.lease) is None:
                    raise LeaseLost()
                policy = self.usage.policy(tx, job.tenant_id)
                self.usage.reserve(
                    tx,
                    job.tenant_id,
                    job.operation_id,
                    usage_key,
                    Amounts(
                        input_tokens=policy.model_input_tokens,
                        output_tokens=self.budget.max_output_tokens,
                    ),
                )
                return policy.model_input_tokens

            try:
                input_limit = self.jobs.store.run(reserve)
            except QuotaExceeded:
                return Outcome(status="failed", error_code="rate_limited").model_dump(mode="json")
            authorizer = Authorizer(self.jobs, self.state, job, self.reader, self.memory)
            metered = MeteredModel(self.model, input_limit, before_start=authorizer.release_current)
            memory_store = BoundMemory(self.memory, job)
            core = TextAgentCore(metered, memory_store, authorizer, clock=self.clock)
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
                                    - (
                                        100
                                        if self.tool_registry is not None
                                        and self.tool_registry.definitions(
                                            request.agent.allowed_tools
                                        )
                                        else 0
                                    ),
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

            result: TextRunResult | None = None
            try:
                result = asyncio.run(generate())
            finally:
                actual = Settlement()
                if not metered.started:
                    actual = Settlement(input_tokens=0, output_tokens=0, voice_seconds=0)
                elif (
                    result is not None
                    and metered.completed
                    and isinstance(result.terminal, Completed)
                ):
                    actual = Settlement(
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        voice_seconds=0,
                    )
                self.usage.settle(
                    job.tenant_id, job.operation_id, usage_key, actual, observed=metered.observed
                )
            assert result is not None
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
            if memory_store.unavailable is not None:
                raise memory_store.unavailable
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

    def reconcile(self, job: Job) -> None:
        UsageReconciler(self.jobs).reconcile(job.tenant_id, job.operation_id, provider_stopped=True)

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        outcome = Outcome.model_validate(result)
        try:
            run, parent, _, _ = self.state.check(tx, job)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        if run.get("reminder_update") is not None and outcome != Outcome(
            status="completed", text=self.memory.reminders.UPDATE_ACK
        ):
            raise PolicyDenied()
        if run.get("task_update") is not None and outcome != Outcome(
            status="completed", text=self.memory.tasks.UPDATE_ACK
        ):
            raise PolicyDenied()
        if run.get("note_update") is not None and outcome != Outcome(
            status="completed", text=self.memory.notes.UPDATE_ACK
        ):
            raise PolicyDenied()
        message_id = ResourceId(str(uuid5(UUID(str(job.operation_id)), "final-message")))
        message_path = (
            conversation_path(job.tenant_id, job.grant.ref.id) + "/messages/" + message_id
        )
        existing = tx.get(message_path)
        if existing is not None:
            raise PolicyDenied()
        release_usage = self.usage.prepare_release(tx, job.tenant_id, job.operation_id)
        release_usage()
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
                    "suppress_reuse": bool(run.get("forget_operation_id")),
                    "sequence": parent["next_sequence"],
                    "tool_bindings": run.get("tool_bindings", []),
                    "memory_records": run.get("memory_records", []),
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
    deletion_ledger: DeletionLedger | None = None,
) -> Worker:
    """Explicit local composition; production injects its verified Tasks caller."""
    from brainoid_worker.local import LocalCaller

    return Worker(
        jobs,
        LocalCaller(),
        {
            "run_turn": RunTurn(
                jobs,
                model,
                credential_epoch=credential_epoch,
                clock=clock,
                deletion_ledger=deletion_ledger,
            )
        },
    )
