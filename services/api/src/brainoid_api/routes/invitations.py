"""Code issuance and authenticated, self-only acceptance."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.invitations import (
    Invitation,
    InvitationAccept,
    InvitationCreate,
    Invitations,
    MembershipResult,
)
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["invitations"])


def invitation_repository(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> Invitations:
    if services.invitations is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.invitations


Repository = Annotated[Invitations, Depends(invitation_repository)]


@router.post(
    "/tenants/{tenant_id}/invitations",
    response_model=Invitation,
    status_code=201,
    operation_id="createInvitation",
    responses={"default": {"model": ErrorResponse}},
)
async def create_invitation(
    tenant_id: ResourceId,
    body: InvitationCreate,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Invitation:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.create, identity, tenant_id, body, _header(request, "Idempotency-Key")
    )


@router.post(
    "/invitations:accept",
    response_model=MembershipResult,
    status_code=201,
    operation_id="acceptInvitation",
    responses={"default": {"model": ErrorResponse}},
)
async def accept_invitation(
    body: InvitationAccept,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> MembershipResult:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.accept, identity, body, _header(request, "Idempotency-Key")
    )
