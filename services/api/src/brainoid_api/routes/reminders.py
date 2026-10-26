"""Reminder CRUD over the same domain service used by typed conversation tools."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.reminders import (
    ReminderCreate,
    ReminderPage,
    ReminderPatch,
    ReminderSnooze,
    ReminderStatus,
    ReminderView,
)
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["reminders"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> ReminderRequests:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return ReminderRequests(services.memories)


Repository = Annotated[ReminderRequests, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/reminders",
    response_model=ReminderPage,
    operation_id="listReminders",
    responses={"default": {"model": ErrorResponse}},
)
async def list_reminders(
    tenant_id: ResourceId,
    identity: Reader,
    reminders: Repository,
    session: Session,
    status: ReminderStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> ReminderPage:
    return await run_in_threadpool(
        reminders.list, identity, tenant_id, session, status=status, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/reminders",
    response_model=ReminderView,
    status_code=201,
    response_model_exclude_none=True,
    operation_id="createReminder",
    responses={"default": {"model": ErrorResponse}},
)
async def create_reminder(
    tenant_id: ResourceId,
    body: ReminderCreate,
    identity: Reader,
    reminders: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> ReminderView:
    result = await run_in_threadpool(
        reminders.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/reminders/{reminder_id}",
    response_model=ReminderView,
    response_model_exclude_none=True,
    operation_id="getReminder",
    responses={"default": {"model": ErrorResponse}},
)
async def get_reminder(
    tenant_id: ResourceId,
    reminder_id: ResourceId,
    identity: Reader,
    reminders: Repository,
    session: Session,
    response: Response,
) -> ReminderView:
    result = await run_in_threadpool(reminders.get, identity, tenant_id, reminder_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/reminders/{reminder_id}",
    response_model=ReminderView,
    response_model_exclude_none=True,
    operation_id="updateReminder",
    responses={"default": {"model": ErrorResponse}},
)
async def update_reminder(
    tenant_id: ResourceId,
    reminder_id: ResourceId,
    body: ReminderPatch,
    identity: Reader,
    reminders: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> ReminderView:
    result = await run_in_threadpool(
        reminders.update,
        identity,
        tenant_id,
        reminder_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.post(
    "/tenants/{tenant_id}/reminders/{reminder_id}:snooze",
    response_model=ReminderView,
    response_model_exclude_none=True,
    operation_id="snoozeReminder",
    responses={"default": {"model": ErrorResponse}},
)
async def snooze_reminder(
    tenant_id: ResourceId,
    reminder_id: ResourceId,
    body: ReminderSnooze,
    identity: Reader,
    reminders: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> ReminderView:
    result = await run_in_threadpool(
        reminders.snooze,
        identity,
        tenant_id,
        reminder_id,
        body,
        _header(request, "Idempotency-Key"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
