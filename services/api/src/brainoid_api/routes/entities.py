"""Entity HTTP contract. Provider inventory registration is intentionally internal."""

from typing import Annotated

from brainoid_contracts import ResourceId
from brainoid_memory import EntityPatch
from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.versioning import etag
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.entities import Entities, EntityCreate, EntityPage, EntityView
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["entities"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> Entities:
    if services.entities is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.entities


Repository = Annotated[Entities, Depends(repository)]


@router.get(
    "/tenants/{tenant_id}/entities",
    response_model=EntityPage,
    operation_id="listEntities",
    responses={"default": {"model": ErrorResponse}},
)
async def list_entities(
    tenant_id: ResourceId,
    identity: Reader,
    entities: Repository,
    session: Session,
    query: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> EntityPage:
    return await run_in_threadpool(
        entities.list, identity, tenant_id, session, query=query, limit=limit, cursor=cursor
    )


@router.post(
    "/tenants/{tenant_id}/entities",
    response_model=EntityView,
    status_code=201,
    response_model_exclude_none=True,
    operation_id="createEntity",
    responses={"default": {"model": ErrorResponse}},
)
async def create_entity(
    tenant_id: ResourceId,
    body: EntityCreate,
    identity: Reader,
    entities: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> EntityView:
    result = await run_in_threadpool(
        entities.create, identity, tenant_id, body, _header(request, "Idempotency-Key"), session
    )
    response.headers["ETag"] = etag(result.version)
    return result


@router.get(
    "/tenants/{tenant_id}/entities/{entity_id}",
    response_model=EntityView,
    response_model_exclude_none=True,
    operation_id="getEntity",
    responses={"default": {"model": ErrorResponse}},
)
async def get_entity(
    tenant_id: ResourceId,
    entity_id: ResourceId,
    identity: Reader,
    entities: Repository,
    session: Session,
    response: Response,
) -> EntityView:
    result = await run_in_threadpool(entities.get, identity, tenant_id, entity_id, session)
    response.headers["ETag"] = etag(result.version)
    return result


@router.patch(
    "/tenants/{tenant_id}/entities/{entity_id}",
    response_model=EntityView,
    response_model_exclude_none=True,
    operation_id="updateEntity",
    responses={"default": {"model": ErrorResponse}},
)
async def update_entity(
    tenant_id: ResourceId,
    entity_id: ResourceId,
    body: EntityPatch,
    identity: Reader,
    entities: Repository,
    session: Session,
    request: Request,
    response: Response,
) -> EntityView:
    result = await run_in_threadpool(
        entities.update,
        identity,
        tenant_id,
        entity_id,
        body,
        _header(request, "Idempotency-Key"),
        _header(request, "If-Match"),
        session,
    )
    response.headers["ETag"] = etag(result.version)
    return result
