"""Task CRUD over the same domain service used by typed conversation tools."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.tasks import TaskCreate, TaskPage, TaskPatch, TaskStatus, TaskView
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.tasks import TaskRequests
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["tasks"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> TaskRequests:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return TaskRequests(services.memories)


Repository = Annotated[TaskRequests, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/tasks",
    response_model=TaskPage,
    operation_id="listTasks",
    responses={"default": {"model": ErrorResponse}},
)
async def list_tasks(
    tenant_id: ResourceId,
    identity: Reader,
    tasks: Repository,
    session: Session,
    status: TaskStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> TaskPage:
    return await run_in_threadpool(
        tasks.list, identity, tenant_id, session, status=status, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/tasks",
    response_model=TaskView,
    status_code=201,
    response_model_exclude_none=True,
    operation_id="createTask",
    responses={"default": {"model": ErrorResponse}},
)
async def create_task(
    tenant_id: ResourceId,
    body: TaskCreate,
    identity: Reader,
    tasks: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> TaskView:
    result = await run_in_threadpool(
        tasks.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/tasks/{task_id}",
    response_model=TaskView,
    response_model_exclude_none=True,
    operation_id="getTask",
    responses={"default": {"model": ErrorResponse}},
)
async def get_task(
    tenant_id: ResourceId,
    task_id: ResourceId,
    identity: Reader,
    tasks: Repository,
    session: Session,
    response: Response,
) -> TaskView:
    result = await run_in_threadpool(tasks.get, identity, tenant_id, task_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/tasks/{task_id}",
    response_model=TaskView,
    response_model_exclude_none=True,
    operation_id="updateTask",
    responses={"default": {"model": ErrorResponse}},
)
async def update_task(
    tenant_id: ResourceId,
    task_id: ResourceId,
    body: TaskPatch,
    identity: Reader,
    tasks: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> TaskView:
    result = await run_in_threadpool(
        tasks.update,
        identity,
        tenant_id,
        task_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
