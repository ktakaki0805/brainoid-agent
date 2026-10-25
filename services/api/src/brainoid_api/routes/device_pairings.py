"""User confirmation and Pi-only secret exchange; no secrets in browser responses."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.repositories.device_credentials import (
    Device,
    DeviceCredentialRepository,
    DeviceCredentials,
    DeviceRefresh,
    Pairing,
    PairingConfirm,
    PairingCreate,
    PairingExchange,
)
from brainoid_api.routes.tenants import Identity, _header

router = APIRouter(tags=["devices"])


def device_repository(
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> DeviceCredentialRepository:
    if services.devices is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.devices


Repository = Annotated[DeviceCredentialRepository, Depends(device_repository)]


def _ip(request: Request) -> str:
    # Ignore Forwarded/X-Forwarded-For. Deployment must enforce the trusted proxy boundary.
    if request.client is None:
        raise APIError(ErrorCode.INVALID_REQUEST)
    return request.client.host


@router.post(
    "/device-pairings",
    response_model=Pairing,
    status_code=201,
    operation_id="createDevicePairing",
    responses={"default": {"model": ErrorResponse}},
)
async def create_pairing(
    body: PairingCreate, request: Request, response: Response, repository: Repository
) -> Pairing:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.create, body, _header(request, "Idempotency-Key"), _ip(request)
    )


@router.post(
    "/tenants/{tenant_id}/device-pairings:confirm",
    response_model=Device,
    status_code=201,
    operation_id="confirmDevicePairing",
    responses={"default": {"model": ErrorResponse}},
)
async def confirm_pairing(
    tenant_id: ResourceId,
    body: PairingConfirm,
    request: Request,
    response: Response,
    identity: Identity,
    repository: Repository,
) -> Device:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.confirm,
        identity,
        tenant_id,
        body,
        _header(request, "Idempotency-Key"),
        _ip(request),
    )


@router.post(
    "/device-pairings/{pairing_id}:exchange",
    response_model=DeviceCredentials,
    operation_id="exchangeDevicePairing",
    responses={"default": {"model": ErrorResponse}},
)
async def exchange_pairing(
    pairing_id: ResourceId,
    body: PairingExchange,
    request: Request,
    response: Response,
    repository: Repository,
) -> DeviceCredentials:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.exchange, pairing_id, body, _header(request, "Idempotency-Key"), _ip(request)
    )


@router.post(
    "/device-tokens:refresh",
    response_model=DeviceCredentials,
    operation_id="refreshDeviceToken",
    responses={"default": {"model": ErrorResponse}},
)
async def refresh_device(
    body: DeviceRefresh, request: Request, response: Response, repository: Repository
) -> DeviceCredentials:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(
        repository.refresh, body, _header(request, "Idempotency-Key"), _ip(request)
    )
