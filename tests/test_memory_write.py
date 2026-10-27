"""Sequential write handoff must publish completion only after releasing capacity."""

import asyncio
import unittest
from concurrent.futures import Future
from threading import Event
from typing import Any
from unittest.mock import patch

from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_worker.run_event_writer import WriteBridge, memory_write


class MemoryWriteTests(unittest.TestCase):
    def test_success_publish_admits_immediate_sequential_work(self) -> None:
        self.handoff(False)

    def test_error_publish_admits_immediate_sequential_work(self) -> None:
        self.handoff(True)

    def handoff(self, failed: bool) -> None:
        bridge = WriteBridge()
        publish_paused, resume_publisher, publisher_done = Event(), Event(), Event()

        class PausedFuture(Future[Any]):
            def set_result(self, result: Any) -> None:
                publish_paused.set()
                super().set_result(result)
                try:
                    resume_publisher.wait(5)
                finally:
                    publisher_done.set()

            def set_exception(self, error: BaseException | None) -> None:
                publish_paused.set()
                super().set_exception(error)
                try:
                    resume_publisher.wait(5)
                finally:
                    publisher_done.set()

        def work() -> str:
            if failed:
                raise ValueError("synthetic write failure")
            return "committed"

        async def scenario() -> None:
            try:
                with patch("brainoid_worker.run_event_writer.Future", PausedFuture):
                    if failed:
                        with self.assertRaisesRegex(ValueError, "synthetic write failure"):
                            await memory_write(bridge, work, stopped=lambda: False)
                    else:
                        self.assertEqual(
                            await memory_write(bridge, work, stopped=lambda: False),
                            "committed",
                        )
                self.assertTrue(publish_paused.is_set())
                self.assertFalse(publisher_done.is_set())
                # The first Future publisher remains deliberately paused. This
                # succeeds only when release precedes completion publication.
                self.assertEqual(
                    await memory_write(bridge, lambda: "reconciled", stopped=lambda: False),
                    "reconciled",
                )
            finally:
                resume_publisher.set()
                self.assertTrue(await asyncio.to_thread(publisher_done.wait, 2))
            self.assertTrue(bridge.slot.acquire(blocking=False))
            bridge.slot.release()

        asyncio.run(scenario())

    def test_cancelled_await_keeps_active_work_bounded_until_it_finishes(self) -> None:
        bridge = WriteBridge()
        entered, finish_work, finished = Event(), Event(), Event()

        def work() -> None:
            entered.set()
            try:
                finish_work.wait(5)
            finally:
                finished.set()

        async def scenario() -> None:
            first = asyncio.create_task(memory_write(bridge, work, stopped=lambda: False))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                with self.assertRaises(PersistenceUnavailable):
                    await memory_write(bridge, lambda: None, stopped=lambda: False)
            finally:
                finish_work.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            # Wait for actual release after work returns, including exceptional cleanup.
            self.assertTrue(await asyncio.to_thread(bridge.slot.acquire, True, 2))
            bridge.slot.release()
            self.assertEqual(
                await memory_write(bridge, lambda: "next", stopped=lambda: False), "next"
            )

        asyncio.run(scenario())

    def test_thread_start_failure_and_stopped_admission_preserve_slot(self) -> None:
        bridge = WriteBridge()

        async def scenario() -> None:
            with self.assertRaises(PersistenceUnavailable):
                await memory_write(bridge, lambda: None, stopped=lambda: True)
            with patch("brainoid_worker.run_event_writer.Thread.start", side_effect=RuntimeError):
                with self.assertRaises(PersistenceUnavailable):
                    await memory_write(bridge, lambda: None, stopped=lambda: False)
            self.assertEqual(
                await memory_write(bridge, lambda: "next", stopped=lambda: False), "next"
            )
            self.assertTrue(bridge.slot.acquire(blocking=False))
            bridge.slot.release()

        asyncio.run(scenario())
