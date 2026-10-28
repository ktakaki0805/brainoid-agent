"""HA metadata management and dedicated device-only inventory admission."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_device, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.home_assistant_entities import (
    HomeAssistantEntities,
    HomeIntegration,
    HomeIntegrationCreate,
    HomeInventorySync,
)
from brainoid_api.repositories.entities import EntityPage
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["integrations"])


def service(services: Annotated[AuthServices, Depends(get_auth_services)]) -> HomeAssistantEntities:
    if services.home_assistant is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.home_assistant


Service = Annotated[HomeAssistantEntities, Depends(service)]
Device = Annotated[DeviceIdentity, Depends(authenticated_device)]


@router.post(
    "/tenants/{tenant_id}/integrations/home-assistant",
    status_code=201,
    response_model=HomeIntegration,
)
async def create(
    tenant_id: ResourceId,
    body: HomeIntegrationCreate,
    identity: Identity,
    home: Service,
    request: Request,
    response: Response,
) -> HomeIntegration:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        home.create, identity, tenant_id, body, _header(request, "Idempotency-Key")
    )


@router.get(
    "/tenants/{tenant_id}/integrations/{integration_id}/home-assistant",
    response_model=HomeIntegration,
)
async def get(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    identity: Identity,
    home: Service,
    response: Response,
) -> HomeIntegration:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(home.get, identity, tenant_id, integration_id)


@router.delete(
    "/tenants/{tenant_id}/integrations/{integration_id}/home-assistant",
    response_model=HomeIntegration,
)
async def revoke(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    identity: Identity,
    home: Service,
    request: Request,
    response: Response,
) -> HomeIntegration:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        home.revoke, identity, tenant_id, integration_id, _header(request, "Idempotency-Key")
    )


@router.post(
    "/tenants/{tenant_id}/integrations/{integration_id}/home-assistant/entities:sync",
    response_model=EntityPage,
)
async def sync(
    tenant_id: ResourceId,
    integration_id: ResourceId,
    body: HomeInventorySync,
    identity: Device,
    home: Service,
    request: Request,
    response: Response,
) -> EntityPage:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        home.sync, identity, tenant_id, integration_id, body, _header(request, "Idempotency-Key")
    )
