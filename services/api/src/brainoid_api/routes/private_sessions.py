"""Explicit smartphone consent, owner-only status and session termination."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.routes.tenants import Identity, _header
from brainoid_api.services.private_sessions import (
    PrivateSession,
    PrivateSessionCreate,
    PrivateSessions,
)

router = APIRouter(tags=["devices"])


def private_sessions(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> PrivateSessions:
    if services.private_sessions is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.private_sessions


Repository = Annotated[PrivateSessions, Depends(private_sessions)]


@router.post(
    "/tenants/{tenant_id}/devices/{device_id}/private-sessions",
    response_model=PrivateSession,
    status_code=201,
    operation_id="createPrivateSession",
    responses={"default": {"model": ErrorResponse}},
)
async def create_private_session(
    tenant_id: ResourceId,
    device_id: ResourceId,
    body: PrivateSessionCreate,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> PrivateSession:
    result = await run_in_threadpool(
        repository.start,
        identity,
        tenant_id,
        device_id,
        body,
        _header(request, "Idempotency-Key"),
        request.state.request_id,
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/private-sessions/{private_session_id}",
    response_model=PrivateSession,
    operation_id="getPrivateSession",
    responses={"default": {"model": ErrorResponse}},
)
async def get_private_session(
    tenant_id: ResourceId,
    private_session_id: ResourceId,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> PrivateSession:
    result = await run_in_threadpool(repository.get, identity, tenant_id, private_session_id)
    response.headers["ETag"] = etag(result.version)
    return result


@router.delete(
    "/tenants/{tenant_id}/private-sessions/{private_session_id}",
    status_code=204,
    operation_id="endPrivateSession",
    responses={"default": {"model": ErrorResponse}},
)
async def end_private_session(
    tenant_id: ResourceId,
    private_session_id: ResourceId,
    request: Request,
    identity: Identity,
    repository: Repository,
) -> Response:
    await run_in_threadpool(
        repository.end,
        identity,
        tenant_id,
        private_session_id,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    return Response(status_code=204)
