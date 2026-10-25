"""User-only device management; credentials are never returned here."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.device_credentials import Device
from brainoid_api.repositories.devices import DevicePage, DevicePatch, Devices
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["devices"])


def device_management(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Devices:
    if services.device_management is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.device_management


Repository = Annotated[Devices, Depends(device_management)]


@router.get(
    "/tenants/{tenant_id}/devices",
    response_model=DevicePage,
    operation_id="listDevices",
    responses={"default": {"model": ErrorResponse}},
)
async def list_devices(
    tenant_id: ResourceId,
    identity: Identity,
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> DevicePage:
    return await run_in_threadpool(repository.list, identity, tenant_id, limit=limit, cursor=cursor)


@router.get(
    "/tenants/{tenant_id}/devices/{device_id}",
    response_model=Device,
    operation_id="getDevice",
    responses={"default": {"model": ErrorResponse}},
)
async def get_device(
    tenant_id: ResourceId,
    device_id: ResourceId,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Device:
    result = await run_in_threadpool(repository.get, identity, tenant_id, device_id)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/devices/{device_id}",
    response_model=Device,
    operation_id="updateDevice",
    responses={"default": {"model": ErrorResponse}},
)
async def update_device(
    tenant_id: ResourceId,
    device_id: ResourceId,
    body: DevicePatch,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Device:
    result = await run_in_threadpool(
        repository.update,
        identity,
        tenant_id,
        device_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.delete(
    "/tenants/{tenant_id}/devices/{device_id}",
    status_code=204,
    operation_id="revokeDevice",
    responses={"default": {"model": ErrorResponse}},
)
async def revoke_device(
    tenant_id: ResourceId,
    device_id: ResourceId,
    request: Request,
    identity: Identity,
    repository: Repository,
) -> Response:
    await run_in_threadpool(
        repository.revoke,
        identity,
        tenant_id,
        device_id,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    return Response(status_code=204)
