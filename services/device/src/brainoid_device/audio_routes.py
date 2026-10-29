"""Loopback/session/CSRF protected controls. Raw PCM never crosses into the UI."""

import asyncio
from typing import Annotated, Literal

from brainoid_contracts import ResourceId
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from brainoid_device.audio_service import AudioService
from brainoid_device.cloud_models import CloudError, Record
from brainoid_device.local_api import error
from brainoid_device.ui_bridge import GuardedResponse, local_body


class AudioControl(Record):
    operation: Literal["select", "start", "finish", "send", "discard", "stop"]
    private_session_id: ResourceId | None = None
    input_device: Annotated[str, Field(max_length=128)] | None = None
    output_device: Annotated[str, Field(max_length=128)] | None = None


def audio_routes(audio: AudioService) -> list[Route]:
    async def dispatch(request: Request) -> Response:
        current = (
            audio.sessions.authorize_mutation(request)
            if request.method == "POST"
            else audio.sessions.find(request)
        )
        if isinstance(current, Response):
            return current
        if current is None:
            return error("session_required", 401)
        try:
            if request.method == "GET":
                if request.url.path == "/local/audio/devices":
                    async with asyncio.timeout(4):
                        devices = await audio.backend.devices()
                    if not audio.sessions.valid(current[0]):
                        raise CloudError("session_required", 401)
                    return JSONResponse({"devices": devices})
                return JSONResponse(audio.state(current[0]))
            operation = AudioControl.model_validate(local_body(await request.body()))
            if operation.operation in {"stop", "discard"}:
                audio.clear()
                return JSONResponse({"stopped": True, "run_cancelled": False})
            audio.client.check_output(operation.private_session_id)
            if operation.operation == "select":
                if operation.input_device is None or operation.output_device is None:
                    raise ValueError
                await audio.select(operation.input_device, operation.output_device, current[0])
            elif operation.operation == "start":
                audio.start_capture(current[0], operation.private_session_id)
            else:
                binding = audio.binding
                if (
                    binding is None
                    or binding.owner != current[0]
                    or binding.session != operation.private_session_id
                ):
                    raise CloudError("audio_context_changed", 409)
                audio.check(binding)
                if operation.operation == "finish":
                    audio.finish_capture()
                else:
                    transcript = await audio.send(current[0])
                    return GuardedResponse(
                        JSONResponse(transcript.model_dump(mode="json")),
                        audio.client,
                        audio.sessions,
                        request,
                        lambda: audio.check(binding),
                    )
            return JSONResponse(audio.state(current[0]))
        except (ValidationError, ValueError):
            return error("invalid_operation", 400)
        except CloudError as exc:
            return error(exc.code, exc.status)
        except (OSError, TimeoutError):
            audio.notice = "audio_device_unavailable"
            return error("audio_device_unavailable", 503)
        except asyncio.CancelledError:
            return error("audio_stopped", 409)

    return [
        Route("/local/audio", dispatch, methods=["GET", "POST"]),
        Route("/local/audio/devices", dispatch, methods=["GET"]),
    ]
