"""Real pinned Gen AI serialization/SSE parsing over an offline HTTPX transport."""

import asyncio
import base64
import io
import json
import logging
import unittest
import warnings
from collections.abc import AsyncIterator
from contextlib import aclosing, redirect_stderr
from datetime import datetime
from typing import cast
from unittest.mock import patch

import httpx
from brainoid_agent_core.ports import CallControl, ModelProvider
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
    ToolDefinition,
    ToolReply,
    ToolRequested,
    Usage,
)
from brainoid_model_google import GoogleModelProvider, GoogleModelSettings, SystemClock
from fakes.runtime import CancellationToken
from google.auth.credentials import AnonymousCredentials, Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from pydantic import JsonValue, ValidationError
from test_agent_core import model_contract

SETTINGS = GoogleModelSettings(
    provider="vertex",
    project="fixture-project",
    location="us-central1",
    model_id="fixture-model",
    timeout_seconds=5.0,
    max_output_tokens=100,
)
REQUEST = ModelRequest(messages=(ModelMessage(role="user", parts=(TextPart(text="FIXTURE"),)),))


def chunk(parts: list[dict[str, object]], finish: str | None = None) -> dict[str, object]:
    candidate: dict[str, object] = {"index": 0, "content": {"role": "model", "parts": parts}}
    if finish is not None:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


class Body(httpx.AsyncByteStream):
    def __init__(self, steps: list[dict[str, object] | bytes | Exception | asyncio.Event]) -> None:
        self.steps = steps
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            for step in self.steps:
                if isinstance(step, Exception):
                    raise step
                if isinstance(step, asyncio.Event):
                    self.waiting.set()
                    await step.wait()
                elif isinstance(step, bytes):
                    yield step
                else:
                    yield b"data: " + json.dumps(step).encode() + b"\n\n"
        finally:
            self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, body: Body, status: int = 200, opening: asyncio.Event | None = None) -> None:
        self.body = body
        self.status = status
        self.opening = opening
        self.requests: list[httpx.Request] = []
        self.entered = asyncio.Event()
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.entered.set()
        if self.opening is not None:
            await self.opening.wait()
        return httpx.Response(
            self.status, stream=self.body, headers={"content-type": "text/event-stream"}
        )

    async def aclose(self) -> None:
        self.closed = True


class ManualClock(SystemClock):
    def __init__(self) -> None:
        self.elapsed = 100.0
        self.changed = asyncio.Event()

    def monotonic(self) -> float:
        return self.elapsed

    async def sleep(self, seconds: float) -> None:
        await self.changed.wait()
        self.changed.clear()

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.changed.set()


class GoogleModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.token = CancellationToken()
        self.control = CallControl(self.token, None)

    def provider(
        self,
        transport: Transport,
        settings: GoogleModelSettings = SETTINGS,
        clock: SystemClock | None = None,
    ) -> GoogleModelProvider:
        credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]
        credentials.token = "FIXTURE-TOKEN"
        return GoogleModelProvider(
            settings,
            credentials=credentials,
            clock=clock or SystemClock(),
            transport_factory=lambda: transport,
        )

    async def collect(
        self, body: Body, *, request: ModelRequest = REQUEST, status: int = 200
    ) -> tuple[tuple[ModelEvent, ...], Transport]:
        transport = Transport(body, status)
        events = await model_contract(self.provider(transport), request, self.control)
        self.assertTrue(transport.closed)
        self.assertTrue(body.closed)
        self.assertEqual(len(transport.requests), 1)
        return events, transport

    async def test_text_usage_and_explicit_address_ignore_sdk_environment(self) -> None:
        body = Body(
            [
                chunk([{"text": "HELLO"}]),
                {"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1}},
                chunk([{"text": " WORLD"}], "STOP"),
                {"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3}},
            ]
        )
        env = {
            "GEMINI_API_KEY": "SECRET-GEMINI",
            "GOOGLE_API_KEY": "SECRET-GOOGLE",
            "GOOGLE_CLOUD_PROJECT": "wrong-project",
            "GOOGLE_CLOUD_LOCATION": "global",
            "GOOGLE_GENAI_USE_VERTEXAI": "false",
            "GOOGLE_GENAI_USE_ENTERPRISE": "false",
            "GOOGLE_VERTEX_BASE_URL": "https://wrong.example",
            "GOOGLE_GEMINI_BASE_URL": "https://wrong.example",
            "HTTPS_PROXY": "http://wrong.example:123",
            "GOOGLE_GENAI_HTTP_CLIENT": "httpx2",
            "GOOGLE_GENAI_CLIENT_MODE": "record",
            "GOOGLE_GENAI_REPLAYS_DIRECTORY": "/fixture-never-write",
            "GOOGLE_GENAI_REPLAY_ID": "fixture-never-record",
        }
        with (
            patch.dict("os.environ", env),
            patch("google.auth.default", side_effect=AssertionError("ADC")),
        ):
            events, transport = await self.collect(body)
        self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], ["HELLO", " WORLD"])
        self.assertEqual(
            [e for e in events if isinstance(e, Usage)][-1], Usage(input_tokens=4, output_tokens=3)
        )
        self.assertEqual(events[-1], Completed())
        request = transport.requests[0]
        self.assertEqual(
            str(request.url),
            "https://us-central1-aiplatform.googleapis.com/v1/projects/fixture-project/locations/us-central1/publishers/google/models/fixture-model:streamGenerateContent?alt=sse",
        )
        self.assertNotIn("x-goog-api-key", request.headers)
        payload = json.loads(request.content)
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 100)
        self.assertEqual(payload["generationConfig"]["candidateCount"], 1)
        self.assertFalse(payload["generationConfig"]["thinkingConfig"]["include_thoughts"])
        self.assertLessEqual(request.extensions["timeout"]["read"], 5)

    async def test_tool_roundtrip_preserves_signatures_and_missing_provider_ids(self) -> None:
        signature = base64.b64encode(b"OPAQUE-SIGNATURE").decode()
        body = Body(
            [
                chunk([{"text": "PRIVATE THOUGHT", "thought": True}]),
                chunk(
                    [
                        {
                            "functionCall": {"name": "fixture_tool", "args": {"x": 1}},
                            "thoughtSignature": signature,
                        }
                    ]
                ),
                chunk(
                    [
                        {
                            "functionCall": {
                                "id": "provider-2",
                                "name": "fixture_tool",
                                "args": {"x": 2},
                            }
                        }
                    ],
                    "STOP",
                ),
            ]
        )
        request = REQUEST.model_copy(
            update={
                "tools": (
                    ToolDefinition(
                        name="fixture_tool", description="FIXTURE", parameters={"type": "object"}
                    ),
                )
            }
        )
        events, transport = await self.collect(body, request=request)
        calls = [e.call for e in events if isinstance(e, ToolRequested)]
        self.assertEqual([c.call_key for c in calls], ["google-call-0", "provider-2"])
        self.assertFalse(any(isinstance(e, TextDelta) for e in events))
        self.assertEqual(events[-1], Completed(finish_reason="tool_calls"))
        response = next(e.message for e in events if isinstance(e, ModelResponse))
        self.assertNotIn("PRIVATE THOUGHT", repr(response))
        self.assertNotIn("OPAQUE", repr(response))
        self.assertFalse(
            json.loads(transport.requests[0].content)["toolConfig"]["functionCallingConfig"][
                "stream_function_call_arguments"
            ]
        )
        followup = ModelRequest(
            messages=(
                *REQUEST.messages,
                response,
                ModelMessage(
                    role="tool",
                    parts=tuple(
                        ToolReply(call_key=c.call_key, name=c.name, result={"ok": True})
                        for c in calls
                    ),
                ),
            )
        )
        _, second = await self.collect(Body([chunk([{"text": "DONE"}], "STOP")]), request=followup)
        payload = json.loads(second.requests[0].content)
        assistant = payload["contents"][1]
        self.assertEqual(assistant["parts"][1]["thoughtSignature"], signature)
        self.assertNotIn("id", assistant["parts"][1]["functionCall"])
        self.assertNotIn("id", payload["contents"][2]["parts"][0]["functionResponse"])
        self.assertEqual(payload["contents"][2]["parts"][1]["functionResponse"]["id"], "provider-2")

    async def test_missing_usage_remains_unknown(self) -> None:
        events, _ = await self.collect(Body([chunk([{"text": "A"}], "STOP")]))
        self.assertEqual([e for e in events if isinstance(e, Usage)], [Usage()])

    async def test_partial_cumulative_usage_preserves_known_fields_without_adding(self) -> None:
        events, _ = await self.collect(
            Body(
                [
                    {"usageMetadata": {"promptTokenCount": 9, "cachedContentTokenCount": 2}},
                    {"usageMetadata": {"candidatesTokenCount": 3}},
                    chunk([], "STOP"),
                    {"usageMetadata": {"candidatesTokenCount": 5}},
                ]
            )
        )
        self.assertEqual(
            [e for e in events if isinstance(e, Usage)][-1],
            Usage(input_tokens=9, output_tokens=5, cached_input_tokens=2),
        )

    async def test_unspecified_prompt_feedback_is_not_a_content_block(self) -> None:
        for reason, expected in [
            ("BLOCKED_REASON_UNSPECIFIED", Completed()),
            ("SAFETY", Completed(finish_reason="content_filter")),
        ]:
            steps: list[dict[str, object] | bytes | Exception | asyncio.Event] = [
                {"promptFeedback": {"blockReason": reason}}
            ]
            if reason == "BLOCKED_REASON_UNSPECIFIED":
                steps.append(chunk([{"text": "OK"}], "STOP"))
            events, _ = await self.collect(Body(steps))
            self.assertEqual(events[-1], expected)

    async def test_credentials_never_refresh_or_discover_auth(self) -> None:
        for expired, token in [(False, None), (True, "EXPIRED"), (False, "READY")]:
            credentials: Credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]
            credentials.token = token
            if expired:
                credentials.expiry = datetime(2000, 1, 1)
                # AnonymousCredentials.expired always returns False; use the base implementation.
                credentials = OAuthCredentials(token=token, expiry=credentials.expiry)  # type: ignore[no-untyped-call]
            transport = Transport(Body([chunk([], "STOP")]))

            def factory(transport: Transport = transport) -> httpx.AsyncBaseTransport:
                return transport

            provider = GoogleModelProvider(
                SETTINGS,
                credentials=credentials,
                clock=SystemClock(),
                transport_factory=factory,
            )
            with (
                patch("google.auth.default", side_effect=AssertionError("ADC invoked")),
                patch.object(credentials, "refresh", side_effect=AssertionError("Refresh invoked")),
                patch(
                    "google.auth.transport.requests.Request",
                    side_effect=AssertionError("Auth transport invoked"),
                ),
            ):
                events = await model_contract(provider, REQUEST, self.control)
            if token == "READY":
                self.assertEqual(events[-1], Completed())
                self.assertEqual(len(transport.requests), 1)
            else:
                self.assertEqual(events, (Failed(code="permission_denied"),))
                self.assertEqual(transport.requests, [])

    async def test_mtls_environment_cannot_change_destination(self) -> None:
        transport = Transport(Body([chunk([], "STOP")]))
        with patch.dict("os.environ", {"GOOGLE_API_USE_MTLS_ENDPOINT": "always"}):
            events = await model_contract(self.provider(transport), REQUEST, self.control)
        self.assertEqual(events, (Failed(code="invalid_arguments"),))
        self.assertEqual(transport.requests, [])

    async def test_malformed_schema_and_missing_local_ref_are_nonretryable(self) -> None:
        for schema in [
            {"type": "not-a-type"},
            {"$ref": "#/$defs/missing"},
            {"$id": "https://wrong.example/base", "$ref": "#missing"},
        ]:
            transport = Transport(Body([chunk([{"text": "{}"}], "STOP")]))
            events = await model_contract(
                self.provider(transport),
                REQUEST.model_copy(update={"response_schema": schema}),
                self.control,
            )
            self.assertEqual(events[-1], Failed(code="invalid_arguments"))

    async def test_system_instruction_tool_schema_error_reply_and_output_cap(self) -> None:
        call = ToolCall(call_key="call", name="fixture_tool", arguments_json='{"a":1}')
        request = ModelRequest(
            messages=(
                ModelMessage(role="system", parts=(TextPart(text="SYSTEM"),)),
                *REQUEST.messages,
                ModelMessage(role="assistant", parts=(call,)),
                ModelMessage(
                    role="tool",
                    parts=(
                        ToolReply(
                            call_key="call",
                            name="fixture_tool",
                            result={"code": "unavailable"},
                            is_error=True,
                        ),
                    ),
                ),
            ),
            tools=(
                ToolDefinition(
                    name="fixture_tool", description="DESCRIPTION", parameters={"type": "object"}
                ),
            ),
            max_output_tokens=7,
        )
        _, transport = await self.collect(Body([chunk([], "STOP")]), request=request)
        payload = json.loads(transport.requests[0].content)
        self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "SYSTEM"}])
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 7)
        self.assertEqual(
            payload["contents"][-1]["parts"][0]["functionResponse"]["response"],
            {"error": {"code": "unavailable"}},
        )
        self.assertEqual(payload["tools"][0]["functionDeclarations"][0]["name"], "fixture_tool")

    async def test_debug_task_logs_do_not_include_provider_exception_or_body(self) -> None:
        loop = asyncio.get_running_loop()
        previous = loop.slow_callback_duration
        loop.slow_callback_duration = 0
        try:
            with self.assertLogs(level=logging.DEBUG) as logs:
                events, _ = await self.collect(
                    Body(
                        [
                            chunk(
                                [
                                    {
                                        "functionCall": {
                                            "name": "tool",
                                            "args": "PRIVATE_PROVIDER_BODY",
                                        }
                                    }
                                ],
                                "STOP",
                            )
                        ]
                    )
                )
            self.assertEqual(events[-1], Failed(code="invalid_arguments"))
            self.assertNotIn("PRIVATE_PROVIDER_BODY", "\n".join(logs.output))
            self.assertNotIn("FIXTURE-TOKEN", "\n".join(logs.output))
        finally:
            loop.slow_callback_duration = previous

    async def test_provider_failures_are_safe_and_never_retry(self) -> None:
        for status, expected in [
            (429, Failed(code="rate_limited", retryable=True)),
            (503, Failed(code="unavailable", retryable=True)),
            (403, Failed(code="permission_denied")),
            (504, TimedOut()),
        ]:
            with self.subTest(status=status):
                body = Body(
                    [
                        json.dumps(
                            {"error": {"code": status, "message": "SECRET BODY TOKEN"}}
                        ).encode()
                    ]
                )
                with self.assertNoLogs("brainoid_model_google", level=logging.DEBUG):
                    events, _ = await self.collect(body, status=status)
                self.assertEqual(events, (expected,))
                self.assertNotIn("SECRET", repr(events))

    async def test_midstream_disconnect_timeout_and_eof_never_complete(self) -> None:
        scenarios: list[tuple[list[Exception], ModelEvent]] = [
            ([], Failed(code="stream_interrupted", retryable=True)),
            ([httpx.ReadError("SECRET")], Failed(code="unavailable", retryable=True)),
            ([httpx.ReadTimeout("SECRET")], TimedOut()),
        ]
        for tail, expected in scenarios:
            with self.subTest(expected=expected):
                events, _ = await self.collect(Body([chunk([{"text": "PARTIAL"}]), *tail]))
                self.assertEqual(events[0], TextDelta(text="PARTIAL"))
                self.assertEqual(events[-1], expected)
                self.assertFalse(any(isinstance(e, ModelResponse) for e in events))

    async def test_finish_then_disconnect_is_failure_and_trailing_usage_is_read(self) -> None:
        events, _ = await self.collect(Body([chunk([], "STOP"), httpx.ReadError("SECRET")]))
        self.assertIsInstance(events[-1], Failed)

    async def test_broken_and_split_tool_arguments_fail_without_candidate(self) -> None:
        cases: list[dict[str, object]] = [
            {"name": "fixture_tool", "args": "{broken"},
            {"name": "fixture_tool"},
            {"name": "fixture_tool", "args": {"x": 1}, "willContinue": True},
            {"name": "fixture_tool", "args": {}, "partialArgs": []},
        ]
        for call in cases:
            with self.subTest(call=call):
                events, _ = await self.collect(Body([chunk([{"functionCall": call}], "STOP")]))
                self.assertEqual(events[-1], Failed(code="invalid_arguments"))
                self.assertFalse(any(isinstance(e, ToolRequested) for e in events))

    async def test_duplicate_tool_ids_and_unknown_finish_fail(self) -> None:
        call: dict[str, object] = {"functionCall": {"id": "same", "name": "tool", "args": {}}}
        for body in [
            Body([chunk([call]), chunk([call], "STOP")]),
            Body([chunk([], "MALFORMED_FUNCTION_CALL")]),
            Body([b"data: {broken SECRET\n\n"]),
        ]:
            events, _ = await self.collect(body)
            self.assertEqual(events[-1], Failed(code="invalid_arguments"))

    async def test_length_continuation_and_filter_never_emit_tools_or_autocall(self) -> None:
        call: dict[str, object] = {"functionCall": {"name": "tool", "args": {}}}
        for reason, normalized in [
            ("MAX_TOKENS", "length"),
            ("CONTINUATION", "length"),
            ("SAFETY", "content_filter"),
        ]:
            with self.subTest(reason=reason):
                response = chunk([call], reason)
                if reason == "CONTINUATION":
                    response["candidates"] = [
                        {
                            "index": 0,
                            "content": {"role": "model", "parts": [call]},
                            "finishReason": reason,
                            "continuationToken": base64.b64encode(b"CONTINUE").decode(),
                        }
                    ]
                events, _ = await self.collect(Body([response]))
                self.assertEqual(events[-1].model_dump()["finish_reason"], normalized)
                self.assertFalse(any(isinstance(e, (ToolRequested, ModelResponse)) for e in events))

    async def test_unspecified_finish_is_intermediate_and_requires_later_finish(self) -> None:
        events, _ = await self.collect(
            Body(
                [
                    chunk([{"text": "A"}], "FINISH_REASON_UNSPECIFIED"),
                    chunk([{"text": "B"}], "STOP"),
                ]
            )
        )
        self.assertEqual(events[-1], Completed())
        events, _ = await self.collect(Body([chunk([], "FINISH_REASON_UNSPECIFIED")]))
        self.assertEqual(events[-1], Failed(code="stream_interrupted", retryable=True))

    async def test_structured_output_is_buffered_and_schema_checked(self) -> None:
        request = REQUEST.model_copy(
            update={
                "response_schema": {
                    "type": "object",
                    "required": ["n"],
                    "properties": {"n": {"type": "integer"}},
                }
            }
        )
        for text, valid in [(' {"n": 2}', True), ('{"n":"bad"}', False), ("{broken", False)]:
            with self.subTest(text=text):
                events, _ = await self.collect(
                    Body([chunk([{"text": text[:3]}]), chunk([{"text": text[3:]}], "STOP")]),
                    request=request,
                )
                if valid:
                    self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], [text])
                    self.assertEqual(events[-1], Completed())
                else:
                    self.assertEqual(events[-1], Failed(code="invalid_arguments"))
                    self.assertFalse(any(isinstance(e, TextDelta) for e in events))

    async def test_structured_output_rejects_nonfinite_numbers_at_every_depth(self) -> None:
        schemas: tuple[tuple[str, JsonObject], ...] = (
            ("%s", {"type": "number"}),
            ('{"n":%s}', {"type": "object", "properties": {"n": {"type": "number"}}}),
            ("[%s]", {"type": "array", "items": {"type": "number"}}),
            ('{"n":[{"nested":%s}]}', {"type": "object"}),
        )
        for number in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1e9999"):
            for template, schema in schemas:
                with self.subTest(number=number, template=template):
                    text = template % number
                    events, _ = await self.collect(
                        Body([chunk([{"text": text[:2]}]), chunk([{"text": text[2:]}], "STOP")]),
                        request=REQUEST.model_copy(update={"response_schema": schema}),
                    )
                    self.assertEqual(events, (Failed(code="invalid_arguments", retryable=False),))

    async def test_structured_output_preserves_finite_numbers_and_numeric_strings(self) -> None:
        for text in ("0", "-0.5", "1e308", "1e-308", '{"n":[1e308,-2.5,0,null,"NaN","Infinity"]}'):
            with self.subTest(text=text):
                events, _ = await self.collect(
                    Body([chunk([{"text": text}], "STOP")]),
                    request=REQUEST.model_copy(update={"response_schema": {}}),
                )
                self.assertEqual([e.text for e in events if isinstance(e, TextDelta)], [text])
                self.assertEqual(events[-1], Completed())
                self.assertTrue(any(isinstance(e, ModelResponse) for e in events))

    async def test_tool_history_rejects_nonfinite_json_before_dispatch(self) -> None:
        for number in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1e9999"):
            for template in ('{"n":%s}', '{"n":[%s]}', '{"n":[{"nested":%s}]}'):
                with self.subTest(number=number, template=template):
                    call = ToolCall(call_key="call", name="tool", arguments_json=template % number)
                    request = ModelRequest(
                        messages=(*REQUEST.messages, ModelMessage(role="assistant", parts=(call,)))
                    )
                    transport = Transport(Body([chunk([], "STOP")]))
                    events = await model_contract(self.provider(transport), request, self.control)
                    self.assertEqual(events, (Failed(code="invalid_arguments", retryable=False),))
                    self.assertEqual(transport.requests, [])
        call = ToolCall(
            call_key="call", name="tool", arguments_json='{"n":[1e308,-2.5,0,null,"NaN"]}'
        )
        request = ModelRequest(
            messages=(*REQUEST.messages, ModelMessage(role="assistant", parts=(call,)))
        )
        events, transport = await self.collect(Body([chunk([], "STOP")]), request=request)
        self.assertEqual(events[-1], Completed())
        payload = json.loads(transport.requests[0].content)
        self.assertEqual(
            payload["contents"][-1]["parts"][0]["functionCall"]["args"]["n"],
            [1e308, -2.5, 0, None, "NaN"],
        )

    async def test_mutated_typed_json_inputs_reject_nonfinite_before_serialization(self) -> None:
        # Core rejects these floats at construction, but its nested JSON containers
        # remain mutable. Check every input route after a legitimate finite value changes.
        for number in (float("nan"), float("inf"), float("-inf")):
            for route in ("reply", "tool_schema", "response_schema", "provider_state"):
                with self.subTest(number=number, route=route):
                    nested: JsonObject = {"n": 0}
                    call = ToolCall(call_key="call", name="tool", arguments_json='{"n":0}')
                    if route == "reply":
                        reply = ToolReply(call_key="call", name="tool", result={"nested": [nested]})
                        request = ModelRequest(
                            messages=(
                                *REQUEST.messages,
                                ModelMessage(role="assistant", parts=(call,)),
                                ModelMessage(role="tool", parts=(reply,)),
                            )
                        )
                        # Use the validated object's owned container, not the construction input.
                        nested = cast(JsonObject, cast(list[JsonValue], reply.result["nested"])[0])
                    elif route == "tool_schema":
                        tool = ToolDefinition(
                            name="tool",
                            description="",
                            parameters={"type": "object", "default": {"nested": [nested]}},
                        )
                        request = REQUEST.model_copy(update={"tools": (tool,)})
                        nested = cast(
                            JsonObject,
                            cast(
                                list[JsonValue],
                                cast(JsonObject, tool.parameters["default"])["nested"],
                            )[0],
                        )
                    elif route == "response_schema":
                        request = ModelRequest(
                            messages=REQUEST.messages,
                            response_schema={"type": "object", "default": {"nested": [nested]}},
                        )
                        assert request.response_schema is not None
                        nested = cast(
                            JsonObject,
                            cast(
                                list[JsonValue],
                                cast(JsonObject, request.response_schema["default"])["nested"],
                            )[0],
                        )
                    else:
                        message = ModelMessage(
                            role="assistant",
                            parts=(call,),
                            provider_state={
                                "binding": {
                                    "provider": "google-genai-v1",
                                    "project": SETTINGS.project,
                                    "location": SETTINGS.location,
                                    "model": SETTINGS.model_id,
                                },
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "function_call": {
                                                "id": "call",
                                                "name": "tool",
                                                "args": nested,
                                            }
                                        }
                                    ],
                                },
                                "call_keys": ["call"],
                            },
                        )
                        request = ModelRequest(messages=(*REQUEST.messages, message))
                        assert message.provider_state is not None
                        content = cast(JsonObject, message.provider_state["content"])
                        part = cast(JsonObject, cast(list[JsonValue], content["parts"])[0])
                        nested = cast(JsonObject, cast(JsonObject, part["function_call"])["args"])
                    nested["n"] = number
                    transport = Transport(Body([chunk([], "STOP")]))
                    events = await model_contract(self.provider(transport), request, self.control)
                    self.assertEqual(events, (Failed(code="invalid_arguments", retryable=False),))
                    self.assertEqual(transport.requests, [])

    async def test_nonfinite_sdk_tool_arguments_cannot_become_null_or_candidates(self) -> None:
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                events, _ = await self.collect(
                    Body(
                        [
                            chunk(
                                [
                                    {
                                        "functionCall": {
                                            "name": "tool",
                                            "args": {"n": [{"nested": number}]},
                                        }
                                    }
                                ],
                                "STOP",
                            )
                        ]
                    )
                )
                self.assertEqual(events, (Failed(code="invalid_arguments", retryable=False),))

    async def test_mutated_json_types_fail_before_copy_or_warning_without_dispatch(self) -> None:
        secret = "FIXTURE_MUTATED_SECRET"

        class InvalidValue:
            copied = 0
            represented = 0

            def __deepcopy__(self, memo: dict[int, object]) -> object:
                self.copied += 1
                raise RuntimeError(secret)

            def __repr__(self) -> str:
                self.represented += 1
                return secret

        def request_with_target(route: str) -> tuple[ModelRequest, dict[object, object]]:
            call = ToolCall(call_key="call", name="tool", arguments_json="{}")
            if route == "reply":
                reply = ToolReply(call_key="call", name="tool", result={})
                request = ModelRequest(
                    messages=(
                        *REQUEST.messages,
                        ModelMessage(role="assistant", parts=(call,)),
                        ModelMessage(role="tool", parts=(reply,)),
                    )
                )
                return request, cast(dict[object, object], reply.result)
            if route == "tool_schema":
                tool = ToolDefinition(name="tool", description="", parameters={"type": "object"})
                request = ModelRequest(messages=REQUEST.messages, tools=(tool,))
                return request, cast(dict[object, object], tool.parameters)
            if route == "response_schema":
                request = ModelRequest(
                    messages=REQUEST.messages, response_schema={"type": "object"}
                )
                assert request.response_schema is not None
                return request, cast(dict[object, object], request.response_schema)
            message = ModelMessage(
                role="assistant",
                parts=(TextPart(text="OK"),),
                provider_state={
                    "binding": {
                        "provider": "google-genai-v1",
                        "project": SETTINGS.project,
                        "location": SETTINGS.location,
                        "model": SETTINGS.model_id,
                    },
                    "content": {"role": "model", "parts": [{"text": "OK"}]},
                    "call_keys": [],
                },
            )
            request = ModelRequest(messages=(*REQUEST.messages, message))
            assert message.provider_state is not None
            return request, cast(dict[object, object], message.provider_state)

        for route in ("reply", "tool_schema", "response_schema", "provider_state"):
            for kind in (
                "bytes",
                "nested_bytes",
                "object",
                "tuple",
                "integer_key",
                "bytes_key",
                "object_key",
            ):
                with self.subTest(route=route, kind=kind):
                    request, target = request_with_target(route)
                    invalid = InvalidValue()
                    if kind == "bytes":
                        target["x"] = secret.encode()
                    elif kind == "nested_bytes":
                        target["x"] = [{"nested": [secret.encode()]}]
                    elif kind == "object":
                        target["x"] = [{"nested": invalid}]
                    elif kind == "tuple":
                        target["x"] = (secret,)
                    elif kind == "integer_key":
                        target[1] = secret
                    elif kind == "bytes_key":
                        target[secret.encode()] = 1
                    else:
                        target[invalid] = 1
                    transport = Transport(Body([chunk([], "STOP")]))
                    stderr = io.StringIO()
                    with (
                        warnings.catch_warnings(record=True) as recorded,
                        redirect_stderr(stderr),
                        self.assertNoLogs(level=logging.DEBUG),
                    ):
                        warnings.simplefilter("always")
                        events = await model_contract(
                            self.provider(transport), request, self.control
                        )
                    self.assertEqual(events, (Failed(code="invalid_arguments", retryable=False),))
                    self.assertEqual(transport.requests, [])
                    self.assertEqual(recorded, [])
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(invalid.copied, 0)
                    self.assertEqual(invalid.represented, 0)

    async def test_external_schema_ref_and_state_from_other_model_reject_before_io(self) -> None:
        requests = [
            REQUEST.model_copy(
                update={"response_schema": {"$ref": "https://wrong.example/schema"}}
            ),
            ModelRequest(
                messages=(ModelMessage(role="assistant", parts=(), provider_state={"binding": {}}),)
            ),
        ]
        for request in requests:
            transport = Transport(Body([]))
            events = await model_contract(self.provider(transport), request, self.control)
            self.assertEqual(events, (Failed(code="invalid_arguments"),))
            self.assertEqual(transport.requests, [])

    async def test_cancel_before_dispatch_wins_over_deadline(self) -> None:
        transport = Transport(Body([]))
        self.token.cancel()
        events = await model_contract(self.provider(transport), REQUEST, CallControl(self.token, 0))
        self.assertEqual(events, (Cancelled(),))
        self.assertEqual(transport.requests, [])

    async def test_live_cancel_during_open_and_body_wait_closes_resources(self) -> None:
        for opening in [True, False]:
            with self.subTest(opening=opening):
                self.token = CancellationToken()
                control = CallControl(self.token, None)
                body = Body([asyncio.Event(), chunk([], "STOP")])
                transport = Transport(body, opening=asyncio.Event() if opening else None)
                task = asyncio.create_task(
                    model_contract(self.provider(transport), REQUEST, control)
                )
                await asyncio.wait_for(
                    transport.entered.wait() if opening else body.waiting.wait(), 2
                )
                self.token.cancel()
                self.assertEqual(await asyncio.wait_for(task, 2), (Cancelled(),))
                self.assertTrue(transport.closed)
                if not opening:
                    self.assertTrue(body.closed)

    async def test_injected_monotonic_deadline_equality_and_cancel_priority(self) -> None:
        for cancel in [False, True]:
            clock = ManualClock()
            body = Body([asyncio.Event()])
            transport = Transport(body)
            token = CancellationToken()
            task = asyncio.create_task(
                model_contract(
                    self.provider(transport, clock=clock), REQUEST, CallControl(token, 102.0)
                )
            )
            await asyncio.wait_for(body.waiting.wait(), 2)
            if cancel:
                token.cancel()
            clock.advance(2.0)
            self.assertEqual(
                await asyncio.wait_for(task, 2), (Cancelled() if cancel else TimedOut(),)
            )
            self.assertTrue(body.closed)

    async def test_config_timeout_without_caller_deadline(self) -> None:
        clock = ManualClock()
        body = Body([asyncio.Event()])
        task = asyncio.create_task(
            model_contract(self.provider(Transport(body), clock=clock), REQUEST, self.control)
        )
        await asyncio.wait_for(body.waiting.wait(), 2)
        clock.advance(5.0)
        self.assertEqual(await asyncio.wait_for(task, 2), (TimedOut(),))

    async def test_task_cancel_propagates_and_aclose_releases_response(self) -> None:
        body = Body([asyncio.Event()])
        transport = Transport(body)
        task = asyncio.create_task(model_contract(self.provider(transport), REQUEST, self.control))
        await asyncio.wait_for(body.waiting.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(body.closed)
        self.assertTrue(transport.closed)
        body = Body([chunk([{"text": "FIRST"}]), asyncio.Event()])
        transport = Transport(body)
        provider: ModelProvider = self.provider(transport)
        async with aclosing(provider.stream(REQUEST, control=self.control)) as stream:
            self.assertEqual(await anext(stream), TextDelta(text="FIRST"))
        self.assertTrue(body.closed)
        self.assertTrue(transport.closed)

    async def test_cancel_between_chunks_suppresses_remaining_output(self) -> None:
        transport = Transport(Body([chunk([{"text": "FIRST"}, {"text": "SECOND"}], "STOP")]))
        async with aclosing(
            self.provider(transport).stream(REQUEST, control=self.control)
        ) as stream:
            self.assertEqual(await anext(stream), TextDelta(text="FIRST"))
            self.token.cancel()
            self.assertEqual([event async for event in stream], [Cancelled()])
        self.assertTrue(transport.closed)

    def test_typed_settings_require_explicit_finite_bounded_config(self) -> None:
        for field, value in [
            ("provider", "developer"),
            ("location", ""),
            ("project", "https://wrong"),
            ("model_id", "projects/elsewhere/model"),
            ("timeout_seconds", float("inf")),
            ("timeout_seconds", 0),
            ("max_output_tokens", True),
        ]:
            with self.subTest(field=field):
                values = SETTINGS.model_dump()
                values[field] = value
                with self.assertRaises(ValidationError):
                    GoogleModelSettings.model_validate(values)
        with self.assertRaises(ValidationError):
            GoogleModelSettings.model_validate({})


if __name__ == "__main__":
    unittest.main()
