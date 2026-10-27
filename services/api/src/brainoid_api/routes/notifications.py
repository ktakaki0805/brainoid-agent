"""Notification content is returned only through current authenticated authorization."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory.notifications import NotificationAck, NotificationPage, NotificationView
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.notifications import NotificationRequests
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["notifications"])


def repository(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> NotificationRequests:
    if services.memories is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return NotificationRequests(services.memories)


Repository = Annotated[NotificationRequests, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/notifications",
    response_model=NotificationPage,
    operation_id="listNotifications",
    responses={"default": {"model": ErrorResponse}},
)
async def list_notifications(
    tenant_id: ResourceId,
    identity: Reader,
    notifications: Repository,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> NotificationPage:
    return await run_in_threadpool(
        notifications.list, identity, tenant_id, session, limit=limit, cursor=cursor
    )


@router.get(
    "/tenants/{tenant_id}/notifications/{notification_id}",
    response_model=NotificationView,
    operation_id="getNotification",
    responses={"default": {"model": ErrorResponse}},
)
async def get_notification(
    tenant_id: ResourceId,
    notification_id: ResourceId,
    identity: Reader,
    notifications: Repository,
    session: Session,
    response: Response,
) -> NotificationView:
    result = await run_in_threadpool(
        notifications.get, identity, tenant_id, notification_id, session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.post(
    "/tenants/{tenant_id}/notifications/{notification_id}:ack",
    response_model=NotificationView,
    operation_id="ackNotification",
    responses={"default": {"model": ErrorResponse}},
)
async def ack_notification(
    tenant_id: ResourceId,
    notification_id: ResourceId,
    body: NotificationAck,
    identity: Reader,
    notifications: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> NotificationView:
    result = await run_in_threadpool(
        notifications.ack,
        identity,
        tenant_id,
        notification_id,
        body,
        _header(request, "Idempotency-Key"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
