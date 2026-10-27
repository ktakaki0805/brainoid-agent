"""Synthetic Linux lifecycle fixture. Never used by the installed device entrypoint."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from brainoid_device.app import DeviceApplication
from brainoid_device.config import load_settings
from brainoid_device.runtime import Runtime
from brainoid_device.server import serve


async def startup(runtime: Runtime) -> None:
    if os.environ.get("DEVICE_TEST_STARTUP_EVENT_ONLY") == "1":
        print(json.dumps({"event": "startup_waiting", "monotonic": time.monotonic()}), flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            print(
                json.dumps({"event": "startup_finally", "monotonic": time.monotonic()}), flush=True
            )
        return
    marker = Path(os.environ["DEVICE_TEST_MARKER"])
    child = await runtime.start_child(
        [
            sys.executable,
            "-B",
            "-c",
            "import signal,time,pathlib,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(300)",
            str(marker),
        ]
    )
    print(
        json.dumps(
            {
                "child_pid": child.pid,
                "birth": Path(f"/proc/{child.pid}/stat").read_text().split()[21],
            }
        ),
        flush=True,
    )
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("synthetic child did not start")

    async def worker() -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    if os.environ.get("DEVICE_TEST_STUBBORN_TASK") == "1":
                        continue
                    raise
        finally:
            marker.with_suffix(".closed").write_text("closed")

    runtime.start_task(worker())
    if os.environ.get("DEVICE_TEST_STARTUP_FAIL") == "1":
        raise RuntimeError("fixture-secret-not-for-logs")
    if os.environ.get("DEVICE_TEST_STARTUP_WAIT") == "1":
        marker.with_suffix(".starting").write_text("waiting")
        try:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    marker.with_suffix(".cancelled").write_text("cancelled")
                    if os.environ.get("DEVICE_TEST_STUBBORN_STARTUP") == "1":
                        continue
                    raise
        finally:
            marker.with_suffix(".startup_closed").write_text("closed")


if __name__ == "__main__":
    import logging

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    settings = load_settings()
    application = DeviceApplication(settings, startup=startup)
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = serve(application, settings)
    except (Exception, SystemExit):
        result = False
    finally:
        application.close()
    restored = all(signal.getsignal(sig) == handler for sig, handler in previous_handlers.items())
    if os.environ.get("DEVICE_TEST_STARTUP_EVENT_ONLY") == "1":
        print(json.dumps({"event": "signals_restored", "restored": restored}), flush=True)
    result = result and restored
    raise SystemExit(0 if result else 1)
