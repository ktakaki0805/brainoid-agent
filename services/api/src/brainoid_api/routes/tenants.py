"""User-only tenant endpoints; every selection resolves current membership."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.tenants import Tenant, TenantCreate, TenantPage, TenantPatch, Tenants

router = APIRouter(tags=["tenants"])
Identity = Annotated[AuthenticatedIdentity, Depends(authenticated_identity)]


def tenant_repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Tenants:
    if services.tenants is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.tenants


Repository = Annotated[Tenants, Depends(tenant_repository)]


def _header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise APIError(ErrorCode.INVALID_REQUEST)
    return values[0] if values else None


@router.get(
    "/tenants",
    response_model=TenantPage,
    operation_id="listTenants",
    responses={"default": {"model": ErrorResponse}},
)
async def list_tenants(
    identity: Identity,
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> TenantPage:
    return await run_in_threadpool(repository.list, identity, limit=limit, cursor=cursor)


@router.post(
    "/tenants",
    response_model=Tenant,
    status_code=201,
    operation_id="createTenant",
    responses={"default": {"model": ErrorResponse}},
)
async def create_tenant(
    body: TenantCreate,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Tenant:
    result = await run_in_threadpool(
        repository.create,
        identity,
        body,
        _header(request, "Idempotency-Key"),
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}",
    response_model=Tenant,
    operation_id="getTenant",
    responses={"default": {"model": ErrorResponse}},
)
async def get_tenant(
    tenant_id: ResourceId,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Tenant:
    result = await run_in_threadpool(repository.get, identity, tenant_id)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}",
    response_model=Tenant,
    operation_id="updateTenant",
    responses={"default": {"model": ErrorResponse}},
)
async def update_tenant(
    tenant_id: ResourceId,
    body: TenantPatch,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Tenant:
    result = await run_in_threadpool(
        repository.update,
        identity,
        tenant_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
    )
    response.headers["ETag"] = etag(result.version)
    return result
