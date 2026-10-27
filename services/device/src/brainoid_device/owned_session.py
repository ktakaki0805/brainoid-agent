"""Async-facing Linux child handle whose session identity survives leader exit.

Only finish() reaps the leader. The asyncio child watcher never owns this Popen,
so even an exited leader reserves its PID/session until all descendants are gone.
Adapters must not daemonize into another session or reap processes themselves.
"""

import asyncio
import ctypes
import os
import signal
import subprocess
from collections.abc import Sequence

from brainoid_device.update_runtime import OwnedDaemon


class SessionChild:
    def __init__(self, argv: Sequence[str]) -> None:
        # Adopt orphaned descendants, but reap only verified members of our own
        # child sessions. Never waitpid(-1) or signal a borrowed group/session.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("child ownership unavailable")
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.pid = self._process.pid
        identity = OwnedDaemon.identity(self.pid)
        if identity is None or identity[0] != self.pid:
            # The unreaped direct child is still ours; never borrow another PID.
            self._process.kill()
            self._process.wait()
            raise RuntimeError("child session unavailable")
        self._birth = identity[1]

    @property
    def returncode(self) -> int | None:
        if self._process.returncode is not None:
            return self._process.returncode
        status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    async def wait(self) -> int:
        while (code := self.returncode) is None:
            await asyncio.sleep(0.02)
        if self.drained():
            self.finish()
        return code

    def _require_owned(self) -> None:
        if self._process.returncode is not None or OwnedDaemon.identity(self.pid) != (
            self.pid,
            self._birth,
        ):
            raise RuntimeError("child session ownership lost")

    def signal(self, sig: signal.Signals) -> None:
        self._require_owned()
        # DEV08 helpers acquire pidfds, then recheck PID/birth/session before
        # signaling. Force is a nonblocking stop/classify/drain step, repeated by
        # the guardian within its shared deadline. New groups stay in the session.
        OwnedDaemon.signal_session(self.pid, sig)

    def terminate(self) -> None:
        self.signal(signal.SIGTERM)

    def kill(self) -> None:
        self.signal(signal.SIGKILL)

    def drained(self) -> bool:
        if self._process.returncode is not None:
            return True  # finish() already verified and released this session.
        self._require_owned()
        OwnedDaemon.reap_members(self.pid)
        return self.returncode is not None and not OwnedDaemon.session_members(self.pid)

    def finish(self) -> None:
        if not self.drained():
            raise RuntimeError("child session is still active")
        if self._process.returncode is None:
            self._process.wait(timeout=0)
