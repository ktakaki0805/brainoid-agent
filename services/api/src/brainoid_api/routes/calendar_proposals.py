"""Private proposal production and owner-only preview/rejection."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.calendar_proposals import Change, ProposalView
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import Identity, _header
from brainoid_api.services.calendar_proposals import Proposals

router = APIRouter(tags=["calendar"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Proposals:
    if services.calendar_events is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return Proposals(services.calendar_events, services.conversations)


Repository = Annotated[Proposals, Depends(repository)]


@router.post(
    "/tenants/{tenant_id}/integrations/{integration_id}/calendar-proposals",
    response_model=ProposalView,
    status_code=201,
    operation_id="createCalendarProposal",
)
async def create(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    body: Change,
    identity: Reader,
    session: Session,
    repo: Repository,
    request: Request,
    response: Response,
) -> ProposalView:
    result = await run_in_threadpool(
        repo.create_for_release,
        identity,
        tenant_id,
        integration_id,
        body,
        _header(request, "Idempotency-Key"),
        session,
    )
    response.headers["Cache-Control"] = "no-store"
    return result.release()


@router.get(
    "/tenants/{tenant_id}/calendar-proposals/{proposal_id}",
    response_model=ProposalView,
    operation_id="getCalendarProposal",
)
async def get(
    tenant_id: ResourceId,
    proposal_id: ResourceId,
    identity: Identity,
    repo: Repository,
    response: Response,
) -> ProposalView:
    result = await run_in_threadpool(repo.get_for_release, identity, tenant_id, proposal_id)
    response.headers["Cache-Control"] = "no-store"
    return result.release()


@router.post(
    "/tenants/{tenant_id}/calendar-proposals/{proposal_id}:reject",
    response_model=ProposalView,
    operation_id="rejectCalendarProposal",
)
async def reject(
    tenant_id: ResourceId,
    proposal_id: ResourceId,
    identity: Identity,
    repo: Repository,
    request: Request,
    response: Response,
) -> ProposalView:
    if await request.body():
        raise APIError(ErrorCode.INVALID_REQUEST)
    result = await run_in_threadpool(
        repo.reject_for_release,
        identity,
        tenant_id,
        proposal_id,
        _header(request, "Idempotency-Key"),
    )
    response.headers["Cache-Control"] = "no-store"
    return result.release()
