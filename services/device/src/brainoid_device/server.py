"""Single event loop with bounded final cancellation and content-free diagnostics."""

import asyncio
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType

import uvicorn

from brainoid_device.app import DeviceApplication
from brainoid_device.config import Settings


class DeviceServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, request_stop: Callable[[], None]) -> None:
        super().__init__(config)
        self._request_stop = request_stop

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # Also interrupt pending lifespan startup. Repeated signals must not use
        # Uvicorn's force-exit path, which skips the resource cleanup lifespan.
        self._request_stop()
        self.should_exit = True

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # Uvicorn normally re-raises SIGTERM after cleanup. The service reports its
        # own successful/failed cleanup exit status to systemd instead.
        loop = asyncio.get_running_loop()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig in previous:
                # The loop's signal wakeup FD interrupts an idle selector even
                # when startup has no periodic task to advance its ready queue.
                loop.add_signal_handler(sig, self.handle_exit, sig, None)
            yield
        finally:
            for sig, handler in previous.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)


def serve(application: DeviceApplication, settings: Settings) -> bool:
    server = DeviceServer(
        uvicorn.Config(
            application.app,
            host=settings.host,
            port=settings.port,
            http="h11",
            ws="none",
            lifespan="on",
            access_log=False,
            proxy_headers=False,
            log_config=None,
            server_header=False,
            timeout_keep_alive=2,
            timeout_graceful_shutdown=3,
            h11_max_incomplete_event_size=8192,
            limit_concurrency=32,
        ),
        application.request_stop,
    )
    # asyncio.run waits indefinitely for tasks that suppress cancellation. Library
    # extensions must cooperate; bound even a broken extension's final drain.
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda loop, context: None)
    asyncio.set_event_loop(loop)
    drained = True
    try:
        loop.run_until_complete(server.serve())
    except SystemExit:
        # Uvicorn reports an interrupted lifespan startup as a startup failure.
        # A requested stop with completed cleanup is a normal service exit even
        # though the listener never opened; actual startup failures still fail.
        if not application.startup_stopped:
            raise
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            _, remaining = loop.run_until_complete(asyncio.wait(pending, timeout=1))
            drained = not remaining
        loop.close()
        asyncio.set_event_loop(None)
    return (
        (server.started or application.startup_stopped)
        and not application.shutdown_failed
        and drained
    )
