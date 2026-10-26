"""One explicitly addressed Vertex generateContent call; no autonomous tool execution."""

import asyncio
import json
import math
import os
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

import httpx
from brainoid_agent_core.ports import CallControl, Clock
from brainoid_agent_core.types import (
    Cancelled,
    Completed,
    Failed,
    JsonObject,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    TimedOut,
    ToolCall,
    ToolReply,
    ToolRequested,
    Usage,
)
from google import genai
from google.auth.credentials import Credentials
from google.auth.transport import Request
from google.genai import errors, types
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator
from referencing import Registry
from referencing.exceptions import Unresolvable


class GoogleModelSettings(BaseModel):
    """No environment lookup or defaults for deployment/cost choices."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    provider: Literal["vertex"]
    project: str
    location: str
    model_id: str
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=300, allow_inf_nan=False)]
    max_output_tokens: Annotated[int, Field(strict=True, gt=0, le=65536)]

    @field_validator("project", "location", "model_id")
    @classmethod
    def address(cls, value: str) -> str:
        # Bare IDs prevent SDK acceptance of cross-project/region resource names or URLs.
        if re.fullmatch(r"[a-z][a-z0-9.-]{0,126}", value) is None or ".." in value:
            raise ValueError("Invalid model address component")
        return value

    @property
    def endpoint(self) -> str:
        if self.location == "global":
            return "https://aiplatform.googleapis.com"
        return f"https://{self.location}-aiplatform.googleapis.com"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _InvalidResponse(Exception):
    pass


class _MissingCredentials(Exception):
    pass


class _TokenSnapshot(Credentials):
    """Caller refreshes credentials before dispatch; SDK cannot start auth HTTP."""

    def __init__(self, token: str) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.token = token

    def refresh(self, request: Request) -> None:
        raise _MissingCredentials


class _Stopped(Exception):
    def __init__(self, event: Cancelled | TimedOut) -> None:
        self.event = event


def _require_finite_json(value: object) -> None:
    """Check SDK native values without rejecting legitimate bytes/tuple fields."""
    if isinstance(value, float) and not math.isfinite(value):
        raise _InvalidResponse
    if isinstance(value, dict):
        for item in value.values():
            _require_finite_json(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_finite_json(item)


def _require_json(value: object) -> None:
    """Inspect raw JSON containers without copying, coercing, serializing or repr."""
    if value is None or type(value) is str or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        _require_finite_json(value)
    elif type(value) is list:
        for item in value:
            _require_json(item)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _InvalidResponse
            _require_json(item)
    else:
        raise _InvalidResponse


def _require_request_json(request: ModelRequest) -> None:
    # Frozen DTOs still contain mutable JSON. Validate these fields directly,
    # before deepcopy hooks or Pydantic warnings can expose an invalid value.
    for message in request.messages:
        if message.provider_state is not None:
            _require_json(message.provider_state)
        for part in message.parts:
            if isinstance(part, ToolReply):
                _require_json(part.result)
    for tool in request.tools:
        _require_json(tool.parameters)
    if request.response_schema is not None:
        _require_json(request.response_schema)


def _parse_json(value: str) -> JsonValue:
    # Pydantic accepts NaN/Infinity and overflows large exponents to infinity.
    parsed = TypeAdapter(JsonValue).validate_json(value)
    _require_json(parsed)
    return parsed


def _json_object(value: str) -> JsonObject:
    parsed = _parse_json(value)
    if not isinstance(parsed, dict):
        raise _InvalidResponse
    return parsed


def _safe_json(value: object) -> str:
    _require_json(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _validate_schema(schema: JsonObject) -> None:
    # Only local refs: never allow the validator to fetch an external schema.
    def walk(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$ref", "$dynamicRef") and (
                    not isinstance(item, str) or not item.startswith("#")
                ):
                    raise _InvalidResponse
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(schema)
    Draft202012Validator.check_schema(schema)


class GoogleModelProvider:
    """Credentials and Clock are explicit; each invocation owns its HTTP clients.

    transport_factory is a test seam. Production uses HTTPX without environment
    proxies, redirects or transport retries. Never pass user-controlled transports.
    """

    def __init__(
        self,
        settings: GoogleModelSettings,
        *,
        credentials: Credentials,
        clock: Clock,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        if credentials is None:
            raise ValueError("Explicit Google Cloud credentials are required")
        self.settings = settings
        self._credentials = credentials
        self._clock = clock
        self._transport_factory = transport_factory

    def _stopped(self, control: CallControl, deadline: float) -> Cancelled | TimedOut | None:
        if control.cancellation.cancelled:
            return Cancelled()
        if self._clock.monotonic() >= deadline:
            return TimedOut()
        return None

    async def _watch(self, control: CallControl, deadline: float) -> None:
        while (stopped := self._stopped(control, deadline)) is None:
            await self._clock.sleep(min(0.05, max(0.0, deadline - self._clock.monotonic())))
        raise _Stopped(stopped)

    async def _next(
        self, stream: AsyncGenerator[ModelEvent], control: CallControl, deadline: float
    ) -> ModelEvent:
        if stopped := self._stopped(control, deadline):
            raise _Stopped(stopped)
        # A task repr in asyncio debug logs must not contain SDK exceptions or text.
        outcome: list[ModelEvent | Exception] = []

        async def read() -> None:
            try:
                outcome.append(await anext(stream))
            except Exception as error:
                outcome.append(error)

        pending = asyncio.create_task(read())
        watcher = asyncio.create_task(self._watch(control, deadline))
        try:
            await asyncio.wait((pending, watcher), return_when=asyncio.FIRST_COMPLETED)
            if stopped := self._stopped(control, deadline):
                raise _Stopped(stopped)
            if watcher.done():
                watcher.result()
            pending.result()
            result = outcome[0]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            pending.cancel()
            watcher.cancel()
            await asyncio.gather(pending, watcher, return_exceptions=True)

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        deadline = self._clock.monotonic() + self.settings.timeout_seconds
        if control.deadline is not None:
            deadline = min(deadline, control.deadline)
        if stopped := self._stopped(control, deadline):
            yield stopped
            return
        source: AsyncGenerator[ModelEvent] | None = None
        terminal: Completed | Failed | Cancelled | TimedOut
        try:
            _require_request_json(request)
            source = self._events(request.model_copy(deep=True), control, deadline)
            while True:
                event = await self._next(source, control, deadline)
                if isinstance(event, (Completed, Failed, Cancelled, TimedOut)):
                    terminal = event
                    break
                yield event
        except _Stopped as stopped_error:
            terminal = stopped_error.event
        except StopAsyncIteration:
            terminal = Failed(code="stream_interrupted", retryable=True)
        except (TimeoutError, httpx.TimeoutException):
            terminal = TimedOut()
        except _MissingCredentials:
            terminal = Failed(code="permission_denied")
        except errors.APIError as error:
            if error.code == 429:
                terminal = Failed(code="rate_limited", retryable=True)
            elif error.code in (408, 504):
                terminal = TimedOut()
            elif error.code in (401, 403):
                terminal = Failed(code="permission_denied")
            elif error.code == 400:
                terminal = Failed(code="invalid_arguments")
            elif error.code == 404:
                terminal = Failed(code="not_found")
            else:
                terminal = Failed(code="unavailable", retryable=error.code >= 500)
        except (
            ValueError,
            TypeError,
            _InvalidResponse,
            errors.UnknownApiResponseError,
            SchemaError,
            Unresolvable,
            RecursionError,
        ):
            terminal = Failed(code="invalid_arguments")
        except Exception:
            # Do not log/return raw SDK, credential, schema or provider exception text.
            terminal = Failed(code="unavailable", retryable=True)
        finally:
            if source is not None:
                with suppress(Exception):
                    await source.aclose()
        yield self._stopped(control, deadline) or terminal

    def _binding(self) -> JsonObject:
        return {
            "provider": "google-genai-v1",
            "project": self.settings.project,
            "location": self.settings.location,
            "model": self.settings.model_id,
        }

    def _input(
        self, request: ModelRequest
    ) -> tuple[list[types.Content], types.GenerateContentConfig]:
        contents: list[types.Content] = []
        system: list[types.Part] = []
        call_ids: dict[str, str | None] = {}
        for message in request.messages:
            if message.provider_state is not None:
                state = message.provider_state
                if message.role != "assistant" or state.get("binding") != self._binding():
                    raise _InvalidResponse
                content = types.Content.model_validate(state.get("content"))
                keys = state.get("call_keys")
                if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                    raise _InvalidResponse
                expected, ids = self._visible(content.parts or [], keys)
                if tuple(expected) != message.parts or content.role != "model":
                    raise _InvalidResponse
                call_ids.update(ids)
                contents.append(content)
                continue
            parts: list[types.Part] = []
            for part in message.parts:
                if isinstance(part, TextPart) and message.role != "tool":
                    parts.append(types.Part(text=part.text))
                elif isinstance(part, ToolCall) and message.role == "assistant":
                    args = _json_object(part.arguments_json)
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id=part.call_key, name=part.name, args=args
                            )
                        )
                    )
                    call_ids[part.call_key] = part.call_key
                elif isinstance(part, ToolReply) and message.role == "tool":
                    if part.call_key not in call_ids:
                        raise _InvalidResponse
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call_ids[part.call_key],
                                name=part.name,
                                response={"error" if part.is_error else "output": part.result},
                            )
                        )
                    )
                else:
                    raise _InvalidResponse
            if message.role == "system":
                if contents:
                    raise _InvalidResponse
                system.extend(parts)
            else:
                contents.append(
                    types.Content(
                        role="model" if message.role == "assistant" else "user", parts=parts
                    )
                )
        if not contents:
            raise _InvalidResponse
        for tool in request.tools:
            _validate_schema(tool.parameters)
        if request.response_schema is not None:
            _validate_schema(request.response_schema)
        config = types.GenerateContentConfig(
            system_instruction=types.Content(parts=system) if system else None,
            candidate_count=1,
            max_output_tokens=min(request.max_output_tokens, self.settings.max_output_tokens),
            response_mime_type="application/json" if request.response_schema is not None else None,
            response_json_schema=request.response_schema,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            automatic_continuation=False,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool.name,
                            description=tool.description,
                            parameters_json_schema=tool.parameters,
                        )
                        for tool in request.tools
                    ]
                )
            ]
            if request.tools
            else None,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    stream_function_call_arguments=False
                )
            )
            if request.tools
            else None,
            thinking_config=types.ThinkingConfig(include_thoughts=False),
        )
        return contents, config

    def _visible(
        self, parts: list[types.Part], keys: list[JsonValue] | None = None
    ) -> tuple[list[TextPart | ToolCall], dict[str, str | None]]:
        visible: list[TextPart | ToolCall] = []
        ids: dict[str, str | None] = {}
        for part in parts:
            allowed = {"text", "function_call", "thought", "thought_signature"}
            if set(part.model_dump(exclude_none=True)) - allowed:
                raise _InvalidResponse
            if part.function_call is not None:
                call = part.function_call
                if part.text is not None or part.thought or not call.name or call.args is None:
                    raise _InvalidResponse
                if call.partial_args is not None or call.will_continue is not None:
                    raise _InvalidResponse
                key = call.id or f"google-call-{len(ids)}"
                if keys is not None:
                    if len(ids) >= len(keys) or not isinstance(keys[len(ids)], str):
                        raise _InvalidResponse
                    key = str(keys[len(ids)])
                if key in ids:
                    raise _InvalidResponse
                ids[key] = call.id
                visible.append(
                    ToolCall(call_key=key, name=call.name, arguments_json=_safe_json(call.args))
                )
            elif part.text and not part.thought:
                visible.append(TextPart(text=part.text))
        if keys is not None and len(ids) != len(keys):
            raise _InvalidResponse
        return visible, ids

    async def _events(
        self, request: ModelRequest, control: CallControl, deadline: float
    ) -> AsyncGenerator[ModelEvent]:
        contents, config = self._input(request)
        token = self._credentials.token
        if not isinstance(token, str) or not token or self._credentials.expired:
            raise _MissingCredentials
        credentials = _TokenSnapshot(token)
        if os.getenv("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false").lower() != "false":
            raise _InvalidResponse
        responses: list[httpx.Response] = []

        async def check_destination(request: httpx.Request) -> None:
            if stopped := self._stopped(control, deadline):
                raise _Stopped(stopped)
            if (
                request.url.scheme != "https"
                or request.url.host != httpx.URL(self.settings.endpoint).host
                or "x-goog-api-key" in request.headers
            ):
                raise _InvalidResponse

        async def remember(response: httpx.Response) -> None:
            responses.append(response)

        remaining = max(0.001, deadline - self._clock.monotonic())
        transport = self._transport_factory() if self._transport_factory else None
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=remaining,
            transport=transport,
            event_hooks={"request": [check_destination], "response": [remember]},
        ) as http:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=remaining) as sync:
                client = genai.Client(
                    vertexai=True,
                    project=self.settings.project,
                    location=self.settings.location,
                    credentials=credentials,
                    debug_config=genai.client.DebugConfig(
                        client_mode=None, replays_directory=None, replay_id=None
                    ),
                    http_options=types.HttpOptions(
                        api_version="v1",
                        base_url=self.settings.endpoint,
                        timeout=math.ceil(remaining * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                        httpx_client=sync,
                        httpx_async_client=http,
                    ),
                )
                stream = None
                try:
                    stream = cast(
                        AsyncGenerator[types.GenerateContentResponse],
                        await client.aio.models.generate_content_stream(
                            model=self.settings.model_id, contents=contents, config=config
                        ),
                    )
                    all_parts: list[types.Part] = []
                    finish: Literal["stop", "length", "content_filter"] | None = None
                    usage = Usage()
                    response_bytes = 0
                    async for chunk in stream:
                        _require_finite_json(chunk.model_dump(mode="python"))
                        response_bytes += len(chunk.model_dump_json().encode())
                        if response_bytes > 8 * 1024 * 1024:
                            raise _InvalidResponse
                        if chunk.usage_metadata is not None:
                            metadata = chunk.usage_metadata
                            # Each report is cumulative. Missing fields stay unknown.
                            usage = Usage(
                                input_tokens=metadata.prompt_token_count
                                if metadata.prompt_token_count is not None
                                else usage.input_tokens,
                                output_tokens=metadata.candidates_token_count
                                if metadata.candidates_token_count is not None
                                else usage.output_tokens,
                                cached_input_tokens=metadata.cached_content_token_count
                                if metadata.cached_content_token_count is not None
                                else usage.cached_input_tokens,
                            )
                            yield usage
                        if (
                            chunk.prompt_feedback
                            and chunk.prompt_feedback.block_reason
                            and chunk.prompt_feedback.block_reason
                            != types.BlockedReason.BLOCKED_REASON_UNSPECIFIED
                        ):
                            if finish is not None:
                                raise _InvalidResponse
                            finish = "content_filter"
                            continue
                        if not chunk.candidates:
                            continue
                        if len(chunk.candidates) != 1:
                            raise _InvalidResponse
                        candidate = chunk.candidates[0]
                        if finish is not None:
                            raise _InvalidResponse
                        if candidate.index not in (None, 0):
                            raise _InvalidResponse
                        new_parts = candidate.content.parts if candidate.content else None
                        if new_parts:
                            self._visible(new_parts)
                            all_parts.extend(new_parts)
                            if request.response_schema is None:
                                for part in new_parts:
                                    if part.text and not part.thought:
                                        yield TextDelta(text=part.text)
                        if (
                            candidate.finish_reason
                            and candidate.finish_reason
                            != types.FinishReason.FINISH_REASON_UNSPECIFIED
                        ):
                            reason = str(candidate.finish_reason.value)
                            if reason == "STOP":
                                finish = "stop"
                            elif reason in ("MAX_TOKENS", "CONTINUATION"):
                                finish = "length"
                            elif reason in (
                                "SAFETY",
                                "RECITATION",
                                "BLOCKLIST",
                                "PROHIBITED_CONTENT",
                                "SPII",
                                "IMAGE_SAFETY",
                            ):
                                finish = "content_filter"
                            else:
                                raise _InvalidResponse
                    if finish is None:
                        yield Failed(code="stream_interrupted", retryable=True)
                        return
                    visible, ids = self._visible(all_parts)
                    text = "".join(part.text for part in visible if isinstance(part, TextPart))
                    if request.response_schema is not None and finish == "stop":
                        parsed = _parse_json(text)
                        if not Draft202012Validator(
                            request.response_schema, registry=Registry()
                        ).is_valid(parsed):
                            raise _InvalidResponse
                        if text:
                            yield TextDelta(text=text)
                    yield usage
                    # Truncated/blocked calls are never eligible tool candidates or history.
                    if finish == "stop":
                        for visible_part in visible:
                            if isinstance(visible_part, ToolCall):
                                yield ToolRequested(call=visible_part)
                        content = types.Content(role="model", parts=all_parts)
                        yield ModelResponse(
                            message=ModelMessage(
                                role="assistant",
                                parts=tuple(visible),
                                provider_state={
                                    "binding": self._binding(),
                                    "content": _json_object(
                                        content.model_dump_json(exclude_none=True)
                                    ),
                                    "call_keys": list(ids),
                                },
                            )
                        )
                    yield Completed(
                        finish_reason="tool_calls" if finish == "stop" and ids else finish
                    )
                finally:
                    if stream is not None:
                        with suppress(Exception):
                            await stream.aclose()
                    # SDK nested generators do not consistently close their response on aclose.
                    for response in responses:
                        with suppress(Exception):
                            await response.aclose()
                    with suppress(Exception):
                        await client.aio.aclose()
                    with suppress(Exception):
                        client.close()
