"""One explicit Vertex embedding request, using the workspace's fixed official SDK."""

import os
from collections.abc import Callable
from typing import Annotated, Literal

import httpx
from brainoid_memory.models import MemoryValue
from brainoid_memory.vector_store import EmbeddingConfig, VectorUnavailable
from google import genai
from google.auth.credentials import Credentials
from google.genai import types
from pydantic import Field

from brainoid_model_google.provider import _TokenSnapshot


class GoogleEmbeddingSettings(MemoryValue):
    project: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    location: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=30, allow_inf_nan=False)]

    @property
    def endpoint(self) -> str:
        return (
            "https://aiplatform.googleapis.com"
            if self.location == "global"
            else f"https://{self.location}-aiplatform.googleapis.com"
        )


class GoogleEmbeddings:
    def __init__(
        self,
        config: EmbeddingConfig,
        settings: GoogleEmbeddingSettings,
        *,
        credentials: Credentials,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ) -> None:
        self.config = EmbeddingConfig.model_validate(config.model_dump())
        self.settings = GoogleEmbeddingSettings.model_validate(settings.model_dump())
        self.credentials, self.transport_factory = credentials, transport_factory

    def embed(self, text: str, *, task: Literal["document", "query"]) -> tuple[float, ...]:
        token = self.credentials.token
        if (
            not isinstance(token, str)
            or not token
            or self.credentials.expired
            or any(os.environ.get(name) for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"))
            or os.getenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false").lower() != "false"
        ):
            raise VectorUnavailable()

        def destination(request: httpx.Request) -> None:
            if (
                request.url.scheme != "https"
                or request.url.host != httpx.URL(self.settings.endpoint).host
                or "x-goog-api-key" in request.headers
            ):
                raise VectorUnavailable()

        try:
            with (
                httpx.Client(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=self.settings.timeout_seconds,
                    transport=self.transport_factory() if self.transport_factory else None,
                    event_hooks={"request": [destination]},
                ) as http,
                genai.Client(
                    vertexai=True,
                    project=self.settings.project,
                    location=self.settings.location,
                    credentials=_TokenSnapshot(token),
                    debug_config=genai.client.DebugConfig(
                        client_mode=None, replays_directory=None, replay_id=None
                    ),
                    http_options=types.HttpOptions(
                        api_version="v1",
                        base_url=self.settings.endpoint,
                        timeout=int(self.settings.timeout_seconds * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                        httpx_client=http,
                    ),
                ) as client,
            ):
                result = client.models.embed_content(
                    model=self.config.model,
                    contents=text,
                    config=types.EmbedContentConfig(
                        task_type="RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY",
                        output_dimensionality=self.config.dimensions,
                        auto_truncate=False,
                    ),
                )
        except Exception:
            raise VectorUnavailable() from None
        if result.embeddings is None or len(result.embeddings) != 1:
            raise VectorUnavailable()
        values = result.embeddings[0].values
        if values is None:
            raise VectorUnavailable()
        return self.config.vector(tuple(values))
