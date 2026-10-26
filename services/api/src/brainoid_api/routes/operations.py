"""User-only forget acceptance and status; other lifecycle kinds are not enabled."""

from brainoid_contracts import ResourceId
from brainoid_memory.forget import ForgetCreate, Operation
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from brainoid_api.errors import ErrorResponse
from brainoid_api.routes.memories import Repository
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["operations"])


@router.post(
    "/tenants/{tenant_id}/operations",
    response_model=Operation,
    status_code=202,
    operation_id="createTenantOperation",
    responses={"default": {"model": ErrorResponse}},
)
async def create_operation(
    tenant_id: ResourceId,
    body: ForgetCreate,
    identity: Identity,
    memories: Repository,
    request: Request,
) -> Operation:
    return await run_in_threadpool(
        memories.forget, identity, tenant_id, body, _header(request, "Idempotency-Key")
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
    memories: Repository,
) -> Operation:
    return await run_in_threadpool(memories.operation, identity, tenant_id, operation_id)
