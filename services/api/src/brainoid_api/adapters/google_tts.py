"""Explicit Cloud Text-to-Speech v1 REST; bounded inline response, no retries/storage."""

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from brainoid_api.errors import APIError, ErrorCode


class TTSSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    location: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    voice: str = Field(pattern=r"^ja-JP-[A-Za-z0-9-]{1,100}$")


class GoogleTTS:
    def __init__(
        self,
        settings: TTSSettings,
        token: Callable[[], Awaitable[SecretStr]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings, self.token = settings, token
        host = (
            "texttospeech.googleapis.com"
            if settings.location == "global"
            else settings.location + "-texttospeech.googleapis.com"
        )
        self.url = f"https://{host}/v1/text:synthesize"
        self.client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=40
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def synthesize(self, text: str) -> bytes:
        try:
            if not text.strip() or len(text.encode()) > 4000:
                raise ValueError("invalid_synthesis_text")
            # Includes credential acquisition and response reading, including dribbling bodies.
            async with asyncio.timeout(40):
                token = await self.token()
                async with self.client.stream(
                    "POST",
                    self.url,
                    headers={
                        "Authorization": "Bearer " + token.get_secret_value(),
                        "x-goog-user-project": self.settings.project,
                    },
                    json={
                        "input": {"text": text},
                        "voice": {"languageCode": "ja-JP", "name": self.settings.voice},
                        "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 16000},
                    },
                ) as response:
                    if response.status_code == 429:
                        raise APIError(ErrorCode.RATE_LIMITED)
                    if response.status_code != 200:
                        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunk) > 2_600_000 - len(body):
                            raise ValueError("tts_response_limit")
                        body.extend(chunk)
                    try:
                        payload = json.loads(body)
                        audio = base64.b64decode(payload["audioContent"], validate=True)
                    finally:
                        body.clear()
                    if not audio or len(audio) > 1_921_024:
                        raise ValueError("tts_audio_limit")
                    return audio
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
