"""Device bearer plus session reference; Firebase and bare session IDs cannot delegate."""

from dataclasses import dataclass
from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import Depends, Request
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_device, get_auth_services
from brainoid_api.errors import APIError, ErrorCode

_resource_id = TypeAdapter(ResourceId)


@dataclass(frozen=True)
class PrivateDeviceIdentity:
    device: DeviceIdentity
    session_id: ResourceId


async def private_session_context(
    request: Request,
    device: Annotated[DeviceIdentity, Depends(authenticated_device)],
    services: Annotated[AuthServices, Depends(get_auth_services)],
) -> PrivateDeviceIdentity:
    """Admission only. Resource handlers must call PrivateSessions.current in their tx."""
    values = request.headers.getlist("x-private-session")
    if len(values) != 1:
        raise APIError(ErrorCode.INVALID_REQUEST)
    try:
        session = _resource_id.validate_python(values[0])
    except ValidationError:
        raise APIError(ErrorCode.INVALID_REQUEST) from None
    if services.private_sessions is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    await run_in_threadpool(services.private_sessions.context, device, session)
    return PrivateDeviceIdentity(device, session)
