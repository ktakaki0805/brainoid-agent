"""Authenticated authorization admission and anonymous provider redirect callback."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.calendar_oauth import IntegrationPage, OAuthStart, OAuthStartResponse
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.tenants import Identity, _header
from brainoid_api.services.calendar_authorization import CalendarAuthorization

router = APIRouter(tags=["integrations"])


def service(services: Annotated[AuthServices, Depends(get_auth_services)]) -> CalendarAuthorization:
    if services.calendar is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.calendar


Service = Annotated[CalendarAuthorization, Depends(service)]


@router.post(
    "/tenants/{tenant_id}/integrations/google-calendar/authorizations",
    status_code=201,
    response_model=OAuthStartResponse,
)
async def start(
    tenant_id: ResourceId,
    body: OAuthStart,
    identity: Identity,
    calendar: Service,
    request: Request,
    response: Response,
) -> OAuthStartResponse:
    result = await run_in_threadpool(
        calendar.start, identity, tenant_id, body, _header(request, "Idempotency-Key")
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return result


@router.get("/oauth/google-calendar/callback")
async def callback(request: Request, calendar: Service) -> Response:
    params = request.query_params
    if any(len(params.getlist(name)) != 1 for name in params) or any(
        name not in {"state", "code", "error", "scope", "authuser", "prompt"} for name in params
    ):
        raise APIError(ErrorCode.INVALID_REQUEST)
    state, code, error = params.get("state", ""), params.get("code"), params.get("error")
    if (
        not 16 <= len(state) <= 512
        or (code is not None and not 1 <= len(code) <= 4096)
        or (error is not None and not 1 <= len(error) <= 100)
    ):
        raise APIError(ErrorCode.INVALID_REQUEST)
    location = await run_in_threadpool(calendar.callback, state, code, error)
    return Response(
        status_code=303,
        headers={
            "Location": location,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/tenants/{tenant_id}/integrations", response_model=IntegrationPage)
async def integrations(
    tenant_id: ResourceId,
    identity: Identity,
    calendar: Service,
    limit: int = 20,
    cursor: str | None = None,
) -> IntegrationPage:
    return await run_in_threadpool(calendar.list, identity, tenant_id, limit=limit, cursor=cursor)
