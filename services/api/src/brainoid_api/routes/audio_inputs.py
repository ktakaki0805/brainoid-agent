"""Raw WAV upload. Multipart spooling and unrestricted Request.body are not used."""

from typing import Annotated

from brainoid_contracts import ResourceId
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from brainoid_api.audio_inputs import MAX_BYTES, AudioInput, AudioInputs, receive_audio
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode, ErrorResponse
from brainoid_api.routes.agents import Reader, Session
from brainoid_api.routes.tenants import _header

router = APIRouter(tags=["speech"])


def repository(services: Annotated[AuthServices, Depends(get_auth_services)]) -> AudioInputs:
    if services.audio_inputs is None:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
    return services.audio_inputs


@router.post(
    "/tenants/{tenant_id}/audio-inputs",
    response_model=AudioInput,
    status_code=201,
    operation_id="transcribeAudio",
    responses={"default": {"model": ErrorResponse}},
)
async def transcribe_audio(
    tenant_id: ResourceId,
    identity: Reader,
    session: Session,
    service: Annotated[AudioInputs, Depends(repository)],
    request: Request,
) -> AudioInput:
    if (
        _header(request, "Content-Type") != "audio/wav"
        or _header(request, "Content-Encoding") is not None
    ):
        raise APIError(ErrorCode.VALIDATION_FAILED)
    length = _header(request, "Content-Length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise APIError(ErrorCode.INVALID_REQUEST)
        if int(length) > MAX_BYTES:
            raise APIError(ErrorCode.PAYLOAD_TOO_LARGE)
    key = _header(request, "Idempotency-Key")
    if key is None or not 16 <= len(key) <= 128:
        raise APIError(ErrorCode.INVALID_REQUEST)
    await run_in_threadpool(
        service.store.run, lambda tx: service.authority(tx, identity, tenant_id, session)
    )
    try:
        audio = await receive_audio(request.stream())
        if length is not None and int(length) != len(audio):
            raise APIError(ErrorCode.VALIDATION_FAILED)
        result = await service.transcribe(
            identity, tenant_id, session, audio, key, request.is_disconnected
        )
        del audio
        # Settlement can block: re-read current authority after it and retain the clock guard
        # across the thread handoff until FastAPI serializes the response.
        guarded = await run_in_threadpool(
            service.result, identity, tenant_id, session, result.audio_input_id
        )
        return guarded.release()
    except ClientDisconnect:
        raise APIError(ErrorCode.CONFLICT) from None
