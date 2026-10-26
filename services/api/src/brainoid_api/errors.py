"""Public errors and request logging with an explicit, content-free field allowlist."""

import json
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from brainoid_contracts.errors import APIError as APIError
from brainoid_contracts.errors import ErrorCode as ErrorCode
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("brainoid_api")


@dataclass(frozen=True)
class ErrorDefinition:
    status: int
    message: str
    retryable: bool = False


ERRORS = {
    ErrorCode.INVALID_REQUEST: ErrorDefinition(400, "Invalid request."),
    ErrorCode.VALIDATION_FAILED: ErrorDefinition(422, "Request validation failed."),
    ErrorCode.AUTHENTICATION_REQUIRED: ErrorDefinition(401, "Authentication required."),
    ErrorCode.OPERATION_FORBIDDEN: ErrorDefinition(403, "Operation forbidden."),
    ErrorCode.REAUTH_REQUIRED: ErrorDefinition(403, "Recent authentication required."),
    ErrorCode.RESOURCE_NOT_FOUND: ErrorDefinition(404, "Resource not found."),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorDefinition(405, "Method not allowed."),
    ErrorCode.OWNERSHIP_TRANSFER_REQUIRED: ErrorDefinition(
        409, "Ownership transfer or tenant deletion required."
    ),
    ErrorCode.CONFLICT: ErrorDefinition(409, "Request conflicts with the current state."),
    ErrorCode.PAIRING_PENDING: ErrorDefinition(409, "Pairing awaits confirmation.", True),
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorDefinition(
        409, "Idempotency key was used with another body."
    ),
    ErrorCode.RESOURCE_EXPIRED: ErrorDefinition(410, "Resource expired."),
    ErrorCode.VERSION_CONFLICT: ErrorDefinition(412, "Resource version changed."),
    ErrorCode.VERSION_REQUIRED: ErrorDefinition(428, "Resource version required."),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorDefinition(413, "Request too large."),
    ErrorCode.RATE_LIMITED: ErrorDefinition(429, "Request rate exceeded.", True),
    ErrorCode.TEMPORARILY_UNAVAILABLE: ErrorDefinition(
        503, "Service temporarily unavailable.", True
    ),
    ErrorCode.INTERNAL_ERROR: ErrorDefinition(500, "Internal server error."),
}


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ErrorCode
    message: str
    request_id: UUID
    retryable: bool


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: ErrorDetail


def error_response(scope: Scope, code: ErrorCode, status: int | None = None) -> JSONResponse:
    definition = ERRORS[code]
    scope["state"]["error_code"] = code.value
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=definition.message,
            request_id=UUID(scope["state"]["request_id"]),
            retryable=definition.retryable,
        )
    )
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status or definition.status,
        headers={"Retry-After": "60"} if code == ErrorCode.RATE_LIMITED else None,
    )


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(request: Request, exception: Exception) -> JSONResponse:
        assert isinstance(exception, APIError)
        return error_response(request.scope, exception.code)

    async def validation_error(request: Request, exception: Exception) -> JSONResponse:
        return error_response(request.scope, ErrorCode.VALIDATION_FAILED)

    async def http_error(request: Request, exception: Exception) -> JSONResponse:
        assert isinstance(exception, HTTPException)
        code = next(
            (
                code
                for code, definition in ERRORS.items()
                if definition.status == exception.status_code
            ),
            ErrorCode.INTERNAL_ERROR if exception.status_code >= 500 else ErrorCode.INVALID_REQUEST,
        )
        if exception.status_code in (502, 504):
            code = ErrorCode.TEMPORARILY_UNAVAILABLE
        response = error_response(request.scope, code, exception.status_code)
        if exception.status_code == 405 and exception.headers:
            allowed = exception.headers.get("Allow", "")
            if set(allowed.split(", ")) <= {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
            }:
                response.headers["Allow"] = allowed
        return response

    app.add_exception_handler(APIError, api_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["error_code"] = None
        status = 500
        started = False

        async def send_response(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in (b"x-request-id", b"cache-control")
                ]
                message["headers"] = headers + [
                    (b"x-request-id", request_id.encode("ascii")),
                    (b"cache-control", b"no-store"),
                ]
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception:
            state["error_code"] = ErrorCode.INTERNAL_ERROR.value
            if started:
                # Abort a partial response without exposing the original exception to ASGI logs.
                raise RuntimeError("API response interrupted after headers were sent") from None
            await error_response(scope, ErrorCode.INTERNAL_ERROR)(scope, receive, send_response)
        finally:
            # Never log the scope, URL, headers, body, exception text, or traceback.
            logger.log(
                logging.ERROR if state["error_code"] == "internal_error" else logging.INFO,
                json.dumps(
                    {
                        "event": "request_completed",
                        "request_id": request_id,
                        "status": status,
                        "error_code": state["error_code"],
                    }
                ),
            )
