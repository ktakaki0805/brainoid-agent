"""User-only membership metadata and serialized management routes."""

from typing import Annotated

from brainoid_contracts import FirebaseUid, ResourceId
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.memberships import (
    MembershipPage,
    MembershipPatch,
    Memberships,
    MembershipView,
    OwnerTransfer,
)
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["memberships"])


def membership_repository(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> Memberships:
    if services.memberships is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.memberships


Repository = Annotated[Memberships, Depends(membership_repository)]


@router.get(
    "/tenants/{tenant_id}/memberships",
    response_model=MembershipPage,
    operation_id="listMemberships",
    responses={"default": {"model": ErrorResponse}},
)
async def list_memberships(
    tenant_id: ResourceId,
    identity: Identity,
    repository: Repository,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> MembershipPage:
    return await run_in_threadpool(repository.list, identity, tenant_id, limit=limit, cursor=cursor)


@router.post(
    "/tenants/{tenant_id}/memberships:transfer-owner",
    response_model=MembershipView,
    operation_id="transferOwner",
    responses={"default": {"model": ErrorResponse}},
)
async def transfer_owner(
    tenant_id: ResourceId,
    body: OwnerTransfer,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> MembershipView:
    result = await run_in_threadpool(
        repository.transfer,
        identity,
        tenant_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/memberships",
    response_model=MembershipView,
    operation_id="updateMembershipByUid",
    responses={"default": {"model": ErrorResponse}},
)
@router.patch(
    "/tenants/{tenant_id}/memberships/{user_id:path}",
    response_model=MembershipView,
    operation_id="updateMembership",
    responses={"default": {"model": ErrorResponse}},
)
async def update_membership(
    tenant_id: ResourceId,
    user_id: FirebaseUid,
    body: MembershipPatch,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> MembershipView:
    # The collection route takes the UID as a required query value. The legacy
    # path route keeps its existing binding; opaque IDs never become DB paths.
    if "user_id" not in request.path_params and len(request.query_params.getlist("user_id")) != 1:
        raise APIError(ErrorCode.INVALID_REQUEST)
    result = await run_in_threadpool(
        repository.update,
        identity,
        tenant_id,
        user_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.delete(
    "/tenants/{tenant_id}/memberships",
    status_code=204,
    operation_id="revokeMembershipByUid",
    responses={"default": {"model": ErrorResponse}},
)
@router.delete(
    "/tenants/{tenant_id}/memberships/{user_id:path}",
    status_code=204,
    operation_id="revokeMembership",
    responses={"default": {"model": ErrorResponse}},
)
async def revoke_membership(
    tenant_id: ResourceId,
    user_id: FirebaseUid,
    request: Request,
    identity: Identity,
    repository: Repository,
) -> Response:
    if "user_id" not in request.path_params and len(request.query_params.getlist("user_id")) != 1:
        raise APIError(ErrorCode.INVALID_REQUEST)
    await run_in_threadpool(
        repository.revoke,
        identity,
        tenant_id,
        user_id,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        request.state.request_id,
    )
    return Response(status_code=204)
