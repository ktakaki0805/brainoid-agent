"""Command acceptance and current status; physical execution is a separate protocol."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.commands import Command, CommandCreate
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["commands"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> CommandRequests:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return CommandRequests(services.memories)


Repository = Annotated[CommandRequests, Depends(repository)]


@router.post(
    "/tenants/{tenant_id}/commands",
    response_model=Command,
    status_code=202,
    response_model_exclude_none=True,
    operation_id="createDeviceCommand",
    responses={"default": {"model": ErrorResponse}},
)
async def create_command(
    tenant_id: ResourceId,
    body: CommandCreate,
    identity: Reader,
    commands: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> Command:
    result = await run_in_threadpool(
        commands.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/commands/{command_id}",
    response_model=Command,
    response_model_exclude_none=True,
    operation_id="getDeviceCommand",
    responses={"default": {"model": ErrorResponse}},
)
async def get_command(
    tenant_id: ResourceId,
    command_id: ResourceId,
    identity: Reader,
    commands: Repository,
    session: Session,
    response: Response,
) -> Command:
    result = await run_in_threadpool(commands.get, identity, tenant_id, command_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.post(
    "/tenants/{tenant_id}/commands/{command_id}:cancel",
    response_model=Command,
    response_model_exclude_none=True,
    operation_id="cancelDeviceCommand",
    responses={"default": {"model": ErrorResponse}},
)
async def cancel_command(
    tenant_id: ResourceId,
    command_id: ResourceId,
    identity: Reader,
    commands: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> Command:
    result = await run_in_threadpool(
        commands.cancel,
        identity,
        tenant_id,
        command_id,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
