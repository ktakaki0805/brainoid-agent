"""Explicit client origins; CORS never substitutes for bearer/membership authorization."""

from collections.abc import Callable

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from brainoid_api.settings import Settings


class ClientOriginsMiddleware:
    def __init__(self, app: ASGIApp, settings: Callable[[], Settings | None]) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        settings = self.settings()
        middleware = CORSMiddleware(
            self.app,
            allow_origins=list(settings.client_origins) if settings is not None else [],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "Last-Event-ID",
                "X-Private-Session",
            ],
            expose_headers=["ETag", "X-Request-ID"],
            max_age=0,
        )
        await middleware(scope, receive, send)
