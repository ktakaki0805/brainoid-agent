"""Daemon-owned lifetime, independent of UI requests and local sessions."""

import asyncio
import signal
import time
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from brainoid_contracts.identifiers import ResourceId
from brainoid_contracts.time import format_utc

from brainoid_device.owned_child import OwnedChild, create_owned_child


class Runtime:
    def __init__(self, shutdown_seconds: float = 3.0) -> None:
        self.instance_id = ResourceId(str(uuid4()))
        self.started_at = format_utc(datetime.now(UTC))
        self.running = False
        self.shutdown_seconds = shutdown_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._children: list[OwnedChild] = []
        self._child_watchers: set[asyncio.Task[None]] = set()
        self._child_lock = asyncio.Lock()
        self._spawns: set[asyncio.Task[OwnedChild]] = set()
        self.resource_failed = False
        self._generation = 0
        self._operations_fenced = False
        self._private_cleanup: list[Callable[[], None]] = []

    @property
    def generation(self) -> int:
        return self._generation

    def permits_work(self, generation: int) -> bool:
        """Adapters must recheck this immediately before publishing/using private work."""
        return self.running and not self._operations_fenced and generation == self._generation

    def fence_operations(self) -> None:
        self._operations_fenced = True
        self._generation += 1

    def register_private_cleanup(self, cleanup: Callable[[], None]) -> None:
        if self._operations_fenced:
            raise RuntimeError("operations are stopped")
        self._private_cleanup.append(cleanup)

    def clear_private(self) -> None:
        failed = False
        for cleanup in self._private_cleanup:
            try:
                cleanup()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("private cleanup failed")

    def start_task(self, work: Coroutine[object, object, None]) -> asyncio.Task[None]:
        if not self.permits_work(self.generation):
            work.close()
            raise RuntimeError("service is not running")
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def start_child(self, argv: Sequence[str]) -> OwnedChild:
        spawn = asyncio.create_task(self._spawn_child(argv))
        self._spawns.add(spawn)
        spawn.add_done_callback(self._spawn_done)
        # Caller cancellation must not cancel subprocess creation before ownership
        # is recorded. close() waits on the same lock before collecting children.
        return await asyncio.shield(spawn)

    def _spawn_done(self, task: asyncio.Task[OwnedChild]) -> None:
        self._spawns.discard(task)
        if not task.cancelled():
            task.exception()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.resource_failed = True

    async def _spawn_child(self, argv: Sequence[str]) -> OwnedChild:
        async with self._child_lock:
            if not self.permits_work(self.generation):
                raise RuntimeError("service is not running")
            child = await create_owned_child(argv)
            self._children.append(child)
            watcher = asyncio.create_task(self._observe_child(child))
            self._child_watchers.add(watcher)
            watcher.add_done_callback(self._child_observer_done)
            return child

    async def _observe_child(self, child: OwnedChild) -> None:
        while child in self._children:
            if child.drained():
                child.finish()
                self._children.remove(child)
                return
            await asyncio.sleep(0.02)

    def _child_observer_done(self, task: asyncio.Task[None]) -> None:
        self._child_watchers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.resource_failed = True

    async def close(self) -> None:
        self.running = False
        async with self._child_lock:
            await self._close_owned()

    async def _close_owned(self) -> None:
        for task in self._tasks:
            task.cancel()
        # Both rounds use one shared deadline across all owned child sessions.
        # Keep leaders unreaped until descendants (including late forks) drain.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            remaining = [child for child in self._children if not child.drained()]
            for child in remaining:
                child.signal(sig)
            deadline = time.monotonic() + self.shutdown_seconds
            while remaining and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                remaining = [child for child in remaining if not child.drained()]
                if sig == signal.SIGKILL:
                    for child in remaining:
                        child.signal(sig)  # catch a fork racing the first enumeration
            if not remaining:
                break
        if remaining:
            # Retain handles/reserved leaders for safe retry; reset keeps its
            # durable credential fence until this barrier actually completes.
            raise RuntimeError("child session shutdown deadline exceeded")
        for child in self._children:
            child.finish()
        if self._tasks:
            done, pending_tasks = await asyncio.wait(self._tasks, timeout=self.shutdown_seconds)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Consume exceptions without logging payloads.
            if pending_tasks:
                raise RuntimeError("task shutdown deadline exceeded")
        self._children.clear()
        for watcher in self._child_watchers:
            watcher.cancel()
        if self._child_watchers:
            await asyncio.gather(*self._child_watchers, return_exceptions=True)
        self._tasks.clear()
