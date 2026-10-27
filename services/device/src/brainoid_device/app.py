"""Local service and durable storage; no cloud/audio/device-control adapters yet."""

import asyncio
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from brainoid_device.assets import load_assets
from brainoid_device.config import Settings
from brainoid_device.diagnostics import local_diagnostics
from brainoid_device.ledger import DeviceStorage
from brainoid_device.local_api import LocalBoundary, Sessions, error
from brainoid_device.reset_command import ResetCommand
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore

STARTUP_SECONDS = 5.0


class DeviceApplication:
    def __init__(
        self, settings: Settings, *, startup: Callable[[Runtime], Awaitable[None]] | None = None
    ) -> None:
        assets = load_assets(settings.static_dir)
        activation = os.environ.get("DEVICE_ACTIVATION_NONCE", "")
        if activation and re.fullmatch("[0-9a-f]{64}", activation) is None:
            raise ValueError("invalid activation identity")
        self.credentials = CredentialStore(settings.credentials_dir)
        self.runtime = Runtime()
        self.shutdown_failed = False
        self.startup_stopped = False
        self._stop_requested = asyncio.Event()
        sessions = Sessions()
        self.runtime.register_private_cleanup(sessions.clear)
        resumed_reset = self.credentials.reset_pending
        self.storage = DeviceStorage(
            settings.state_dir, self.credentials, cleanup_private=self.runtime.clear_private
        )
        self.reset = ResetCommand(self.runtime, self.storage, self.credentials)
        if resumed_reset:
            self.runtime.fence_operations()
            self.reset.status = (
                "complete" if self.storage.snapshot().status == "unregistered" else "failed"
            )

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            self.runtime.running = True
            try:
                if startup is not None and not resumed_reset:
                    await self._run_startup(startup)
                self._check_startup_stop()
                yield
            finally:
                sessions.clear()
                try:
                    await self.reset.finish()
                    await self.runtime.close()
                except Exception:
                    self.shutdown_failed = True
                    raise RuntimeError("device_resource_shutdown_failed") from None
                finally:
                    self.storage.close()
                    self.credentials.close()

        async def health(request: Request) -> Response:
            storage = self.storage.snapshot()
            healthy = (
                (self.runtime.running or self.reset.status == "complete")
                and not self.runtime.resource_failed
                and storage.status in {"ready", "unregistered"}
            )
            return JSONResponse(
                {"status": "ok" if healthy else "unavailable"},
                status_code=200 if healthy else 503,
                headers={"X-Brainoid-Activation": activation} if activation else None,
            )

        async def state(request: Request) -> Response:
            if sessions.find(request) is None:
                return error("session_required", 401)
            return JSONResponse(
                {
                    "status": "stopping" if self._stop_requested.is_set() else "running",
                    "instance_id": self.runtime.instance_id,
                    "started_at": self.runtime.started_at,
                    "software_version": "0.1.0",
                    "storage_status": self.storage.snapshot().status,
                }
            )

        async def diagnostics(request: Request) -> Response:
            if sessions.find(request) is None:
                return error("session_required", 401)
            observed = await asyncio.to_thread(local_diagnostics, settings.state_dir)
            storage = self.storage.snapshot()
            observed.update(
                service="stopping" if self._stop_requested.is_set() else "running",
                storage=storage.status,
                reset=self.reset.status,
                volume_pct=storage.preferences.volume_pct if storage.preferences else None,
                microphone_muted=storage.preferences.microphone_muted
                if storage.preferences
                else None,
            )
            return JSONResponse(observed)

        async def reset(request: Request) -> Response:
            current = sessions.authorize_mutation(request)
            if isinstance(current, Response):
                return current
            if request.url.path == "/local/reset/prepare":
                confirmation = self.reset.prepare(current[0])
                return JSONResponse({"reset_token": confirmation.token, "expires_in": 60})
            if request.headers.get("x-brainoid-confirm") != "local-administrator":
                return error("local_confirmation_required", 403)
            if not self.reset.confirm(
                current[0], request.headers.get("x-brainoid-reset-token", "")
            ):
                return error("reset_confirmation_expired", 403)
            complete = await self.reset.execute()
            return JSONResponse(
                {
                    "local_reset": "complete" if complete else "incomplete",
                    "remote_revocation": "unconfirmed",
                },
                status_code=200 if complete else 503,
            )

        async def static(request: Request) -> Response:
            asset = assets.get(request.url.path)
            if asset is None:
                return error("not_found", 404)
            return Response(asset.content, media_type=asset.media_type)

        async def session(request: Request) -> Response:
            if request.method == "POST":
                return await sessions.bootstrap(request)
            return await sessions.logout(request)

        async def internal_error(request: Request, exc: Exception) -> Response:
            return error("internal_error", 500)

        async def http_error(request: Request, exc: Exception) -> Response:
            assert isinstance(exc, HTTPException)
            code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
            response = error(code, exc.status_code)
            # Keep protocol headers such as Allow, never an exception's detail text.
            if exc.headers:
                response.headers.update(exc.headers)
            return response

        self.app: ASGIApp = LocalBoundary(
            Starlette(
                routes=[
                    Route("/healthz", health, methods=["GET"]),
                    Route("/local/session", session, methods=["POST", "DELETE"]),
                    Route("/local/state", state, methods=["GET"]),
                    Route("/local/diagnostics", diagnostics, methods=["GET"]),
                    Route("/local/reset/prepare", reset, methods=["POST"]),
                    Route("/local/reset", reset, methods=["POST"]),
                    Route("/{path:path}", static, methods=["GET"]),
                ],
                lifespan=lifespan,
                exception_handlers={500: internal_error, HTTPException: http_error},
            ),
            settings,
        )

    def request_stop(self) -> None:
        self._stop_requested.set()

    def _check_startup_stop(self) -> None:
        if self._stop_requested.is_set():
            self.startup_stopped = True
            raise RuntimeError("device_startup_stopped")

    async def _run_startup(self, startup: Callable[[Runtime], Awaitable[None]]) -> None:
        self._check_startup_stop()

        async def initialize() -> None:
            await startup(self.runtime)

        # The hook remains owned even if it suppresses cancellation. Runtime.close
        # cancels and bounds it together with every resource it already registered.
        initialize_task = self.runtime.start_task(initialize())
        stopping = asyncio.create_task(self._stop_requested.wait())
        try:
            done, _ = await asyncio.wait(
                (initialize_task, stopping),
                timeout=STARTUP_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if initialize_task in done:
                initialize_task.result()
            self._check_startup_stop()
            if initialize_task not in done:
                raise RuntimeError("device_startup_deadline_exceeded")
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    def close(self) -> None:
        self.storage.close()
        self.credentials.close()
