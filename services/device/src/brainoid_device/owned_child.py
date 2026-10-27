"""Async command handle; trusted guardian owns its dedicated command session."""

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from brainoid_device.update_runtime import OwnedDaemon

GUARDIAN_LAUNCH = """
import os, signal, sys
sys.path.insert(0, sys.argv.pop(1))
from brainoid_device.child_guardian import main
result = main()
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGUSR1})
os._exit(result)
"""


class OwnedChild:
    def __init__(self, argv: Sequence[str]) -> None:
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("child ownership unavailable")
        self._read, write = os.pipe2(os.O_CLOEXEC)
        self._buffer = b""
        self._code: int | None = None
        self._closed = False
        self._eof = False
        self._reaped = False
        previous = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGUSR1}
        )
        try:
            # Guardian stays in the daemon's session, so DEV08 can observe and
            # drain it even after the daemon is unresponsive or already killed.
            self._process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    GUARDIAN_LAUNCH,
                    str(Path(__file__).resolve().parents[1]),
                    str(os.getpid()),
                    str(write),
                    *argv,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(write,),
            )
        except BaseException:
            os.close(self._read)
            raise
        finally:
            os.close(write)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        os.set_blocking(self._read, False)
        self.pid = self._process.pid
        self._identity = OwnedDaemon.identity(self.pid)
        if self._identity is None:
            raise RuntimeError("guardian ownership unavailable")

    def _guardian_code(self) -> int | None:
        if self._process.returncode is not None:
            return self._process.returncode
        status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    def _read_status(self) -> None:
        if self._reaped:
            return
        while True:
            try:
                data = os.read(self._read, 1024)
            except BlockingIOError:
                break
            if not data:
                self._eof = True
                break
            self._buffer += data
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if line.startswith(b"exit:"):
                    self._code = int(line.removeprefix(b"exit:"))
                elif line == b"closed":
                    self._closed = True
        code = self._guardian_code()
        if code is not None and self._eof and (code != 0 or not self._closed or self._code is None):
            raise RuntimeError("guardian failed without verified cleanup")

    @property
    def returncode(self) -> int | None:
        self._read_status()
        return self._code

    async def wait(self) -> int:
        while (code := self.returncode) is None:
            await asyncio.sleep(0.02)
        if self.drained():
            self.finish()
        return code

    def signal(self, sig: signal.Signals) -> None:
        if self._reaped or OwnedDaemon.identity(self.pid) != self._identity:
            raise RuntimeError("guardian ownership lost")
        assert self._identity is not None
        # SIGKILL is a force-drain request, never destruction of the guardian
        # before it has verified its separate command session is empty.
        OwnedDaemon.signal_member(
            self.pid,
            self._identity[1],
            self._identity[0],
            signal.SIGUSR1 if sig == signal.SIGKILL else sig,
        )

    def terminate(self) -> None:
        self.signal(signal.SIGTERM)

    def kill(self) -> None:
        self.signal(signal.SIGKILL)

    def drained(self) -> bool:
        if self._reaped:
            return True
        self._read_status()
        return self._closed and self._guardian_code() == 0

    def finish(self) -> None:
        if not self.drained():
            raise RuntimeError("command session is still active")
        if not self._reaped:
            self._process.wait(timeout=0)
            os.close(self._read)
            self._reaped = True


async def create_owned_child(argv: Sequence[str]) -> OwnedChild:
    return OwnedChild(argv)
