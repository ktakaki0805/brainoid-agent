"""Trusted cleanup guardian kept visible in its daemon/updater's session."""

import ctypes
import os
import signal
import sys
import time
from types import FrameType

from brainoid_device.owned_session import SessionChild
from brainoid_device.update_runtime import GUARDIAN_NAME


def main() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    # Set the role before any command exists. /proc comm remains on a zombie,
    # letting the updater refuse to reap an unexpectedly failed guardian.
    if libc.prctl(15, GUARDIAN_NAME.encode(), 0, 0, 0) != 0:
        return 1
    parent, output, *argv = sys.argv[1:]
    fd = int(output)
    phase = ""
    deadline = 0.0

    def requested(sig: int, frame: FrameType | None) -> None:
        nonlocal phase, deadline
        if sig == signal.SIGUSR1:
            phase = "kill"
        elif not phase:
            phase = "term"
            deadline = time.monotonic() + 3

    signal.signal(signal.SIGTERM, requested)
    signal.signal(signal.SIGINT, requested)
    signal.signal(signal.SIGUSR1, requested)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        return 1
    if os.getppid() != int(parent):
        requested(signal.SIGTERM, None)
    signal.pthread_sigmask(signal.SIG_SETMASK, [])

    def current_phase() -> str:
        return phase

    def report(value: str) -> None:
        try:
            os.write(fd, value.encode() + b"\n")
        except BrokenPipeError:
            # Parent death closes the pipe; continue owning and draining work.
            requested(signal.SIGTERM, None)

    if current_phase():
        report("exit:-15")
        report("closed")
        return 0
    child = SessionChild(argv)
    report(f"session:{child.pid}")
    observed = False
    term_sent = False
    while True:
        try:
            if not observed and (code := child.returncode) is not None:
                report(f"exit:{code}")
                observed = True
            if child.drained():
                child.finish()
                if not observed:
                    report(f"exit:{child.returncode}")
                report("closed")
                return 0
            if current_phase() == "term" and not term_sent:
                child.terminate()
                term_sent = True
            if current_phase() == "term" and time.monotonic() >= deadline:
                phase = "kill"
            if current_phase() == "kill":
                child.kill()  # rescan for forks racing the previous signal
        except Exception:
            # Never abandon a possibly live command session or emit 'closed'.
            # Runtime/updater deadlines report failure while preserving ownership.
            phase = "kill"
        time.sleep(0.02)


if __name__ == "__main__":
    raise SystemExit(main())
