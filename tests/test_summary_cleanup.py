"""MEM06-R1-001: finite provider close, cancellation and private-loop shutdown."""

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Coroutine
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelRequest,
    TextDelta,
    ToolCall,
    ToolRequested,
)
from brainoid_jobs import Delivery
from brainoid_worker.local import LOCAL_AUTHORIZATION
from fakes.memory_repository import TENANT
from test_summary_provenance import SummaryFixture


class CleanupModel:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0
        self.open = False
        self.closing = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.release: asyncio.Event | None = None

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.calls += 1
        self.open = True
        self.loop = asyncio.get_running_loop()
        self.release = asyncio.Event()
        try:
            if self.mode == "invalid":
                yield ToolRequested(
                    call=ToolCall(call_key="x", name="notes.delete", arguments_json="{}")
                )
            elif self.mode in ("timeout", "cancel"):
                if self.mode == "cancel":
                    parent = next(
                        task
                        for task in asyncio.all_tasks()
                        if task.get_name() == "summary-generation"
                    )
                    self.loop.call_soon(parent.cancel)
                await asyncio.Event().wait()
            else:
                yield TextDelta(text="Summary must not survive failed cleanup")
                yield Completed()
        finally:
            self.closing.set()
            try:
                if self.mode == "exception":
                    raise RuntimeError("synthetic cleanup failure")
                await self.release.wait()
            finally:
                self.open = False


class SummaryCleanupTests(SummaryFixture):
    def exercise(self, mode: str) -> None:
        model = CleanupModel(mode)
        self.handler.model = model
        errors: list[BaseException] = []

        def deliver() -> None:
            try:
                self.deliver()
            except BaseException as error:
                errors.append(error)

        real_timeout = asyncio.timeout
        worker = threading.Thread(target=deliver, daemon=True)
        # Shorten only the declared provider deadline, not cancellation grace. This
        # also reproduces the review probe's timeout-entering-blocked-finally case.
        with patch(
            "brainoid_worker.summarize_conversation.asyncio.timeout",
            side_effect=lambda seconds: real_timeout(0.03),
        ):
            started = time.monotonic()
            worker.start()
            try:
                self.assertTrue(model.closing.wait(1), "provider cleanup was not reached")
                worker.join(1)
                self.assertFalse(worker.is_alive(), "worker exceeded bounded cleanup")
                self.assertLess(time.monotonic() - started, 1.2)
            finally:
                # A failing regression must not leave its worker or provider behind.
                if worker.is_alive() and model.loop is not None and model.release is not None:
                    model.loop.call_soon_threadsafe(model.release.set)
                    worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(
            [type(error) for error in errors], [asyncio.CancelledError] if mode == "cancel" else []
        )
        self.assertFalse(model.open)
        assert model.loop is not None
        self.assertTrue(model.loop.is_closed())
        self.assertEqual(asyncio.all_tasks(model.loop), set())
        self.assertNotIn(self.path, self.store.data)
        self.assertEqual([item.state for item in self.state().checkpoints], ["started"])
        if mode == "cancel":
            # Python cancellation propagates; durable lease recovery still refuses
            # to replay the charged started effect when the caller disappeared.
            self.now += timedelta(minutes=2)
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))
        self.assertEqual(self.state().reason, "outcome_unknown")
        self.worker.handle(
            LOCAL_AUTHORIZATION,
            Delivery(
                tenant_id=TENANT,
                job_id=self.identity,
                dispatch_generation=self.state().dispatch_generation,
            ).model_dump(mode="json"),
        )
        self.assertEqual(model.calls, 1)

    def test_completed_response_with_blocked_close_is_not_saved(self) -> None:
        self.exercise("completed")

    def test_invalid_event_with_blocked_close_is_not_saved(self) -> None:
        self.exercise("invalid")

    def test_anext_timeout_cancels_blocked_finally_again_and_collects(self) -> None:
        self.exercise("timeout")

    def test_cleanup_exception_discards_complete_response(self) -> None:
        self.exercise("exception")

    def test_python_task_cancellation_is_propagated_after_resource_collection(self) -> None:
        self.exercise("cancel")

    def test_scheduled_advance_rechecks_guard_inside_provider_task(self) -> None:
        original = asyncio.create_task
        hits: list[str] = []

        def scheduled(work: Coroutine[Any, Any, Any], **kwargs: Any) -> asyncio.Task[Any]:
            task = original(work, **kwargs)
            if kwargs.get("name") == "summary-provider-wait" and not hits:
                # This is after create_task and before its coroutine first runs.
                self.assertFalse(task.done())
                hits.append("advance")
                self.now += timedelta(hours=1)
            return task

        with patch.object(asyncio, "create_task", scheduled):
            self.deliver()
        self.assertEqual(hits, ["advance"])
        self.assertEqual(self.model.requests, [])
        self.assertNotIn(self.path, self.store.data)
        self.assertEqual(self.state().status, "obsolete")
