"""Updater and Runtime ownership across daemon death and nested command sessions."""

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import brainoid_device.update_runtime as update_runtime
from brainoid_device.app import DeviceApplication
from brainoid_device.update_files import UpdateError
from brainoid_device.update_runtime import OwnedDaemon
from test_device_service import DeviceFixture

PACKAGE = Path(__file__).parents[1] / "services/device/src/brainoid_device"
TREE = Path(__file__).parent / "fixtures/device_reset/tree.py"


class GuardianUpdaterTests(DeviceFixture):
    def test_late_guardian_role_transition_during_actual_updater_stop(self) -> None:
        directory = self.root / "late-role"
        tree = directory / "release"
        shutil.copytree(
            PACKAGE, tree / "lib/brainoid_device", ignore=shutil.ignore_patterns("__pycache__")
        )
        (tree / "ui").mkdir()
        (tree / "ui/index.html").write_text("<!doctype html><title>late guardian</title>")
        gate = directory / "launch"
        guardian_source = tree / "lib/brainoid_device/child_guardian.py"
        guardian_source.write_text(
            "from pathlib import Path\nimport time\n"
            f"Path({str(directory / 'importing')!r}).touch()\n"
            f"while not Path({str(gate)!r}).exists(): time.sleep(.005)\n"
            + guardian_source.read_text()
        )
        (tree / "lib/brainoid_device/__main__.py").write_text(f"""
import asyncio, json, signal, sys
from pathlib import Path
from brainoid_device.runtime import Runtime
async def work():
    runtime = Runtime()
    runtime.running = True
    trigger = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, trigger.set)
    Path({str(directory / "ready")!r}).touch()
    await trigger.wait()
    child = await runtime.start_child([sys.executable, '-B', {str(TREE)!r}, 'parent-resistant', {str(directory)!r}])
    Path({str(directory / "guardian.json")!r}).write_text(json.dumps(dict(pid=child.pid)))
    await asyncio.Event().wait()
def main():
    return asyncio.run(work())
""")
        app = DeviceApplication(self.settings())
        app.close()
        daemon = OwnedDaemon(self.state, self.private, "127.0.0.1", 8765)
        daemon.stop_seconds = 0.1
        daemon.kill_seconds = 2
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        daemon.start(tree)
        assert daemon.process is not None
        leader = daemon.process.pid
        guardian_pid = 0
        guardian_identity: tuple[int, str] | None = None
        command_identity: tuple[int, str] | None = None
        leaf: dict[str, int] = {}
        transitions: list[tuple[bool, bool]] = []
        released_at = 0.0

        def wait_file(path: Path) -> None:
            deadline = time.monotonic() + 3
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(path.exists(), str(path))

        original_session = OwnedDaemon.signal_session
        original_role = update_runtime.is_guardian

        def initial_term(pid: int, sig: signal.Signals) -> None:
            nonlocal guardian_pid, guardian_identity
            if sig == signal.SIGTERM and not guardian_pid:
                self.assertEqual(OwnedDaemon.session_members(pid), {})
                original_session(pid, sig)
                # Real daemon creates its guardian after the initial TERM pass.
                # Hold only the synthetic guardian's import before its role set.
                wait_file(directory / "guardian.json")
                wait_file(directory / "importing")
                guardian_pid = json.loads((directory / "guardian.json").read_text())["pid"]
                guardian_identity = OwnedDaemon.identity(guardian_pid)
                self.assertIsNotNone(guardian_identity)
            else:
                original_session(pid, sig)

        def pause_false_role(pid: int) -> bool:
            nonlocal command_identity, leaf, released_at
            classified = original_role(pid)
            if pid == guardian_pid and not transitions:
                self.assertFalse(classified)
                gate.touch()
                wait_file(directory / "heartbeat")
                leaf = json.loads((directory / "leaf.json").read_text())
                command_identity = OwnedDaemon.identity(leaf["session"])
                self.assertIsNotNone(command_identity)
                transitions.append((classified, original_role(pid)))
                self.assertEqual(transitions, [(False, True)])
                released_at = time.monotonic()
            return classified  # Preserve the real stale read, not a fabricated role.

        try:
            wait_file(directory / "ready")
            with (
                patch.object(OwnedDaemon, "signal_session", side_effect=initial_term),
                patch.object(update_runtime, "is_guardian", pause_false_role),
                self.assertRaises(UpdateError),
            ):
                daemon.stop()  # Actual force-stop, with only scheduling barriers injected.
            self.assertEqual(transitions, [(False, True)])
            # Stop/classify is nonblocking; all members share the force budget.
            self.assertLess(time.monotonic() - released_at, daemon.kill_seconds + 0.5)
            self.assertTrue(daemon.stopped())
            for pid in (leader, guardian_pid, leaf["session"], leaf["pid"]):
                self.assertIsNone(OwnedDaemon.identity(pid))
            beat = (directory / "heartbeat").read_text()
            time.sleep(0.05)
            self.assertEqual((directory / "heartbeat").read_text(), beat)
            self.assertIsNone(unrelated.poll())
            self.assertIsNone(os.waitid(os.P_PID, unrelated.pid, os.WEXITED | os.WNOHANG))
        finally:
            gate.touch()
            daemon.stop_seconds = 0.2
            daemon.kill_seconds = 4
            try:
                try:
                    daemon.stop()
                except UpdateError:
                    if not daemon.stopped() and command_identity is not None:
                        # Regression failure cleanup owns these exact synthetic
                        # identities; never borrow a PID or leave its session live.
                        deadline = time.monotonic() + 4
                        while OwnedDaemon.identity(leaf["session"]) == command_identity:
                            self.assertLess(time.monotonic(), deadline)
                            OwnedDaemon.signal_session(leaf["session"], signal.SIGKILL)
                            OwnedDaemon.reap_members(leaf["session"])
                            if not OwnedDaemon.session_members(leaf["session"]):
                                status = os.waitid(
                                    os.P_PID, leaf["session"], os.WEXITED | os.WNOHANG
                                )
                                if status is not None:
                                    break
                            time.sleep(0.01)
                        if (
                            guardian_identity is not None
                            and OwnedDaemon.identity(guardian_pid) == guardian_identity
                        ):
                            fd = os.pidfd_open(guardian_pid)
                            try:
                                if OwnedDaemon.identity(guardian_pid) == guardian_identity:
                                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                            finally:
                                os.close(fd)
                            deadline = time.monotonic() + 3
                            while (
                                os.waitid(os.P_PID, guardian_pid, os.WEXITED | os.WNOHANG) is None
                            ):
                                self.assertLess(time.monotonic(), deadline)
                                time.sleep(0.01)
                        try:
                            daemon.stop()
                        except UpdateError:
                            self.assertTrue(daemon.stopped())
                    else:
                        self.assertTrue(daemon.stopped())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_updater_drains_runtime_guardians_on_daemon_death_and_force_stop(self) -> None:
        for mode in ("exits", "killed", "unresponsive", "boundary", "retry"):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                tree = directory / "release"
                shutil.copytree(
                    PACKAGE,
                    tree / "lib/brainoid_device",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                (tree / "ui").mkdir()
                (tree / "ui/index.html").write_text(
                    "<!doctype html><title>synthetic guardian</title>"
                )
                # The guardian must load this exact release, not the ambient
                # workspace package available through this interpreter's site path.
                guardian = tree / "lib/brainoid_device/child_guardian.py"
                guardian.write_text(
                    f"from pathlib import Path\nPath({str(directory / 'imported')!r}).write_text(__file__)\n"
                    + guardian.read_text()
                )
                source = f"""
import asyncio, json, signal, sys
from pathlib import Path
from collections.abc import Callable
from brainoid_device.runtime import Runtime
async def work():
    runtime = Runtime()
    runtime.running = True
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = await runtime.start_child([sys.executable, '-B', {str(TREE)!r}, 'parent-resistant', {str(directory)!r}])
    Path({str(directory / "guardian.json")!r}).write_text(json.dumps(dict(pid=child.pid)))
    if {mode!r} != 'boundary':
        while not Path({str(directory / "leaf.json")!r}).exists():
            await asyncio.sleep(.01)
    Path({str(directory / "ready")!r}).touch()
    if {mode!r} == 'exits':
        return 1
    await asyncio.Event().wait()
def main():
    return asyncio.run(work())
"""
                (tree / "lib/brainoid_device/__main__.py").write_text(source)
                app = DeviceApplication(self.settings())
                app.close()  # initialize compatible durable storage for updater ABI
                daemon = OwnedDaemon(self.state, self.private, "127.0.0.1", 8765)
                daemon.stop_seconds = 0.15
                daemon.kill_seconds = 1.5
                daemon.start(tree)
                try:
                    for _ in range(300):
                        if (directory / "ready").exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue((directory / "ready").exists())
                    assert daemon.process is not None
                    leader = daemon.process.pid
                    guardian_pid = json.loads((directory / "guardian.json").read_text())["pid"]
                    if mode != "boundary":
                        self.assertEqual(Path((directory / "imported").read_text()), guardian)
                    if mode == "killed":
                        identity = OwnedDaemon.identity(leader)
                        assert identity is not None
                        for _ in range(100):
                            if daemon.exited() is not None:
                                break
                            OwnedDaemon.signal_member(
                                leader, identity[1], identity[0], signal.SIGKILL
                            )
                            time.sleep(0.01)
                        self.assertIsNotNone(daemon.exited())
                    if mode == "retry":
                        daemon.kill_seconds = 0.01
                        original = OwnedDaemon.signal_member

                        def blocked(
                            pid: int,
                            birth: str,
                            session: int,
                            sig: signal.Signals,
                            *,
                            target: int = guardian_pid,
                            deliver: Callable[[int, str, int, signal.Signals], None] = original,
                        ) -> None:
                            if pid != target:
                                deliver(pid, birth, session, sig)

                        with (
                            patch.object(OwnedDaemon, "signal_member", blocked),
                            self.assertRaises(UpdateError),
                        ):
                            daemon.stop()
                        self.assertFalse(daemon.stopped())
                        self.assertIsNotNone(OwnedDaemon.identity(leader))
                        daemon.kill_seconds = 1.5
                    try:
                        daemon.stop()
                    except UpdateError:
                        pass  # abnormal/forced daemon exit is still reported as such
                    self.assertTrue(daemon.stopped())
                    self.assertIsNone(OwnedDaemon.identity(leader))
                    self.assertIsNone(OwnedDaemon.identity(guardian_pid))
                    if (directory / "leaf.json").exists():
                        leaf = json.loads((directory / "leaf.json").read_text())
                        self.assertIsNone(OwnedDaemon.identity(leaf["pid"]))
                        self.assertIsNone(OwnedDaemon.identity(leaf["session"]))
                finally:
                    daemon.stop_seconds = 0.2
                    daemon.kill_seconds = 4
                    try:
                        daemon.stop()
                    except UpdateError:
                        self.assertTrue(daemon.stopped(), "owned resources must close")


class GuardianFailureTests(DeviceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_guardian_failure_is_not_command_success_or_reset_completion(self) -> None:
        app = DeviceApplication(self.settings())
        app.runtime.running = True
        directory = self.root / "failure"
        directory.mkdir()
        child = await app.runtime.start_child(
            [sys.executable, "-B", str(TREE), "parent-ordinary", str(directory)]
        )
        app.credentials.write("cloud", b"synthetic-private")
        try:
            for _ in range(200):
                if (directory / "leaf.json").exists():
                    break
                await asyncio.sleep(0.01)
            leaf = json.loads((directory / "leaf.json").read_text())
            command_identity = OwnedDaemon.identity(leaf["session"])
            assert command_identity is not None
            fd = os.pidfd_open(child.pid)
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(child.wait(), 3)
            self.assertFalse(await app.reset.execute())
            self.assertEqual(app.reset.status, "failed")
            self.assertTrue(app.credentials.reset_pending)
            self.assertFalse(app.storage.snapshot().permits_operations)
        finally:
            # Deliberately crashed guardian cannot attest cleanup. This test alone
            # owns the synthetic command session, now adopted by its subreaper.
            if (
                "command_identity" in locals()
                and OwnedDaemon.identity(leaf["session"]) == command_identity
            ):
                for _ in range(200):
                    OwnedDaemon.signal_session(leaf["session"], signal.SIGKILL)
                    OwnedDaemon.reap_members(leaf["session"])
                    if not OwnedDaemon.session_members(leaf["session"]) and os.waitid(
                        os.P_PID, leaf["session"], os.WEXITED | os.WNOHANG | os.WNOWAIT
                    ):
                        break
                    await asyncio.sleep(0.01)
                os.waitpid(leaf["session"], 0)
            child._process.wait(timeout=2)
            os.close(child._read)
            app.runtime._children.remove(child)
            await app.runtime.close()
            app.close()
