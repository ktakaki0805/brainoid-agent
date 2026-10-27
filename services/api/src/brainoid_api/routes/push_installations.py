"""User-only global push registration. No token is returned."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.push_installations import PushCreate, PushPatch, PushView
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.push_installations import PushInstallations
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["push-installations"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> PushInstallations:
    if services.tenants is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return PushInstallations(services.tenants)


Repository = Annotated[PushInstallations, Depends(repository)]


@router.post(
    "/users/me/push-installations",
    response_model=PushView,
    status_code=201,
    operation_id="registerPushInstallation",
    responses={"default": {"model": ErrorResponse}},
)
async def register(
    body: PushCreate,
    identity: Identity,
    installations: Repository,
    request: Request,
    response: Response,
) -> PushView:
    result = await run_in_threadpool(
        installations.mutate, identity, body, _header(request, "Idempotency-Key")
    )
    assert result is not None
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/users/me/push-installations/{installation_id}",
    response_model=PushView,
    operation_id="refreshPushInstallation",
    responses={"default": {"model": ErrorResponse}},
)
async def refresh(
    installation_id: ResourceId,
    body: PushPatch,
    identity: Identity,
    installations: Repository,
    request: Request,
    response: Response,
) -> PushView:
    result = await run_in_threadpool(
        installations.mutate,
        identity,
        body,
        _header(request, "Idempotency-Key"),
        installation=installation_id,
        if_match=_header(request, "If-Match"),
    )
    assert result is not None
    response.headers["ETag"] = etag(result.version)
    return result


@router.delete(
    "/users/me/push-installations/{installation_id}",
    status_code=204,
    operation_id="removePushInstallation",
    responses={"default": {"model": ErrorResponse}},
)
async def remove(
    installation_id: ResourceId, identity: Identity, installations: Repository, request: Request
) -> Response:
    await run_in_threadpool(
        installations.mutate,
        identity,
        None,
        _header(request, "Idempotency-Key"),
        installation=installation_id,
        if_match=_header(request, "If-Match"),
    )
    return Response(status_code=204)
