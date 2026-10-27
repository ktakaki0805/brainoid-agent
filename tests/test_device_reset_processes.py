"""Reset drains whole owned sessions while leaving unrelated processes alone."""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from brainoid_device.app import DeviceApplication
from brainoid_device.owned_child import OwnedChild
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialsUnavailable
from brainoid_device.update_runtime import OwnedDaemon
from test_device_service import DeviceFixture

TREE = Path(__file__).parent / "fixtures/device_reset/tree.py"


class ResetProcessTests(DeviceFixture, unittest.IsolatedAsyncioTestCase):
    async def wait_file(self, path: Path) -> None:
        for _ in range(200):
            if path.exists():
                return
            await asyncio.sleep(0.01)
        self.fail("synthetic descendant did not start")

    async def test_owned_descendant_sessions_stop_before_success(self) -> None:
        for kind in ("ordinary", "resistant", "newgroup", "exited", "fork"):
            with self.subTest(kind=kind):
                directory = self.root / kind
                directory.mkdir()
                app = DeviceApplication(self.settings())
                app.runtime.running = True
                app.runtime.shutdown_seconds = 0.2
                app.credentials.write("cloud", b"synthetic-private")
                child = await app.runtime.start_child(
                    [sys.executable, "-B", str(TREE), "parent-" + kind, str(directory)]
                )
                try:
                    await self.wait_file(directory / "leaf.json")
                    await self.wait_file(directory / "heartbeat")
                    leaf = json.loads((directory / "leaf.json").read_text())
                    self.assertNotEqual(leaf["session"], os.getsid(0))
                    if kind == "exited":
                        self.assertEqual(await asyncio.wait_for(child.wait(), 3), 0)
                        # wait() observed the leader exit without losing ownership
                        # of its still-active descendant/session.
                        self.assertIsNotNone(OwnedDaemon.identity(child.pid))
                    self.assertTrue(await app.reset.execute())
                    self.assertEqual(app.reset.status, "complete")
                    self.assertEqual(app.storage.snapshot().status, "unregistered")
                    self.assertIsNone(OwnedDaemon.identity(child.pid))
                    self.assertIsNone(OwnedDaemon.identity(leaf["pid"]))
                    beat = (directory / "heartbeat").read_text()
                    if kind == "fork":
                        late = json.loads((directory / "late.json").read_text())
                        self.assertIsNone(OwnedDaemon.identity(late["pid"]))
                    await asyncio.sleep(0.05)
                    self.assertEqual((directory / "heartbeat").read_text(), beat)
                finally:
                    await app.runtime.close()
                    app.close()

    async def test_failed_drain_retains_leader_fence_and_safely_retries(self) -> None:
        directory = self.root / "retry"
        directory.mkdir()
        app = DeviceApplication(self.settings())
        app.runtime.running = True
        app.runtime.shutdown_seconds = 0.04
        child = await app.runtime.start_child(
            [sys.executable, "-B", str(TREE), "parent-resistant", str(directory)]
        )
        app.credentials.write("cloud", b"synthetic-private")
        try:
            await self.wait_file(directory / "leaf.json")
            leaf = json.loads((directory / "leaf.json").read_text())
            birth = OwnedDaemon.identity(child.pid)
            with patch.object(child, "signal", return_value=None):
                self.assertFalse(await app.reset.execute())
            self.assertEqual(app.reset.status, "failed")
            self.assertEqual(OwnedDaemon.identity(child.pid), birth)
            self.assertTrue(app.credentials.reset_pending)
            with self.assertRaises(CredentialsUnavailable):
                app.credentials.read("cloud")
            self.assertFalse(app.storage.snapshot().permits_operations)
            # The short deadline above makes the injected failure bounded; real
            # guardian/descendant scheduling needs the normal production budget.
            app.runtime.shutdown_seconds = 3.0
            self.assertTrue(await app.reset.execute())
            self.assertIsNone(OwnedDaemon.identity(child.pid))
            self.assertIsNone(OwnedDaemon.identity(leaf["pid"]))
        finally:
            await app.runtime.close()
            app.close()

    async def test_completed_children_are_reaped_without_service_shutdown(self) -> None:
        runtime = Runtime()
        runtime.running = True
        # A sibling process is not in any Runtime-owned session and must remain
        # alive and unreaped through child completion and Runtime.close().
        borrowed = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        unowned_exit = subprocess.Popen([sys.executable, "-c", "raise SystemExit(9)"])
        children: list[OwnedChild] = []
        try:
            for _ in range(200):
                status = os.waitid(os.P_PID, unowned_exit.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if status is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(status)
            for _ in range(12):
                child = await runtime.start_child([sys.executable, "-c", "raise SystemExit(7)"])
                children.append(child)
            for child in children[:6]:
                self.assertEqual(await asyncio.wait_for(child.wait(), 3), 7)
            for _ in range(200):
                if not runtime._children:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(runtime._children, [])
            for child in children:
                self.assertEqual(child.returncode, 7)
                self.assertIsNone(OwnedDaemon.identity(child.pid))
            self.assertEqual(
                os.waitid(os.P_PID, borrowed.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT), None
            )
            await runtime.close()
            self.assertIsNone(borrowed.poll())
            status = os.waitid(os.P_PID, unowned_exit.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertIsNotNone(status)  # Runtime did not reap an unrelated zombie.
            self.assertEqual(unowned_exit.wait(timeout=1), 9)
        finally:
            try:
                await runtime.close()
            finally:
                borrowed.terminate()
                borrowed.wait(timeout=3)
                if unowned_exit.poll() is None:
                    unowned_exit.terminate()
                unowned_exit.wait(timeout=3)
