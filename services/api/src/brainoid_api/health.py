"""Health checks independent of authentication and future business APIs."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.settings import Settings


class ReadinessProbe(Protocol):
    async def is_ready(self) -> bool:
        """Return current availability without exposing service details."""
        ...


@dataclass
class RuntimeState:
    settings: Settings | None = None
    probes: Sequence[ReadinessProbe] = ()
    started: bool = False


def get_runtime(request: Request) -> RuntimeState:
    return cast(RuntimeState, request.app.state.runtime)


def get_settings(runtime: Annotated[RuntimeState, Depends(get_runtime)]) -> Settings:
    if runtime.settings is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return runtime.settings


class HealthResponse(BaseModel):
    status: Literal["ok", "ready"]


router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse, operation_id="getLiveness")
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=HealthResponse,
    operation_id="getReadiness",
    responses={503: {"model": ErrorResponse}},
)
async def readiness(runtime: Annotated[RuntimeState, Depends(get_runtime)]) -> HealthResponse:
    if not runtime.started or runtime.settings is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

    async def check(probe: ReadinessProbe) -> bool:
        try:
            return await probe.is_ready()
        except Exception:
            return False

    try:
        async with asyncio.timeout(runtime.settings.readiness_timeout_seconds):
            results = await asyncio.gather(*(check(probe) for probe in runtime.probes))
    except TimeoutError:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    if not all(results):
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return HealthResponse(status="ready")
