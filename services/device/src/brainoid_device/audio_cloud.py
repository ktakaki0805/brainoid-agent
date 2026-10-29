"""Narrow voice HTTP contracts, current daemon auth, bounded bytes, no retries."""

from collections.abc import Callable
from typing import Annotated, Literal

import httpx
from brainoid_contracts import ResourceId, UtcTimestamp
from pydantic import Field, ValidationError

from brainoid_device.audio_capture import MAX_WAV
from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError, Record
from brainoid_device.cloud_wire import MAX_BYTES, decode


class Transcript(Record):
    audio_input_id: ResourceId
    transcript: Annotated[str, Field(min_length=1, max_length=8000, repr=False)]
    language: Literal["ja-JP"] = "ja-JP"
    expires_at: UtcTimestamp


class AudioCloud:
    def __init__(self, client: CloudClient) -> None:
        self.client = client

    async def request(
        self,
        *,
        session: ResourceId | None,
        check: Callable[[], None],
        audio: bytes | None = None,
        identity: ResourceId,
    ) -> bytes:
        client = self.client
        check()
        token = await client.token()
        check()
        credentials = client.journal.credentials
        assert credentials is not None
        root = f"/v1/tenants/{credentials.tenant_id}"
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
        if session:
            headers["X-Private-Session"] = session
        if audio is None:
            path, method, expected = f"/speech-outputs/{identity}/audio", "GET", 200
            mime, limit = "audio/wav", MAX_WAV
            headers["Accept"] = mime
        else:
            path, method, expected = "/audio-inputs", "POST", 201
            mime, limit = "application/json", MAX_BYTES
            headers.update(
                {"Content-Type": "audio/wav", "Idempotency-Key": identity, "Accept": mime}
            )
        request = httpx.Request(
            method, client.settings.api_url + root + path, headers=headers, content=audio
        )
        try:
            response = await client.http.send(request, stream=True)
            try:
                check()
                if response.status_code != expected:
                    if response.status_code == 401:
                        client.stop()
                    raise CloudError(
                        "audio_cloud_refused",
                        response.status_code
                        if response.status_code in {403, 404, 409, 410, 422, 429}
                        else 503,
                    )
                if (
                    response.headers.get("content-type", "").split(";")[0] != mime
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise CloudError("audio_invalid_response", 502)
                data = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        check()
                        if len(data) + len(chunk) > limit:
                            raise CloudError("audio_invalid_response", 502)
                        data.extend(chunk)
                    check()
                    return bytes(data)
                finally:
                    data.clear()
            finally:
                await response.aclose()
        except (httpx.HTTPError, OSError):
            raise CloudError("audio_cloud_unavailable", 503) from None

    async def upload(
        self, audio: bytes, key: ResourceId, session: ResourceId | None, check: Callable[[], None]
    ) -> Transcript:
        raw = await self.request(audio=audio, identity=key, session=session, check=check)
        try:
            result = Transcript.model_validate(decode(raw))
        except ValidationError:
            raise CloudError("audio_invalid_response", 502) from None
        check()
        if result.expires_at.timestamp() <= self.client.now():
            raise CloudError("audio_expired", 410)
        return result

    async def fetch(
        self, speech: ResourceId, session: ResourceId | None, check: Callable[[], None]
    ) -> bytes:
        return await self.request(identity=speech, session=session, check=check)
