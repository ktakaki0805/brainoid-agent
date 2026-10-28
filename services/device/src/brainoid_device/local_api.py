"""Strict single-origin HTTP boundary and bounded, ephemeral browser sessions."""

import asyncio
import re
import secrets
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from brainoid_device.config import Settings

COOKIE = "brainoid_local"
SESSION_SECONDS = 1800
MAX_SESSIONS = 32
BODY_ROUTES = {"/local/cloud/pairing", "/local/cloud/operations"}
MAX_BODY = 32768
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}


def error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=SAFE_HEADERS)


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float


class Sessions:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def find(self, request: Request) -> tuple[str, Session] | None:
        now = time.monotonic()
        self._sessions = {
            key: value for key, value in self._sessions.items() if value.expires > now
        }
        values = [
            part.strip().partition("=")[2]
            for part in request.headers.get("cookie", "").split(";")
            if part.strip().partition("=")[0] == COOKIE
        ]
        if len(values) != 1 or TOKEN.fullmatch(values[0]) is None:
            return None
        session = self._sessions.get(values[0])
        return (values[0], session) if session else None

    async def bootstrap(self, request: Request) -> Response:
        if request.headers.get("x-brainoid-local") != "1":
            return error("bootstrap_required", 403)
        current = self.find(request)
        if current:
            token, session = current
        else:
            if len(self._sessions) >= MAX_SESSIONS:
                return error("session_limit", 429)
            token = secrets.token_urlsafe(32)
            session = Session(secrets.token_urlsafe(32), time.monotonic() + SESSION_SECONDS)
            self._sessions[token] = session
        remaining = max(0, int(session.expires - time.monotonic()))
        response = JSONResponse({"csrf_token": session.csrf, "expires_in": remaining})
        response.set_cookie(COOKIE, token, max_age=remaining, httponly=True, samesite="strict")
        return response

    def authorize_mutation(self, request: Request) -> tuple[str, Session] | Response:
        current = self.find(request)
        if current is None:
            return error("session_required", 401)
        supplied = request.headers.get("x-csrf-token", "")
        if TOKEN.fullmatch(supplied) is None or not secrets.compare_digest(
            supplied, current[1].csrf
        ):
            return error("csrf_required", 403)
        return current

    async def logout(self, request: Request) -> Response:
        current = self.authorize_mutation(request)
        if isinstance(current, Response):
            return current
        del self._sessions[current[0]]
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, httponly=True, samesite="strict")
        return response

    def clear(self) -> None:
        self._sessions.clear()


class LocalBoundary:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response = self.check(scope)
        if response is not None:
            await response(scope, receive, send)
            return

        if scope["path"] in BODY_ROUTES and scope["method"] == "POST":
            # Narrow typed JSON admission only. Buffer before dispatch so a lying
            # Content-Length or slow upload cannot reach a side-effecting handler.
            body = bytearray()
            try:
                async with asyncio.timeout(3):
                    while True:
                        message = await receive()
                        if message["type"] != "http.request":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > MAX_BODY:
                            await error("body_too_large", 413)(scope, receive, send)
                            return
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await error("body_timeout", 408)(scope, receive, send)
                return
            expected = dict(scope["headers"]).get(b"content-length", b"0")
            if len(body) != int(expected):
                await error("invalid_body", 400)(scope, receive, send)
                return
            original_receive = receive
            delivered = False

            async def buffered_receive() -> Message:
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = buffered_receive

        async def send_safe(message: Message) -> None:
            if message["type"] == "http.response.start":
                security_names = {name.encode() for name in SAFE_HEADERS}
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in security_names
                ]
                headers.extend(
                    (name.encode(), value.encode()) for name, value in SAFE_HEADERS.items()
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_safe)

    def check(self, scope: Scope) -> Response | None:
        peer = scope.get("client")
        if not peer or peer[0] not in {"127.0.0.1", "::1"}:
            return error("loopback_required", 403)
        headers: dict[bytes, list[bytes]] = {}
        raw_headers = scope["headers"]
        if sum(len(k) + len(v) for k, v in raw_headers) > 8192:
            return error("headers_too_large", 431)
        for key, value in raw_headers:
            headers.setdefault(key.lower(), []).append(value)
        for name in (
            b"host",
            b"origin",
            b"cookie",
            b"x-csrf-token",
            b"x-brainoid-local",
            b"x-brainoid-reset-token",
            b"x-brainoid-confirm",
            b"sec-fetch-site",
            b"content-length",
            b"transfer-encoding",
            b"content-type",
            b"x-private-session",
            b"authorization",
        ):
            if len(headers.get(name, [])) > 1:
                return error("duplicate_header", 400)
        if headers.get(b"host") != [self.settings.authority.encode()]:
            return error("host_denied", 403)
        origin = headers.get(b"origin")
        if origin is not None and origin != [self.settings.origin.encode()]:
            return error("origin_denied", 403)
        if headers.get(b"sec-fetch-site", [b"none"])[0] not in {b"same-origin", b"none"}:
            return error("origin_denied", 403)
        if scope["method"] not in {"GET", "HEAD"} and origin is None:
            return error("origin_required", 403)
        if b"authorization" in headers:
            return error("authorization_not_supported", 400)
        if scope["path"] in BODY_ROUTES and scope["method"] == "POST":
            length = headers.get(b"content-length", [b""])[0]
            if (
                b"transfer-encoding" in headers
                or not re.fullmatch(rb"[0-9]{1,5}", length)
                or not 1 <= int(length) <= MAX_BODY
                or headers.get(b"content-type") != [b"application/json"]
            ):
                return error("invalid_body", 413)
        elif b"transfer-encoding" in headers or headers.get(b"content-length", [b"0"]) != [b"0"]:
            return error("body_not_supported", 413)
        if scope["path"].startswith("/local/cloud/") and scope.get("query_string"):
            return error("invalid_operation", 400)
        raw_path = scope.get("raw_path", b"")
        if len(raw_path) + len(scope.get("query_string", b"")) > 2048:
            return error("path_too_long", 414)
        # Assets have canonical URL names. No percent decoding or filesystem joining here.
        if (
            b"%" in raw_path
            or b"\\" in raw_path
            or any(p in {".", ".."} for p in scope["path"].split("/"))
        ):
            return error("path_denied", 404)
        return None
