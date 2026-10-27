"""Calendar reads and explicit user-owned allowlist updates."""

from typing import Annotated

from brainoid_contracts import ProviderId, ResourceId, UtcTimestamp
from brainoid_memory.calendar_oauth import IntegrationView
from brainoid_memory.calendar_reader import CalendarPage, CalendarQuery, CalendarSelection
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import Identity, _header
from brainoid_api.services.calendar_events import CalendarEvents

router = APIRouter(tags=["calendar"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> CalendarEvents:
    if services.calendar_events is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.calendar_events


Repository = Annotated[CalendarEvents, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/integrations/{integration_id}/calendar-events",
    response_model=CalendarPage,
    operation_id="listCalendarEvents",
)
async def list_events(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    calendar_id: ProviderId,
    time_min: UtcTimestamp,
    time_max: UtcTimestamp,
    identity: Reader,
    session: Session,
    repo: Repository,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> CalendarPage:
    try:
        query = CalendarQuery(
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        raise APIError(ErrorCode.INVALID_REQUEST) from None
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(repo.list, identity, tenant_id, integration_id, query, session)


@router.patch(
    "/tenants/{tenant_id}/integrations/{integration_id}",
    response_model=IntegrationView,
    operation_id="updateIntegration",
)
async def select_calendars(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    body: CalendarSelection,
    identity: Identity,
    repo: Repository,
    request: Request,
    response: Response,
) -> IntegrationView:
    result = await run_in_threadpool(
        repo.select,
        identity,
        tenant_id,
        integration_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
    )
    response.headers["ETag"] = etag(result.version)
    response.headers["Cache-Control"] = "no-store"
    return result
