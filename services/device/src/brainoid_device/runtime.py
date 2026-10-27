"""Daemon-owned lifetime, independent of UI requests and local sessions."""

import asyncio
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from brainoid_contracts.identifiers import ResourceId
from brainoid_contracts.time import format_utc


class Runtime:
    def __init__(self, shutdown_seconds: float = 3.0) -> None:
        self.instance_id = ResourceId(str(uuid4()))
        self.started_at = format_utc(datetime.now(UTC))
        self.running = False
        self.shutdown_seconds = shutdown_seconds
        self._tasks: set[asyncio.Task[None]] = set()
        self._children: list[asyncio.subprocess.Process] = []
        self._child_lock = asyncio.Lock()
        self._spawns: set[asyncio.Task[asyncio.subprocess.Process]] = set()
        self.resource_failed = False

    def start_task(self, work: Coroutine[object, object, None]) -> asyncio.Task[None]:
        if not self.running:
            work.close()
            raise RuntimeError("service is not running")
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    async def start_child(self, argv: Sequence[str]) -> asyncio.subprocess.Process:
        spawn = asyncio.create_task(self._spawn_child(argv))
        self._spawns.add(spawn)
        spawn.add_done_callback(self._spawn_done)
        # Caller cancellation must not cancel subprocess creation before ownership
        # is recorded. close() waits on the same lock before collecting children.
        return await asyncio.shield(spawn)

    def _spawn_done(self, task: asyncio.Task[asyncio.subprocess.Process]) -> None:
        self._spawns.discard(task)
        if not task.cancelled():
            task.exception()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.resource_failed = True

    async def _spawn_child(self, argv: Sequence[str]) -> asyncio.subprocess.Process:
        async with self._child_lock:
            if not self.running:
                raise RuntimeError("service is not running")
            child = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._children.append(child)
            return child

    async def close(self) -> None:
        self.running = False
        async with self._child_lock:
            await self._close_owned()

    async def _close_owned(self) -> None:
        for task in self._tasks:
            task.cancel()
        for child in self._children:
            if child.returncode is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        waits = [asyncio.create_task(child.wait()) for child in self._children]
        if waits:
            _, pending = await asyncio.wait(waits, timeout=self.shutdown_seconds)
            for child in self._children:
                if child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
            if pending:
                _, pending = await asyncio.wait(pending, timeout=self.shutdown_seconds)
                if pending:
                    raise RuntimeError("child shutdown deadline exceeded")
        if self._tasks:
            done, pending_tasks = await asyncio.wait(self._tasks, timeout=self.shutdown_seconds)
            for task in done:
                if not task.cancelled():
                    task.exception()  # Consume exceptions without logging payloads.
            if pending_tasks:
                raise RuntimeError("task shutdown deadline exceeded")
        self._children.clear()
        self._tasks.clear()
