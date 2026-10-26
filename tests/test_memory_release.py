"""MEM-04 async consumer deadlines and minimal model projections."""

import copy
import json
from contextlib import AbstractContextManager
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.core import TextAgentCore
from brainoid_agent_core.types import ModelRequest, ToolCall
from brainoid_contracts import ResourceId
from brainoid_worker.metered_model import MeteredModel
from brainoid_worker.run_turn import ReadBridge
from test_memory_revalidation import MatchedClock
from test_scoped_retrieval import MemoryWorkerFixture


class MemoryReleaseTests(MemoryWorkerFixture):
    def exercise(self, kind: str, boundary: str) -> None:
        self.handler.clock = MatchedClock(self.clock.now)
        old = self.memory(
            value="EXPIRED_DEPENDENCY", valid_to=self.clock.now() + timedelta(seconds=1)
        )
        conversation = self.conversation()
        stage = 0
        if kind == "history":
            self.model.text = "EXPIRED_DEPENDENCY"
            first = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
            self.execute(first)
            self.assertEqual(self.get_run(first).json()["status"], "completed")
            text = "zzz_no_match"
        elif kind == "tool":
            self.configure_calls(
                (
                    ToolCall(
                        call_key="search", name="memory.search", arguments_json='{"query":"Alex"}'
                    ),
                )
            )
            stage, text = 1, "zzz_no_match"
        else:
            self.configure_calls(
                (
                    ToolCall(
                        call_key="revise",
                        name="memory.revise",
                        arguments_json=json.dumps(
                            {
                                "memory_id": old.id,
                                "expected_version": 1,
                                "replacement": {**self.body, "value": "coffee"},
                            }
                        ),
                    ),
                )
            )
            stage, text = 1, "Correct the memory: Alex preference is coffee"
        self.handler.clock = MatchedClock(self.clock.now)
        run = ResourceId(
            self.turn(conversation, key="async-boundary-second", text=text).json()["run_id"]
        )
        run_path = f"tenants/{self.tenant}/runs/{run}"
        hits: list[str] = []
        reads, wait = ReadBridge.read, TextAgentCore._wait

        def selected() -> bool:
            return bool(self.store.data[run_path].get("model_step", 0) == stage)

        async def reader(bridge: ReadBridge, work: Any) -> Any:
            result = await reads(bridge, work)
            if selected() and work.__qualname__ == "Authorizer.check.<locals>.<lambda>":
                hits.append(boundary)
                if len(hits) == 2:
                    self.clock.advance(2)
            return result

        async def core_wait(core: TextAgentCore, work: Any, control: Any) -> Any:
            name = getattr(getattr(work, "cr_code", None), "co_qualname", "")
            if (
                boundary == "provider_task"
                and selected()
                and name.endswith("generate.<locals>.advance")
                and not hits
            ):

                async def delayed() -> Any:
                    hits.append(boundary)
                    self.clock.advance(2)
                    return await work

                return await wait(core, delayed(), control)
            result = await wait(core, work, control)
            if boundary == "core_cleanup" and selected() and name == "Authorizer.check":
                hits.append(boundary)
                if len(hits) == 2:
                    self.clock.advance(2)
            return result

        meters: list[MeteredModel] = []

        def metered(*args: Any, **kwargs: Any) -> MeteredModel:
            value = MeteredModel(*args, **kwargs)
            meters.append(value)
            return value

        hook: AbstractContextManager[Any] = (
            patch.object(ReadBridge, "read", reader)
            if boundary == "reader"
            else patch.object(TextAgentCore, "_wait", core_wait)
        )
        if boundary == "meter_serialization":
            serialize = ModelRequest.model_dump_json

            def delayed_serialization(request: ModelRequest, *args: Any, **kwargs: Any) -> str:
                value = serialize(request, *args, **kwargs)
                if selected() and not hits:
                    hits.append(boundary)
                    self.clock.advance(2)
                return value

            hook = patch.object(ModelRequest, "model_dump_json", delayed_serialization)
        with hook, patch("brainoid_worker.run_turn.MeteredModel", side_effect=metered):
            self.execute(run)
        self.assertEqual(
            len(hits), 1 if boundary in ("provider_task", "meter_serialization") else 2
        )
        self.assertEqual(len(self.model.requests), 1)
        self.assertFalse(meters[-1].started)
        self.assertEqual(meters[-1].observed.input_tokens, 0)
        self.assertEqual(meters[-1].observed.output_tokens, 0)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertTrue(
            all(d["active"] == 0 for p, d in self.store.data.items() if "/usage_slots/" in p)
        )
        if kind == "tool":
            self.assertEqual(self.store.data[run_path]["memory_records"], [])
            self.assertEqual(len(self.store.data[run_path]["tool_bindings"]), 1)
        if kind == "revision":
            self.assertEqual(
                self.store.data[f"tenants/{self.tenant}/facts/{old.id}"]["status"], "superseded"
            )

    def test_history_reader_handoff(self) -> None:
        self.exercise("history", "reader")

    def test_history_core_cleanup_handoff(self) -> None:
        self.exercise("history", "core_cleanup")

    def test_history_provider_task_handoff(self) -> None:
        self.exercise("history", "provider_task")

    def test_tool_reader_handoff(self) -> None:
        self.exercise("tool", "reader")

    def test_tool_core_cleanup_handoff(self) -> None:
        self.exercise("tool", "core_cleanup")

    def test_tool_provider_task_handoff(self) -> None:
        self.exercise("tool", "provider_task")

    def test_revision_reader_handoff(self) -> None:
        self.exercise("revision", "reader")

    def test_revision_core_cleanup_handoff(self) -> None:
        self.exercise("revision", "core_cleanup")

    def test_revision_provider_task_handoff(self) -> None:
        self.exercise("revision", "provider_task")

    def test_history_final_provider_boundary_after_metering(self) -> None:
        self.exercise("history", "meter_serialization")

    def test_tool_final_provider_boundary_after_metering(self) -> None:
        self.exercise("tool", "meter_serialization")

    def test_revision_final_provider_boundary_after_metering(self) -> None:
        self.exercise("revision", "meter_serialization")


class MemoryProjectionTests(MemoryWorkerFixture):
    def exercise(self, kind: str) -> tuple[ResourceId, ResourceId]:
        self.body.update(visibility="private", display_policy="never")
        old = self.memory()
        if kind != "automatic":
            args: dict[str, Any]
            if kind == "search":
                args = {"query": "Alex"}
            elif kind == "remember":
                args = dict(self.body)
            else:
                args = {
                    "memory_id": old.id,
                    "expected_version": 1,
                    "replacement": {**self.body, "value": "coffee"},
                }
            self.configure_calls(
                (ToolCall(call_key=kind, name="memory." + kind, arguments_json=json.dumps(args)),)
            )
        conversation = self.conversation(private=True)
        text = {
            "automatic": "Alex",
            "search": "zzz_no_match",
            "remember": "Remember that Alex prefers tea",
            "revise": "Correct the memory: Alex preference is coffee",
        }[kind]
        run = ResourceId(self.turn(conversation, text=text).json()["run_id"])
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        if kind != "automatic":
            stored = self.store.data[f"tenants/{self.tenant}/runs/{run}"]["tool_bindings"][0][
                "result"
            ]["result"]["memories"][0]
            self.assertEqual(stored["tenant_id"], self.tenant)
            self.assertEqual(stored["owner_user_id"], self.uid)
            self.assertEqual(stored["created_by"], self.uid)
            self.model.calls = ()  # type: ignore[attr-defined]
        second = ResourceId(
            self.turn(conversation, key="projection-followup", text="zzz_no_match").json()["run_id"]
        )
        self.execute(second)
        self.assertEqual(self.get_run(second).json()["status"], "completed")
        all_inputs = " ".join(r.model_dump_json() for r in self.model.requests)
        for identity in (str(self.uid), str(self.tenant)):
            self.assertNotIn(identity, all_inputs)
        for internal in ("created_by", "owner_user_id", "tenant_id"):
            self.assertNotIn(internal, all_inputs)
        for provenance in ("memory_id", "recorded_at", "source_refs", "version", "user_explicit"):
            self.assertIn(provenance, all_inputs)
        return conversation, run

    def test_automatic_context_and_history_omit_internal_identity(self) -> None:
        self.exercise("automatic")

    def test_search_reply_and_history_omit_internal_identity(self) -> None:
        self.exercise("search")

    def test_remember_reply_and_history_omit_internal_identity(self) -> None:
        self.exercise("remember")

    def test_revise_reply_and_history_omit_internal_identity(self) -> None:
        self.exercise("revise")

    def test_modified_projected_reply_cannot_bypass_exact_result_binding(self) -> None:
        conversation, run = self.exercise("search")
        source = self.store.data[f"tenants/{self.tenant}/runs/{run}"]["final_message_id"]
        path = f"tenants/{self.tenant}/conversations/{conversation}/messages/{source}"
        snapshots = copy.deepcopy(self.store.data[path]["model_messages"])
        reply = next(p for m in snapshots for p in m["parts"] if p["type"] == "tool_reply")
        reply["result"]["result"]["memories"][0]["content"]["value"] = "TAMPERED_REPLY"
        self.store.data[path]["model_messages"] = snapshots
        next_run = ResourceId(
            self.turn(conversation, key="projection-tampered", text="zzz_no_match").json()["run_id"]
        )
        before = len(self.model.requests)
        self.execute(next_run)
        self.assertEqual(len(self.model.requests), before)
        self.assertEqual(self.get_run(next_run).json()["status"], "failed")

    def test_internal_binding_identity_remains_part_of_full_canonical_validation(self) -> None:
        conversation, run = self.exercise("search")
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        source = saved["final_message_id"]
        message = self.store.data[
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{source}"
        ]
        binding = copy.deepcopy(message["tool_bindings"][0])
        binding["result"]["result"]["memories"][0]["owner_user_id"] = "wrong-owner"
        message["tool_bindings"] = [binding]
        next_run = ResourceId(
            self.turn(conversation, key="canonical-owner-tampered", text="zzz_no_match").json()[
                "run_id"
            ]
        )
        before = len(self.model.requests)
        self.execute(next_run)
        self.assertEqual(len(self.model.requests), before)
        self.assertEqual(self.get_run(next_run).json()["status"], "failed")
