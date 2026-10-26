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
from brainoid_agent_core.core import RunBudget, TextAgentCore
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
)
from brainoid_agent_core.types.common import Stopped
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.models import Record
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.models import Message
from brainoid_runs.state import RunState, conversation_path, run_path
from pydantic import Field, JsonValue

from brainoid_worker.entry import Execution, Worker


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class NoCancellation:
    @property
    def cancelled(self) -> bool:
        return False


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
    status: Literal["completed", "failed"]
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
    ) -> None:
        self.jobs, self.model, self.clock = jobs, model, clock or SystemClock()
        self.reader = ReadBridge()
        self.budget = (budget or RunBudget()).model_copy(
            update={
                "max_output_characters": min(8000, (budget or RunBudget()).max_output_characters)
            }
        )
        self.state = RunState(
            jobs.ledger,
            now=jobs.now,
            credential_epoch=credential_epoch,
            allow_untracked_bootstrap=jobs.allow_untracked,
        )

    def run(self, execution: Execution) -> JsonValue:
        def start(tx: Transaction) -> tuple[Job, ContextInput]:
            job = self.jobs._live(tx, execution.lease)
            if job is None:
                raise LeaseLost()
            run, _, request, _ = self.state.check(tx, job)
            if run["status"] == "queued":
                tx.set(
                    run_path(job.tenant_id, job.operation_id),
                    {**run, "status": "running", "version": run["version"] + 1},
                )
            return job, request

        try:
            job, request = self.jobs.store.run(start)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None

        def invoke(key: str) -> JsonValue:
            authorizer = Authorizer(self.jobs, self.state, job, self.reader)
            core = TextAgentCore(self.model, EmptyMemory(), authorizer, clock=self.clock)
            result = asyncio.run(
                core.run(
                    request,
                    control=CallControl(NoCancellation(), None),
                    budget=self.budget,
                )
            )
            # Core's public errors deliberately normalize exceptions. Preserve the
            # infrastructure category outside Core so Worker can defer/reconcile it.
            if authorizer.unavailable is not None:
                raise authorizer.unavailable
            if isinstance(result.terminal, Completed):
                if result.terminal.finish_reason == "tool_calls":
                    outcome = Outcome(status="failed", error_code="tools_not_supported")
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

        return execution.effect("model", invoke)

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
                    "model_message": None
                    if outcome.response is None
                    else outcome.response.model_dump(mode="json"),
                },
            )
        else:
            updated["error_code"] = outcome.error_code or "execution_failed"
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
