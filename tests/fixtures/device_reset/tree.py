"""Synthetic owned sessions: child, descendant, new process group and late fork."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

mode, directory = sys.argv[1:]
root = Path(directory)


def identity(pid: int) -> dict[str, str | int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "birth": fields[19], "session": int(fields[3])}


if mode.startswith("parent-"):
    kind = mode.removeprefix("parent-")
    child = subprocess.Popen([sys.executable, "-B", __file__, kind, directory])
    if kind != "exited":
        child.wait()
else:
    if mode == "newgroup":
        os.setpgrp()
    if mode in {"resistant", "late"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "fork":

        def fork_on_term(sig: int, frame: object) -> None:
            subprocess.Popen([sys.executable, "-B", __file__, "late", directory])
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, fork_on_term)
    (root / ("late.json" if mode == "late" else "leaf.json")).write_text(
        json.dumps(identity(os.getpid()))
    )
    for n in range(2000):
        (root / ("late-heartbeat" if mode == "late" else "heartbeat")).write_text(str(n))
        time.sleep(0.02)
