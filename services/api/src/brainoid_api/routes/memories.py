"""Explicit memory HTTP contract with current provenance and atomic revisions."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.memories import (
    Memories,
    MemoryCreate,
    MemoryPage,
    MemoryRevision,
    MemoryView,
)
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["memories"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Memories:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.memories


Repository = Annotated[Memories, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/memories",
    response_model=MemoryPage,
    operation_id="listMemories",
    responses={"default": {"model": ErrorResponse}},
)
async def list_memories(
    tenant_id: ResourceId,
    identity: Reader,
    memories: Repository,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> MemoryPage:
    return await run_in_threadpool(
        memories.list, identity, tenant_id, session, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/memories",
    response_model=MemoryView,
    status_code=201,
    response_model_exclude_none=True,
    operation_id="createMemory",
    responses={"default": {"model": ErrorResponse}},
)
async def create_memory(
    tenant_id: ResourceId,
    body: MemoryCreate,
    identity: Reader,
    memories: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> MemoryView:
    result = await run_in_threadpool(
        memories.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/memories/{memory_id}",
    response_model=MemoryView,
    response_model_exclude_none=True,
    operation_id="getMemory",
    responses={"default": {"model": ErrorResponse}},
)
async def get_memory(
    tenant_id: ResourceId,
    memory_id: ResourceId,
    identity: Reader,
    memories: Repository,
    session: Session,
    response: Response,
) -> MemoryView:
    result = await run_in_threadpool(memories.get, identity, tenant_id, memory_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/memories/{memory_id}",
    response_model=MemoryView,
    response_model_exclude_none=True,
    operation_id="reviseMemory",
    responses={"default": {"model": ErrorResponse}},
)
async def revise_memory(
    tenant_id: ResourceId,
    memory_id: ResourceId,
    body: MemoryRevision,
    identity: Reader,
    memories: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> MemoryView:
    result = await run_in_threadpool(
        memories.revise,
        identity,
        tenant_id,
        memory_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
