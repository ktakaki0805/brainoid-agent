"""One bounded writer per handler, with current authority inside each transaction."""

import asyncio
from concurrent.futures import Future
from threading import BoundedSemaphore, Event, Thread

from brainoid_agent_core.ports import Clock
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.events import append_text
from brainoid_runs.state import RunState, run_path


class WriteBridge:
    """Single outstanding daemon write; cancelled work is fenced inside its TX.

    Unlike the read bridge this never accepts arbitrary callbacks. A late SDK commit
    conflicts with every lease/authority record read by RunState. Cancellation stops
    new writes and callbacks that have not reached their final write phase.
    """

    def __init__(self) -> None:
        self.slot = BoundedSemaphore(1)

    async def append(
        self,
        jobs: Jobs,
        state: RunState,
        job: Job,
        prefix: str,
        text: str,
        stopped: Event,
    ) -> None:
        if not self.slot.acquire(blocking=False):
            raise PersistenceUnavailable()
        # Future/task results contain no text or provider exception object.
        future: Future[None] = Future()
        future.set_running_or_notify_cancel()
        error: list[Exception] = []

        def write(tx: Transaction) -> None:
            if stopped.is_set():
                raise PolicyDenied()
            run, _, _, _ = state.check(tx, job)
            if stopped.is_set():
                raise PolicyDenied()
            if run.get("streamed_text", "") == prefix + text:
                return
            if run.get("streamed_text", "") != prefix:
                raise PolicyDenied()
            append_text(tx, run, text, jobs.now())
            run["version"] += 1
            tx.set(run_path(job.tenant_id, job.operation_id), run)

        def invoke() -> None:
            try:
                try:
                    jobs.store.run(write)
                except PolicyUnavailable:
                    error.append(AuthorizationUnavailable())
                except (PolicyDenied, PersistenceUnavailable) as caught:
                    error.append(caught)
                except Exception:
                    error.append(PersistenceUnavailable())
            finally:
                self.slot.release()
                if not future.cancelled():
                    future.set_result(None)

        try:
            Thread(target=invoke, daemon=True, name="run-event-writer").start()
        except Exception:
            self.slot.release()
            raise PersistenceUnavailable() from None
        try:
            await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            stopped.set()
            raise
        if error:
            raise error[0]


class RunEventWriter:
    def __init__(
        self,
        jobs: Jobs,
        state: RunState,
        job: Job,
        clock: Clock,
        bridge: WriteBridge,
    ) -> None:
        self.jobs, self.state, self.job, self.clock, self.bridge = jobs, state, job, clock, bridge
        self.pending = ""
        self.persisted = ""
        self.stopped = Event()
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.due = 0.0
        self.error: Exception | None = None
        self.timer: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.timer = asyncio.create_task(self._timer())

    async def add(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        async with self.lock:
            if not self.pending:
                self.due = self.clock.monotonic() + 0.25
            self.pending += text
            self.ready.set()
            if len(self.pending.encode("utf-8")) >= 1024:
                try:
                    await self._flush()
                except (AuthorizationUnavailable, PersistenceUnavailable, PolicyDenied) as error:
                    self.error = error
                    raise

    async def _flush(self) -> None:
        if self.pending:
            await self.bridge.append(
                self.jobs, self.state, self.job, self.persisted, self.pending, self.stopped
            )
            self.persisted += self.pending
            self.pending = ""
            self.ready.clear()

    async def _timer(self) -> None:
        try:
            while True:
                await self.ready.wait()
                await self.clock.sleep(max(0.0, self.due - self.clock.monotonic()))
                async with self.lock:
                    if self.clock.monotonic() >= self.due:
                        await self._flush()
        except asyncio.CancelledError:
            raise
        except (AuthorizationUnavailable, PersistenceUnavailable, PolicyDenied) as error:
            self.error = error
        except Exception:
            self.error = PersistenceUnavailable()

    async def close(self) -> None:
        # No final flush here: commit atomically appends the remaining successful
        # text with final message and terminal event, including checkpoint replay.
        self.stopped.set()
        if self.timer is None:
            return
        interrupted = False
        for _ in range(2):
            self.timer.cancel()
            try:
                await asyncio.wait((self.timer,), timeout=0.25)
            except asyncio.CancelledError:
                interrupted = True
            if self.timer.done():
                if not self.timer.cancelled():
                    self.timer.exception()
                if interrupted:
                    raise asyncio.CancelledError
                return
        self.error = PersistenceUnavailable()
        self.timer.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        if interrupted:
            raise asyncio.CancelledError
