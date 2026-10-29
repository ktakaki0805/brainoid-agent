"""Explicit Google Speech v2 REST adapter; no SDK or credentials enter Core types."""

import base64
import json
import re
from collections.abc import Awaitable, Callable
from decimal import ROUND_CEILING, Decimal

import httpx
from brainoid_agent_core.types.speech import SpeechRecognition
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from brainoid_api.audio_inputs import Recognition
from brainoid_api.errors import APIError, ErrorCode


class SpeechSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    location: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    recognizer: str = Field(pattern=r"^(?:_|[a-z][a-z0-9-]{0,62})$")
    model: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,127}$")


class GoogleSpeech:
    def __init__(
        self,
        settings: SpeechSettings,
        token: Callable[[], Awaitable[SecretStr]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings, self.token = settings, token
        host = (
            "speech.googleapis.com"
            if settings.location == "global"
            else settings.location + "-speech.googleapis.com"
        )
        self.url = f"https://{host}/v2/projects/{settings.project}/locations/{settings.location}/recognizers/{settings.recognizer}:recognize"
        self.client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=40
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def recognize(self, request: SpeechRecognition) -> Recognition:
        try:
            if (
                request.format.encoding != "pcm_s16le"
                or request.format.sample_rate_hz != 16000
                or request.format.channels != 1
                or request.locale != "ja-JP"
            ):
                raise ValueError("unsupported_speech_request")
            token = await self.token()
            async with self.client.stream(
                "POST",
                self.url,
                headers={"Authorization": "Bearer " + token.get_secret_value()},
                json={
                    "config": {
                        "model": self.settings.model,
                        "languageCodes": ["ja-JP"],
                        "explicitDecodingConfig": {
                            "encoding": "LINEAR16",
                            "sampleRateHertz": 16000,
                            "audioChannelCount": 1,
                        },
                    },
                    "configMask": "*",
                    "content": base64.b64encode(request.audio).decode("ascii"),
                },
            ) as response:
                if response.status_code == 429:
                    raise APIError(ErrorCode.RATE_LIMITED)
                if response.status_code != 200:
                    raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > 128 * 1024:
                        raise ValueError("speech_response_too_large")
                    data.extend(part)
                document = json.loads(data)
            results = document.get("results", [])
            if not isinstance(results, list) or len(results) > 1000:
                raise ValueError("invalid_speech_results")
            texts = []
            for result in results:
                if result.get("languageCode", "ja-JP") != "ja-JP":
                    raise ValueError("invalid_speech_language")
                alternatives = result.get("alternatives", [])
                if alternatives:
                    text = alternatives[0]["transcript"]
                    if not isinstance(text, str):
                        raise ValueError("invalid_speech_text")
                    texts.append(text)
            text = "".join(texts).strip()
            if len(text) > 8000:
                raise ValueError("speech_text_too_large")
            duration = document.get("metadata", {}).get("totalBilledDuration")
            billed = None
            if duration is not None:
                if (
                    not isinstance(duration, str)
                    or re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,9})?s", duration) is None
                ):
                    raise ValueError("invalid_speech_usage")
                billed = int(Decimal(duration[:-1]).to_integral_value(rounding=ROUND_CEILING))
            return Recognition(text, billed)
        except APIError:
            raise
        except Exception:
            # Request/response/error representations may contain audio, text or bearer.
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
