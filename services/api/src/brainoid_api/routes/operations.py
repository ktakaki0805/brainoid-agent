"""User-only forget and lifecycle acceptance with narrowly scoped status access."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.forget import ForgetCreate, Operation
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.routes.tenants import Identity, _header
from brainoid_api.services.deletion_requests import (
    DeleteAccount,
    DeleteTenant,
    DeletionRequests,
    LeaveTenant,
)

router = APIRouter(tags=["operations"])


def lifecycle_repository(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> DeletionRequests:
    if services.deletion_requests is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.deletion_requests


Lifecycle = Annotated[DeletionRequests, Depends(lifecycle_repository)]


@router.post(
    "/tenants/{tenant_id}/operations",
    response_model=Operation,
    status_code=202,
    operation_id="createTenantOperation",
    responses={"default": {"model": ErrorResponse}},
)
async def create_operation(
    tenant_id: ResourceId,
    body: ForgetCreate | LeaveTenant | DeleteTenant,
    identity: Identity,
    services: Annotated[AuthServices, Depends(get_auth_services)],
    request: Request,
) -> Operation:
    if isinstance(body, ForgetCreate):
        if services.memories is None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        return await run_in_threadpool(
            services.memories.forget, identity, tenant_id, body, _header(request, "Idempotency-Key")
        )
    repo = lifecycle_repository(services)
    return await run_in_threadpool(
        repo.create, identity, body, _header(request, "Idempotency-Key"), tenant_id
    )


@router.get(
    "/tenants/{tenant_id}/operations/{operation_id}",
    response_model=Operation,
    operation_id="getTenantOperation",
    responses={"default": {"model": ErrorResponse}},
)
async def get_operation(
    tenant_id: ResourceId,
    operation_id: ResourceId,
    identity: Identity,
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> Operation:
    if services.deletion_requests is not None:
        try:
            return await run_in_threadpool(
                services.deletion_requests.get, identity, operation_id, tenant_id
            )
        except APIError as error:
            if error.code != ErrorCode.RESOURCE_NOT_FOUND:
                raise
    if services.memories is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    return await run_in_threadpool(services.memories.operation, identity, tenant_id, operation_id)


@router.post(
    "/operations",
    response_model=Operation,
    status_code=202,
    operation_id="createAccountOperation",
    responses={"default": {"model": ErrorResponse}},
)
async def create_account_operation(
    body: DeleteAccount, identity: Identity, repository: Lifecycle, request: Request
) -> Operation:
    return await run_in_threadpool(
        repository.create, identity, body, _header(request, "Idempotency-Key")
    )


@router.get(
    "/operations/{operation_id}",
    response_model=Operation,
    operation_id="getAccountOperation",
    responses={"default": {"model": ErrorResponse}},
)
async def get_account_operation(
    operation_id: ResourceId, identity: Identity, repository: Lifecycle
) -> Operation:
    return await run_in_threadpool(repository.get, identity, operation_id)
