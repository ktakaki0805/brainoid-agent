"""Provider-free contract tests, exercising only the public structural port signatures."""

import asyncio
import subprocess
import sys
import tempfile
import unittest
from contextlib import aclosing
from datetime import timedelta
from unittest.mock import patch

from brainoid_agent_core import Clock, MemoryStore, ModelProvider, SpeechProvider, ToolExecutor
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    AudioChunk,
    Cancelled,
    Completed,
    Failed,
    MemoryContent,
    MemoryForget,
    MemoryForgetAccepted,
    MemoryMatches,
    MemoryRemember,
    MemoryRevise,
    MemorySaved,
    MemorySearch,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    SpeechRecognition,
    SpeechSynthesis,
    TextDelta,
    TimedOut,
    ToolDefinition,
    ToolExecution,
    ToolReply,
    ToolRequested,
    ToolResult,
    Transcript,
    Usage,
)
from fakes.memory_speech_tools import FakeMemoryStore, FakeSpeechProvider, FakeToolExecutor
from fakes.model import FakeModelProvider
from fakes.runtime import CancellationToken, FakeClock, Step
from fakes.scenarios import (
    AUDIO_FORMAT,
    CONTEXT,
    FIXED_TIME,
    FORBIDDEN_TOOL_CALL,
    MALFORMED_TOOL_CALL,
    MEMORY_CONTENT,
    MEMORY_ID,
    MEMORY_RECORD,
    MODEL_REQUEST,
    OPERATION_ID,
    OUT_OF_SCOPE_CANDIDATES,
    OUTCOME_UNKNOWN,
    SHARED_SCOPE,
    TENANT_A,
    TOOL_CALL,
    TOOL_CALL_ID,
    TOOL_EXECUTION,
)
from pydantic import TypeAdapter, ValidationError


async def model_contract(
    provider: ModelProvider, request: ModelRequest, control: CallControl
) -> tuple[ModelEvent, ...]:
    """Reusable consumer: assertions require no fake class, SDK, or fixture history API."""
    async with aclosing(provider.stream(request, control=control)) as stream:
        events = tuple([event async for event in stream])
    terminals = (Completed, Cancelled, TimedOut, Failed)
    assert events and isinstance(events[-1], terminals)
    assert not any(isinstance(event, terminals) for event in events[:-1])
    for event in events:
        assert TypeAdapter(ModelEvent).validate_json(event.model_dump_json()) == event
    return events


class ModelContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.token = CancellationToken()
        self.control = CallControl(self.token, deadline=None)

    async def test_interchangeable_scenarios_complete_and_request_tools(self) -> None:
        scenarios: tuple[tuple[ModelEvent, ...], ...] = (
            (TextDelta(text="FIXTURE_HELLO"), Usage(input_tokens=4, output_tokens=2), Completed()),
            (ToolRequested(call=TOOL_CALL), Completed(finish_reason="tool_calls")),
        )
        for expected in scenarios:
            with self.subTest(expected=expected):
                provider: ModelProvider = FakeModelProvider(
                    self.clock, (tuple(Step(event) for event in expected),)
                )
                self.assertEqual(
                    await model_contract(provider, MODEL_REQUEST, self.control), expected
                )

    async def test_invalid_and_forbidden_tool_candidates_remain_untrusted(self) -> None:
        expected: tuple[ModelEvent, ...] = (
            ToolRequested(call=MALFORMED_TOOL_CALL),
            ToolRequested(call=FORBIDDEN_TOOL_CALL),
            Completed(finish_reason="tool_calls"),
        )
        provider: ModelProvider = FakeModelProvider(
            self.clock, (tuple(Step(event) for event in expected),)
        )
        self.assertEqual(await model_contract(provider, MODEL_REQUEST, self.control), expected)

    async def test_cancel_before_dispatch_and_between_chunks(self) -> None:
        fake = FakeModelProvider(
            self.clock,
            (
                (Step(TextDelta(text="FIXTURE_NEVER"), 3), Step(Completed())),
                (
                    Step(TextDelta(text="FIXTURE_FIRST")),
                    Step(TextDelta(text="FIXTURE_NEVER"), 3),
                    Step(Completed()),
                ),
            ),
        )
        self.token.cancel()
        self.assertEqual(await model_contract(fake, MODEL_REQUEST, self.control), (Cancelled(),))
        self.assertEqual(self.clock.monotonic(), 0)
        token = CancellationToken()
        async with aclosing(fake.stream(MODEL_REQUEST, control=CallControl(token, None))) as stream:
            self.assertEqual(await anext(stream), TextDelta(text="FIXTURE_FIRST"))
            token.cancel()
            self.assertEqual([event async for event in stream], [Cancelled()])
        self.assertEqual(self.clock.sleeps, [])
        self.assertTrue(all(call.closed for call in fake.calls.history))

    async def test_deadline_caps_delay_and_equality_times_out(self) -> None:
        fake = FakeModelProvider(
            self.clock,
            (
                (
                    Step(TextDelta(text="FIXTURE_FIRST"), 1),
                    Step(TextDelta(text="FIXTURE_LATE"), 10),
                    Step(Completed()),
                ),
            ),
        )
        events = await model_contract(fake, MODEL_REQUEST, CallControl(self.token, 3))
        self.assertEqual(events, (TextDelta(text="FIXTURE_FIRST"), TimedOut()))
        self.assertEqual(self.clock.sleeps, [1, 2])
        self.assertEqual(self.clock.now(), FIXED_TIME + timedelta(seconds=3))
        self.assertEqual(fake.calls.history[0].finished_at, 3)
        immediate = FakeModelProvider(self.clock, ((Step(Completed()),),))
        self.assertEqual(
            await model_contract(immediate, MODEL_REQUEST, CallControl(self.token, 3)),
            (TimedOut(),),
        )

    async def test_cancel_observed_during_delay_suppresses_the_pending_chunk(self) -> None:
        token = self.token

        class CancellingClock(FakeClock):
            async def sleep(self, seconds: float) -> None:
                await super().sleep(seconds)
                token.cancel()

        fake = FakeModelProvider(
            CancellingClock(), ((Step(TextDelta(text="FIXTURE_NEVER"), 2), Step(Completed())),)
        )
        self.assertEqual(await model_contract(fake, MODEL_REQUEST, self.control), (Cancelled(),))

    async def test_midstream_failure_and_truncation_never_become_success(self) -> None:
        for tail in [(Step(Failed(code="unavailable", retryable=True)),), ()]:
            fake = FakeModelProvider(
                self.clock, ((Step(TextDelta(text="FIXTURE_PARTIAL")), *tail),)
            )
            events = await model_contract(fake, MODEL_REQUEST, self.control)
            self.assertIsInstance(events[-1], Failed)
            self.assertEqual(events[0], TextDelta(text="FIXTURE_PARTIAL"))
            self.assertTrue(fake.calls.history[0].closed)

    async def test_closing_stream_releases_resources_without_synthesizing_completion(self) -> None:
        fake = FakeModelProvider(
            self.clock, ((Step(TextDelta(text="FIXTURE_FIRST")), Step(Completed(), 10)),)
        )
        async with aclosing(fake.stream(MODEL_REQUEST, control=self.control)) as stream:
            await anext(stream)
        self.assertTrue(fake.calls.history[0].closed)
        self.assertEqual(fake.calls.history[0].events, [TextDelta(text="FIXTURE_FIRST")])
        self.assertEqual(self.clock.monotonic(), 0)

    async def test_history_and_failures_repeat_without_random_or_wall_clock(self) -> None:
        histories = []
        for _ in range(2):
            clock = FakeClock()
            fake = FakeModelProvider(
                clock,
                (
                    (
                        Step(TextDelta(text="FIXTURE"), 2),
                        Step(Failed(code="rate_limited", retryable=True)),
                    ),
                ),
            )
            with (
                patch("time.time", side_effect=AssertionError("wall clock used")),
                patch("random.random", side_effect=AssertionError("random used")),
                patch("uuid.uuid4", side_effect=AssertionError("random ID used")),
            ):
                await model_contract(fake, MODEL_REQUEST, self.control)
            histories.append(fake.calls.history)
        self.assertEqual(histories[0], histories[1])

    async def test_task_cancellation_propagates_and_closes_the_stream(self) -> None:
        class InterruptedClock(FakeClock):
            async def sleep(self, seconds: float) -> None:
                raise asyncio.CancelledError

        fake = FakeModelProvider(InterruptedClock(), ((Step(Completed(), 1),),))
        with self.assertRaises(asyncio.CancelledError):
            await model_contract(fake, MODEL_REQUEST, self.control)
        self.assertTrue(fake.calls.history[0].closed)
        self.assertEqual(fake.calls.history[0].events, [])


class OtherPortContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.control = CallControl(CancellationToken(), None)

    async def test_memory_reads_writes_revision_and_forget_keep_scope_sources_and_versions(
        self,
    ) -> None:
        revised = MEMORY_RECORD.model_copy(update={"version": 2})
        fake = FakeMemoryStore(
            self.clock,
            searches=((Step(MemoryMatches(records=(MEMORY_RECORD,))),),),
            remembers=((Step(MemorySaved(record=MEMORY_RECORD)),),),
            revisions=((Step(MemorySaved(record=revised)),),),
            forgets=((Step(MemoryForgetAccepted(operation_id=OPERATION_ID)),),),
        )
        store: MemoryStore = fake
        search = MemorySearch(context=CONTEXT, scopes=(SHARED_SCOPE,), query="FIXTURE")
        matches = await store.search(search, control=self.control)
        self.assertEqual(matches, MemoryMatches(records=(MEMORY_RECORD,)))
        self.assertEqual(fake.searches.history[0].request.context.tenant_id, TENANT_A)
        self.assertEqual(fake.searches.history[0].request.scopes, (SHARED_SCOPE,))
        remembered = await store.remember(
            MemoryRemember(context=CONTEXT, scopes=(SHARED_SCOPE,), content=MEMORY_CONTENT),
            control=self.control,
        )
        self.assertEqual(remembered, MemorySaved(record=MEMORY_RECORD))
        self.assertEqual(
            await store.revise(
                MemoryRevise(
                    context=CONTEXT,
                    scopes=(SHARED_SCOPE,),
                    memory_id=MEMORY_ID,
                    expected_version=1,
                    replacement=MEMORY_CONTENT,
                ),
                control=self.control,
            ),
            MemorySaved(record=revised),
        )
        self.assertEqual(
            await store.forget(
                MemoryForget(
                    context=CONTEXT, scopes=(SHARED_SCOPE,), memory_id=MEMORY_ID, expected_version=2
                ),
                control=self.control,
            ),
            MemoryForgetAccepted(operation_id=OPERATION_ID),
        )
        self.assertTrue(fake.forgets.history[0].closed)

    async def test_out_of_scope_candidate_is_explicit_fault_fixture_not_an_authorization_check(
        self,
    ) -> None:
        store: MemoryStore = FakeMemoryStore(
            self.clock, searches=((Step(OUT_OF_SCOPE_CANDIDATES),),)
        )
        result = await store.search(
            MemorySearch(context=CONTEXT, scopes=(SHARED_SCOPE,), query="FIXTURE"),
            control=self.control,
        )
        self.assertEqual(result, OUT_OF_SCOPE_CANDIDATES)
        assert isinstance(result, MemoryMatches)
        self.assertNotEqual(result.records[1].tenant_id, CONTEXT.tenant_id)
        self.assertEqual(result.records[1].content.value, "FIXTURE_FORBIDDEN_TENANT_B_PRIVATE")

    async def test_tool_success_explicit_failure_confirmation_and_unknown_are_distinct(
        self,
    ) -> None:
        outcomes = (
            ToolResult(
                tool_call_id=TOOL_CALL_ID,
                status="succeeded",
                result={"value": "FIXTURE"},
                operation_id=OPERATION_ID,
            ),
            Failed(code="invalid_arguments"),
            Failed(code="permission_denied"),
            ToolResult(
                tool_call_id=TOOL_CALL_ID,
                status="waiting_confirmation",
                result={},
                proposal_id=OPERATION_ID,
            ),
            OUTCOME_UNKNOWN,
        )
        fake = FakeToolExecutor(self.clock, tuple((Step(outcome),) for outcome in outcomes))
        executor: ToolExecutor = fake
        for expected in outcomes:
            self.assertEqual(await executor.execute(TOOL_EXECUTION, control=self.control), expected)
        self.assertEqual(len(fake.calls.history), len(outcomes))
        self.assertTrue(
            all(call.request == TOOL_EXECUTION and call.closed for call in fake.calls.history)
        )

    async def test_unary_timeout_and_cancel_share_model_termination_values(self) -> None:
        fake = FakeToolExecutor(self.clock, ((Step(OUTCOME_UNKNOWN, 4),), (Step(OUTCOME_UNKNOWN),)))
        executor: ToolExecutor = fake
        self.assertEqual(
            await executor.execute(TOOL_EXECUTION, control=CallControl(CancellationToken(), 2)),
            TimedOut(),
        )
        token = CancellationToken()
        token.cancel()
        self.assertEqual(
            await executor.execute(TOOL_EXECUTION, control=CallControl(token, 2)), Cancelled()
        )
        self.assertEqual(self.clock.monotonic(), 2)
        store: MemoryStore = FakeMemoryStore(
            self.clock, searches=((Step(MemoryMatches(records=()), 3),),)
        )
        self.assertEqual(
            await store.search(
                MemorySearch(context=CONTEXT, scopes=(SHARED_SCOPE,), query="FIXTURE"),
                control=CallControl(CancellationToken(), 2),
            ),
            TimedOut(),
        )

    async def test_speech_success_silence_failure_and_synthesis_stop(self) -> None:
        recognition = SpeechRecognition(audio=b"FIXTURE_PCM", format=AUDIO_FORMAT, locale="ja-JP")
        synthesis = SpeechSynthesis(
            text="FIXTURE", format=AUDIO_FORMAT, locale="ja-JP", voice="fixture"
        )
        fake = FakeSpeechProvider(
            self.clock,
            recognition=(
                (Step(Transcript(text="FIXTURE_TEXT", is_final=True)), Step(Completed())),
                (Step(Transcript(text="", is_final=True)), Step(Completed())),
                (Step(Failed(code="permission_denied")),),
            ),
            synthesis=(
                (
                    Step(AudioChunk(audio=b"FIXTURE_1", format=AUDIO_FORMAT)),
                    Step(AudioChunk(audio=b"FIXTURE_2", format=AUDIO_FORMAT)),
                    Step(Completed()),
                ),
                (
                    Step(AudioChunk(audio=b"FIXTURE_LATE", format=AUDIO_FORMAT), 5),
                    Step(Completed()),
                ),
            ),
        )
        speech: SpeechProvider = fake
        for expected in [
            [Transcript(text="FIXTURE_TEXT", is_final=True), Completed()],
            [Transcript(text="", is_final=True), Completed()],
            [Failed(code="permission_denied")],
        ]:
            self.assertEqual(
                [event async for event in speech.recognize(recognition, control=self.control)],
                expected,
            )
        token = CancellationToken()
        async with aclosing(
            speech.synthesize(synthesis, control=CallControl(token, None))
        ) as stream:
            self.assertEqual(
                await anext(stream), AudioChunk(audio=b"FIXTURE_1", format=AUDIO_FORMAT)
            )
            token.cancel()
            self.assertEqual([event async for event in stream], [Cancelled()])
        self.assertEqual(
            [
                event
                async for event in speech.synthesize(
                    synthesis, control=CallControl(CancellationToken(), 2)
                )
            ],
            [TimedOut()],
        )
        self.assertTrue(all(call.closed for call in fake.synthesis.history))

    async def test_snapshots_prevent_request_response_and_script_mutation_of_history(self) -> None:
        declaration = ToolDefinition(
            name="fixture.tool", description="fixture", parameters={"nested": {"value": 1}}
        )
        request = ModelRequest(messages=MODEL_REQUEST.messages, tools=(declaration,))
        fake = FakeModelProvider(self.clock, ((Step(Completed()),),))
        await model_contract(fake, request, self.control)
        declaration.parameters["nested"] = {"value": 2}
        self.assertEqual(
            fake.calls.history[0].request.tools[0].parameters, {"nested": {"value": 1}}
        )
        result = ToolResult(tool_call_id=TOOL_CALL_ID, status="succeeded", result={"nested": [1]})
        tool = FakeToolExecutor(self.clock, ((Step(result),),))
        result.result["nested"] = [2]
        emitted = await tool.execute(TOOL_EXECUTION, control=self.control)
        assert isinstance(emitted, ToolResult)
        self.assertEqual(emitted.result, {"nested": [1]})
        emitted.result["nested"] = [3]
        saved = tool.calls.history[0].events[0]
        assert isinstance(saved, ToolResult)
        self.assertEqual(saved.result, {"nested": [1]})


class CoreBoundaryTests(unittest.TestCase):
    def test_import_rejects_frameworks_sdks_and_device_modules_even_if_installed(self) -> None:
        source = """
import importlib.abc
import sys
class BlockExternal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'fastapi', 'firebase_admin', 'google', 'RPi', 'gpiozero', 'brainoid_api', 'brainoid_device'}:
            raise AssertionError('core imported a forbidden dependency: ' + fullname)
sys.meta_path.insert(0, BlockExternal())
from brainoid_agent_core import Clock, MemoryStore, ModelProvider, SpeechProvider, ToolExecutor
from brainoid_agent_core.types import ModelRequest, MemorySearch, ToolExecution, SpeechSynthesis
assert all(item for item in (Clock, MemoryStore, ModelProvider, SpeechProvider, ToolExecutor))
"""
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run([sys.executable, "-I", "-c", source], cwd=temporary, check=True)

    def test_boundary_values_validate_json_without_sdk_types_or_trusted_context_in_model(
        self,
    ) -> None:
        message = ModelMessage(
            role="tool",
            parts=(
                ToolReply(
                    call_key=TOOL_CALL.call_key,
                    name=TOOL_CALL.name,
                    result={"value": [1, True, None]},
                ),
            ),
        )
        request = ModelRequest(
            messages=(*MODEL_REQUEST.messages, message), response_schema={"type": "object"}
        )
        self.assertEqual(ModelRequest.model_validate_json(request.model_dump_json()), request)
        self.assertNotIn("context", ModelRequest.model_fields)
        self.assertEqual(
            ToolExecution.model_validate_json(TOOL_EXECUTION.model_dump_json()), TOOL_EXECUTION
        )
        for value in [
            {"messages": [], "context": CONTEXT.model_dump()},
            {"messages": [], "response_schema": {"sdk_object": object()}},
            {"messages": [], "max_output_tokens": True},
        ]:
            with self.assertRaises(ValidationError):
                ModelRequest.model_validate(value)
        for value in [-1, True, 1.5]:
            with self.assertRaises(ValidationError):
                Usage.model_validate({"input_tokens": value})

    def test_control_and_clock_reject_nonfinite_time_and_invalid_delays(self) -> None:
        clock: Clock = FakeClock()
        self.assertEqual(clock.now(), FIXED_TIME)
        for value in [float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError):
                CallControl(CancellationToken(), value)
        for value in [-1, float("inf"), float("nan")]:
            with self.assertRaises(ValueError):
                Step(Completed(), value)
            with self.assertRaises(ValueError):
                FakeClock().advance(value)

    def test_memory_shape_and_durable_effect_correlation_are_required(self) -> None:
        content = MEMORY_CONTENT.model_dump()
        changes: list[dict[str, object]] = [
            {"value": None},
            {"object_entity_id": MEMORY_ID},
            {"valid_to": FIXED_TIME},
        ]
        for change in changes:
            with self.assertRaises(ValidationError):
                MemoryContent.model_validate({**content, **change})
        for missing in ["context", "scopes"]:
            value = {"context": CONTEXT, "scopes": [SHARED_SCOPE], "query": "FIXTURE"}
            del value[missing]
            with self.assertRaises(ValidationError):
                MemorySearch.model_validate(value)
        for status in ["waiting_confirmation", "outcome_unknown"]:
            with self.assertRaises(ValidationError):
                ToolResult.model_validate(
                    {"tool_call_id": TOOL_CALL_ID, "status": status, "result": {}}
                )


if __name__ == "__main__":
    unittest.main()
