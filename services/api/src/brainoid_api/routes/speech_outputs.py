"""Only stored, authorized sentences can be synthesized; no arbitrary-text endpoint."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.speech_outputs import SpeechOutputs

router = APIRouter(tags=["speech"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> SpeechOutputs:
    if services.speech_outputs is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.speech_outputs


@router.get(
    "/tenants/{tenant_id}/speech-outputs/{speech_id}/audio",
    operation_id="getSpeechAudio",
    response_class=Response,
    responses={
        200: {"content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}},
        "default": {"model": ErrorResponse},
    },
)
async def speech_audio(
    tenant_id: ResourceId,
    speech_id: ResourceId,
    identity: Reader,
    session: Session,
    service: Annotated[SpeechOutputs, Depends(repository)],
    request: Request,
) -> Response:
    audio = await service.synthesize(
        identity, tenant_id, session, speech_id, request.is_disconnected
    )
    guarded = await run_in_threadpool(
        service.authorize,
        identity,
        tenant_id,
        session,
        speech_id,
        attempt_deadline=audio.attempt_deadline,
    )
    guarded.release()
    return Response(
        audio.audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
