"""MEM-04 real transactions from canonical memories through worker tool/history reuse."""

import json
from contextlib import AbstractContextManager
from datetime import timedelta
from threading import current_thread
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from brainoid_agent_core.types import ModelRequest, ToolCall
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.memories import Memories, MemoryCreate
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_contracts.transactions import Transaction
from brainoid_memory.retrieval import SearchInput
from brainoid_persistence.firestore import _Transaction
from brainoid_runs.models import RunAccepted, TurnCreate
from brainoid_runs.state import KIND
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.memory import MemoryRuntime
from brainoid_worker.run_turn import RunTurn
from fakes.run_confirmation import MultiStageModel
from test_memory_revalidation import MatchedClock
from test_turn_run_integration import TurnRunIntegrationFixture


class MemoryRetrievalIntegrationTests(TurnRunIntegrationFixture):
    def test_retrieved_revision_atomic_proof_stream_final_and_history(self) -> None:
        cursors = Cursors(b"x" * 32, now=lambda: self.now)
        entities = Entities(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        entity = entities.create(
            self.identity(),
            self.tenant,
            EntityCreate(
                kind="person",
                label="Alex",
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
            ),
            "retrieval-entity-key",
        )
        body = {
            "subject_entity_id": entity.id,
            "predicate": "prefers",
            "value": "tea",
            "visibility": "shared",
            "display_policy": "shared_display",
        }
        old = memories.create(
            self.identity(), self.tenant, MemoryCreate.model_validate(body), "retrieval-memory-key"
        )
        memories.create(
            self.identity(),
            self.tenant,
            MemoryCreate.model_validate(
                {
                    **body,
                    "value": "PRIVATE_SENTINEL",
                    "visibility": "private",
                    "display_policy": "never",
                }
            ),
            "retrieval-private-key",
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["memory.revise"]}
        )
        conversation = self.conversation()
        accepted = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(text="Correct the memory: Alex preference is coffee", output_mode="text"),
            "retrieval-turn-key",
        )
        model = MultiStageModel(
            (
                ToolCall(
                    call_key="revision",
                    name="memory.revise",
                    arguments_json=json.dumps(
                        {
                            "memory_id": old.id,
                            "expected_version": 1,
                            "replacement": {**body, "value": "coffee"},
                        }
                    ),
                ),
            )
        )
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.execute(accepted.run_id)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, accepted.run_id).status,
            "completed",
        )
        self.assertEqual(memories.get(self.identity(), self.tenant, old.id).status, "superseded")
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn("PRIVATE_SENTINEL", " ".join(r.model_dump_json() for r in model.requests))
        model.calls = ()
        next_run = self.accept(conversation, "retrieval-next-turn-key")
        self.execute(next_run.run_id)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, next_run.run_id).status,
            "completed",
        )
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/memory_effects").stream())), 1
        )

    def seeded_memory(self, *, expires: bool = True) -> tuple[Memories, Any]:
        cursors = Cursors(b"y" * 32, now=lambda: self.now)
        entities = Entities(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        entity = entities.create(
            self.identity(),
            self.tenant,
            EntityCreate(
                kind="person",
                label="Alex",
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
            ),
            "deadline-entity-key",
        )
        body: dict[str, Any] = {
            "subject_entity_id": entity.id,
            "predicate": "prefers",
            "value": "EXPIRING_MEMORY",
            "visibility": "shared",
            "display_policy": "shared_display",
        }
        if expires:
            body["valid_to"] = self.now + timedelta(seconds=1)
        memory = memories.create(
            self.identity(), self.tenant, MemoryCreate.model_validate(body), "deadline-memory-key"
        )
        self.handler.clock = MatchedClock(lambda: self.now)
        return memories, memory

    def accept_text(self, conversation: ResourceId, text: str, key: str) -> RunAccepted:
        return self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(text=text, output_mode="text"),
            key,
        )

    def test_final_last_sdk_read_past_fact_deadline_does_not_save_assistant(self) -> None:
        self.seeded_memory()
        conversation = self.conversation()
        run = self.accept_text(conversation, "Alex", "final-deadline-turn").run_id
        final = f"tenants/{self.tenant}/conversations/{conversation}/messages/{uuid5(UUID(run), 'final-message')}"
        original = _Transaction.get
        advanced: list[str] = []

        def get(tx: _Transaction, path: str) -> Any:
            value = original(tx, path)
            if path == final and not advanced:
                advanced.append(path)
                self.now += timedelta(seconds=2)
            return value

        with patch.object(_Transaction, "get", get):
            self.execute(run)
        self.assertTrue(advanced)
        self.assertFalse(self.db.document(final).get().exists)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "failed"
        )

    def test_stream_last_sdk_read_past_fact_deadline_does_not_save_delta(self) -> None:
        self.seeded_memory()
        self.model.text = "EXPIRING_MEMORY " * 100
        conversation = self.conversation()
        run = self.accept_text(conversation, "Alex", "stream-deadline-turn").run_id
        original_check, original_get = MemoryRuntime.check_sources, _Transaction.get
        armed: set[int] = set()
        advanced: list[str] = []

        def check(runtime: MemoryRuntime, tx: Transaction, *args: Any) -> None:
            original_check(runtime, tx, *args)
            if current_thread().name == "run-event-writer":
                armed.add(id(getattr(tx, "transaction", tx)))

        def get(tx: _Transaction, path: str) -> Any:
            value = original_get(tx, path)
            if id(tx) in armed and not advanced:
                advanced.append(path)
                self.now += timedelta(seconds=2)
            return value

        with (
            patch.object(MemoryRuntime, "check_sources", check),
            patch.object(_Transaction, "get", get),
        ):
            self.execute(run)
        self.assertTrue(advanced)
        events = [
            s.to_dict()
            for s in self.db.collection(f"tenants/{self.tenant}/runs/{run}/events").stream()
        ]
        self.assertFalse(any(e and e.get("type") == "text.delta" for e in events))
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "failed"
        )

    def test_history_delayed_sdk_response_does_not_start_second_model(self) -> None:
        self.seeded_memory()
        self.model.text = "EXPIRING_MEMORY"
        conversation = self.conversation()
        first = self.accept_text(conversation, "Alex", "history-deadline-first").run_id
        self.execute(first)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, first).status, "completed"
        )
        second = self.accept_text(conversation, "zzz_no_match", "history-deadline-second").run_id
        original = self.persistence.run
        reads: list[str] = []

        def delayed(work: Any) -> Any:
            value = original(work)
            if work.__qualname__ == "Authorizer.check.<locals>.read":
                reads.append(work.__qualname__)
                if len(reads) == 2:
                    self.now += timedelta(seconds=2)
            return value

        with patch.object(self.persistence, "run", delayed):
            self.execute(second)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, second).status, "failed"
        )

    def test_legacy_assistant_source_cannot_launder_suppressed_memory(self) -> None:
        memories, old = self.seeded_memory(expires=False)
        conversation = self.conversation()
        first = self.accept_text(conversation, "Alex", "derived-assistant-first").run_id
        self.execute(first)
        run_doc = self.db.document(f"tenants/{self.tenant}/runs/{first}").get().to_dict()
        assert run_doc is not None
        source = [{"kind": "message", "resource_id": run_doc["final_message_id"], "version": 1}]
        body = {
            "subject_entity_id": old.subject_entity_id,
            "predicate": "prefers",
            "value": "DERIVED_SENTINEL",
            "visibility": "shared",
            "display_policy": "shared_display",
        }
        with self.assertRaises(APIError):
            memories.create(
                self.identity(),
                self.tenant,
                MemoryCreate.model_validate({**body, "source_refs": source}),
                "unsupported-assistant-source",
            )
        legacy = memories.create(
            self.identity(), self.tenant, MemoryCreate.model_validate(body), "legacy-derived-memory"
        )
        self.db.document(f"tenants/{self.tenant}/facts/{legacy.id}").update({"source_refs": source})
        self.db.document(f"tenants/{self.tenant}/facts/{old.id}").update({"suppress_reuse": True})
        found = memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="DERIVED_SENTINEL"),
            "legacy-source-search",
        )
        self.assertEqual(found.items, [])
        separate = self.conversation()
        second = self.accept_text(separate, "DERIVED_SENTINEL", "derived-assistant-second").run_id
        self.execute(second)
        reference = " ".join(
            p.text
            for m in self.model.requests[-1].messages
            for p in m.parts
            if p.type == "text" and p.text.startswith("Reference data")
        )
        self.assertNotIn(str(legacy.id), reference)

    def test_canonical_external_id_seeds_scoped_graph(self) -> None:
        memories, record = self.seeded_memory(expires=False)
        self.db.document(f"tenants/{self.tenant}/entities/{record.subject_entity_id}").update(
            {
                "external_refs": [
                    {
                        "provider": "home_assistant",
                        "integration_id": str(uuid4()),
                        "external_id": "light.fixture_bedroom",
                    }
                ]
            }
        )
        found = memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="light.fixture_bedroom"),
            "external-memory-search",
        )
        self.assertEqual([item.id for item in found.items], [record.id])

    def async_history_handoff(self, boundary: str) -> None:
        from brainoid_agent_core.core import TextAgentCore
        from brainoid_worker.metered_model import MeteredModel
        from brainoid_worker.run_turn import ReadBridge

        self.seeded_memory()
        self.model.text = "EXPIRED_ASYNC_HISTORY"
        conversation = self.conversation()
        first = self.accept_text(conversation, "Alex", "async-history-first").run_id
        self.execute(first)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, first).status, "completed"
        )
        second = self.accept_text(conversation, "zzz_no_match", "async-history-second").run_id
        reader, wait = ReadBridge.read, TextAgentCore._wait
        hits: list[str] = []

        async def delayed_reader(bridge: ReadBridge, work: Any) -> Any:
            value = await reader(bridge, work)
            if work.__qualname__ == "Authorizer.check.<locals>.<lambda>":
                hits.append(boundary)
                if len(hits) == 2:
                    self.now += timedelta(seconds=2)
            return value

        async def delayed_core(core: TextAgentCore, work: Any, control: Any) -> Any:
            name = getattr(getattr(work, "cr_code", None), "co_qualname", "")
            if (
                boundary == "provider_task"
                and name.endswith("generate.<locals>.advance")
                and not hits
            ):

                async def delayed() -> Any:
                    hits.append(boundary)
                    self.now += timedelta(seconds=2)
                    return await work

                return await wait(core, delayed(), control)
            value = await wait(core, work, control)
            if boundary == "core_cleanup" and name == "Authorizer.check":
                hits.append(boundary)
                if len(hits) == 2:
                    self.now += timedelta(seconds=2)
            return value

        meters: list[MeteredModel] = []

        def metered(*args: Any, **kwargs: Any) -> MeteredModel:
            value = MeteredModel(*args, **kwargs)
            meters.append(value)
            return value

        hook: AbstractContextManager[Any] = (
            patch.object(ReadBridge, "read", delayed_reader)
            if boundary == "reader"
            else patch.object(TextAgentCore, "_wait", delayed_core)
        )
        if boundary == "meter_serialization":
            serialize = ModelRequest.model_dump_json

            def delayed_serialization(request: ModelRequest, *args: Any, **kwargs: Any) -> str:
                value = serialize(request, *args, **kwargs)
                if not hits:
                    hits.append(boundary)
                    self.now += timedelta(seconds=2)
                return value

            hook = patch.object(ModelRequest, "model_dump_json", delayed_serialization)
        with hook, patch("brainoid_worker.run_turn.MeteredModel", side_effect=metered):
            self.execute(second)
        self.assertEqual(
            len(hits), 1 if boundary in ("provider_task", "meter_serialization") else 2
        )
        self.assertEqual(len(self.model.requests), 1)
        self.assertFalse(meters[-1].started)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, second).status, "failed"
        )

    def test_history_deadline_after_reader_handoff(self) -> None:
        self.async_history_handoff("reader")

    def test_history_deadline_after_core_cleanup_handoff(self) -> None:
        self.async_history_handoff("core_cleanup")

    def test_history_deadline_before_provider_task_starts(self) -> None:
        self.async_history_handoff("provider_task")

    def test_history_deadline_after_metering_before_provider_dispatch(self) -> None:
        self.async_history_handoff("meter_serialization")

    def projected_tool_history(self, kind: str) -> None:
        _, old = self.seeded_memory(expires=False)
        body = {
            "subject_entity_id": old.subject_entity_id,
            "predicate": "prefers",
            "value": "coffee",
            "visibility": "shared",
            "display_policy": "shared_display",
        }
        args: dict[str, Any]
        if kind == "search":
            args = {"query": "Alex"}
        elif kind == "remember":
            args = body
        else:
            args = {"memory_id": old.id, "expected_version": 1, "replacement": body}
        name = "memory." + kind
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": [name]}
        )
        model = MultiStageModel(
            (ToolCall(call_key=kind, name=name, arguments_json=json.dumps(args)),)
        )
        self.handler = RunTurn(
            self.jobs,
            model,
            credential_epoch=self.tokens.epoch,
            clock=MatchedClock(lambda: self.now),
        )
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        conversation = self.conversation()
        text = {
            "search": "Alex",
            "remember": "Remember that Alex prefers coffee",
            "revise": "Correct the memory: Alex preference is coffee",
        }[kind]
        first = self.accept_text(conversation, text, "projected-tool-first").run_id
        self.execute(first)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, first).status, "completed"
        )
        saved = self.db.document(f"tenants/{self.tenant}/runs/{first}").get().to_dict()
        assert saved is not None
        full = saved["tool_bindings"][0]["result"]["result"]["memories"][0]
        self.assertEqual(full["created_by"], old.created_by)
        self.assertEqual(full["tenant_id"], self.tenant)
        model.calls = ()
        second = self.accept_text(conversation, "zzz_no_match", "projected-tool-next").run_id
        self.execute(second)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, second).status, "completed"
        )
        payload = " ".join(r.model_dump_json() for r in model.requests)
        for identity in (str(old.created_by), str(self.tenant)):
            self.assertNotIn(identity, payload)
        for key in ("tenant_id", "owner_user_id", "created_by"):
            self.assertNotIn(key, payload)
        self.assertIn("recorded_at", payload)
        self.assertIn("source_refs", payload)

    def test_search_projection_and_following_history(self) -> None:
        self.projected_tool_history("search")

    def test_remember_projection_and_following_history(self) -> None:
        self.projected_tool_history("remember")

    def test_revise_projection_and_following_history(self) -> None:
        self.projected_tool_history("revise")
