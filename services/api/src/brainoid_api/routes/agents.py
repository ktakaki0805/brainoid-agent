"""User/device reads share the same versioned personality; updates require a user."""

from typing import Annotated

from brainoid_agent_core.definition import AgentDefinition
from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.agents import AgentIdentity, AgentPage, AgentPatch, Agents
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["agents"])
_resource_id = TypeAdapter(ResourceId)


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Agents:
    if services.agents is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.agents


async def reader_identity(
    request: Request,
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> AgentIdentity:
    header = _header(request, "Authorization")
    if header is None:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    scheme, separator, token = header.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or any(c.isspace() for c in token):
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    if services.devices is not None:
        try:
            return await run_in_threadpool(services.devices.authenticate, token)
        except APIError as error:
            if error.code != ErrorCode.AUTHENTICATION_REQUIRED:
                raise
    # Each verifier validates its own issuer/audience/token kind. A device or session
    # ID never becomes a Firebase user identity through unverified claims.
    return await authenticated_identity(request, services)


def session_id(request: Request) -> ResourceId | None:
    value = _header(request, "X-Private-Session")
    if value is None:
        return None
    try:
        return _resource_id.validate_python(value)
    except ValidationError:
        raise APIError(ErrorCode.INVALID_REQUEST) from None


Repository = Annotated[Agents, Depends(repository)]
Reader = Annotated[AgentIdentity, Depends(reader_identity)]
Session = Annotated[ResourceId | None, Depends(session_id)]


@router.get(
    "/tenants/{tenant_id}/agents",
    response_model=AgentPage,
    operation_id="listAgents",
    responses={"default": {"model": ErrorResponse}},
)
async def list_agents(
    tenant_id: ResourceId,
    identity: Reader,
    agents: Repository,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> AgentPage:
    return await run_in_threadpool(
        agents.list, identity, tenant_id, session, limit=limit, cursor=cursor
    )


@router.get(
    "/tenants/{tenant_id}/agents/{agent_id}",
    response_model=AgentDefinition,
    operation_id="getAgent",
    responses={"default": {"model": ErrorResponse}},
)
async def get_agent(
    tenant_id: ResourceId,
    agent_id: ResourceId,
    identity: Reader,
    agents: Repository,
    session: Session,
    response: Response,
) -> AgentDefinition:
    result = await run_in_threadpool(agents.get, identity, tenant_id, agent_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/agents/{agent_id}",
    response_model=AgentDefinition,
    operation_id="updateAgent",
    responses={"default": {"model": ErrorResponse}},
)
async def update_agent(
    tenant_id: ResourceId,
    agent_id: ResourceId,
    body: AgentPatch,
    identity: Identity,
    agents: Repository,
    request: Request,
    response: Response,
) -> AgentDefinition:
    if _header(request, "X-Private-Session") is not None:
        raise APIError(ErrorCode.INVALID_REQUEST)
    result = await run_in_threadpool(
        agents.update,
        identity,
        tenant_id,
        agent_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
    )
    response.headers["ETag"] = etag(result.version)
    return result
