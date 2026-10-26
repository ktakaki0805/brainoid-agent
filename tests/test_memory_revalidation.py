"""R1 deadline retention, unsupported derived sources and scoped seed regressions."""

import copy
import json
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import current_thread
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

import test_scoped_retrieval as fixtures
from brainoid_agent_core.types import ToolCall
from brainoid_api.errors import APIError
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ResourceId, Scope
from brainoid_contracts.guards import GuardedTransactions
from brainoid_contracts.transactions import Transaction
from brainoid_memory.retrieval import SearchInput, retrieve
from brainoid_worker.memory import MemoryRuntime
from brainoid_worker.run_turn import SystemClock
from fakes.memory_repository import LIGHT, PERSON, TENANT, context, entity_values
from test_scoped_retrieval import PRIVATE, SHARED, MemoryWorkerFixture
from test_turn_run import StrictTransaction


class MatchedClock(SystemClock):
    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now()


class MemoryDeadlineTests(MemoryWorkerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.handler.clock = MatchedClock(self.clock.now)

    def configure_calls(self, calls: tuple[ToolCall, ...]) -> None:
        super().configure_calls(calls)
        self.handler.clock = MatchedClock(self.clock.now)

    def final_path(self, conversation: ResourceId, run: ResourceId) -> str:
        final = uuid5(UUID(run), "final-message")
        return f"tenants/{self.tenant}/conversations/{conversation}/messages/{final}"

    def expire_on_final_read(self, path: str) -> Any:
        original = StrictTransaction.get
        advanced: list[str] = []

        def get(tx: StrictTransaction, current: str) -> Any:
            result = original(tx, current)
            if current == path and not advanced:
                advanced.append(current)
                self.clock.advance(2)
            return result

        return patch.object(StrictTransaction, "get", get), advanced

    def test_final_last_read_checks_all_deadlines_and_cleanup_does_not_retain_guards(self) -> None:
        self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        final = self.final_path(conversation, run)
        hook, advanced = self.expire_on_final_read(final)
        with hook:
            self.execute(run)
        self.assertTrue(advanced)
        self.assertNotIn(final, self.store.data)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertTrue(
            all(d["active"] == 0 for p, d in self.store.data.items() if "/usage_slots/" in p)
        )
        following = ResourceId(
            self.turn(conversation, key="after-deadline-cleanup", text="zzz_no_match").json()[
                "run_id"
            ]
        )
        self.execute(following)
        self.assertEqual(self.get_run(following).json()["status"], "completed")

    def test_stream_final_read_cannot_persist_expired_delta(self) -> None:
        self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
        self.model.text = "EXPIRING_MEMORY " * 100
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        original_check, original_get = MemoryRuntime.check_sources, StrictTransaction.get
        armed: set[int] = set()
        advanced: list[str] = []

        def check(runtime: MemoryRuntime, tx: Transaction, *args: Any) -> None:
            original_check(runtime, tx, *args)
            if current_thread().name == "run-event-writer":
                armed.add(id(getattr(tx, "transaction", tx)))

        def get(tx: StrictTransaction, path: str) -> Any:
            result = original_get(tx, path)
            if id(tx) in armed and not advanced:
                advanced.append(path)
                self.clock.advance(2)
            return result

        with (
            patch.object(MemoryRuntime, "check_sources", check),
            patch.object(StrictTransaction, "get", get),
        ):
            self.execute(run)
        self.assertTrue(advanced)
        events = [
            d
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.tenant}/runs/{run}/events/")
            and d.get("type") == "text.delta"
        ]
        self.assertEqual(events, [])
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_delayed_authorizer_response_checks_history_when_new_search_is_empty(self) -> None:
        self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
        self.model.text = "EXPIRING_MEMORY"
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        second = ResourceId(
            self.turn(conversation, key="delayed-history-second", text="zzz_no_match").json()[
                "run_id"
            ]
        )
        original = self.store.run
        reads: list[str] = []

        def run(work: Any) -> Any:
            result = original(work)
            if work.__qualname__ == "Authorizer.check.<locals>.read":
                reads.append(work.__qualname__)
                if len(reads) == 2:
                    self.clock.advance(2)
            return result

        with patch.object(self.store, "run", run):
            self.execute(second)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(second).json()["status"], "failed")

    def test_final_guard_keeps_tool_only_result_deadline(self) -> None:
        self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
        self.configure_calls(
            (ToolCall(call_key="search", name="memory.search", arguments_json='{"query":"Alex"}'),)
        )
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="zzz_no_match").json()["run_id"])
        final = self.final_path(conversation, run)
        hook, advanced = self.expire_on_final_read(final)
        with hook:
            self.execute(run)
        self.assertTrue(advanced)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved.get("memory_records"), [])
        self.assertTrue(saved["tool_bindings"])
        self.assertNotIn(final, self.store.data)
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_final_guard_keeps_own_revision_historical_input_deadline(self) -> None:
        old = self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
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
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text="Correct the memory: Alex preference is coffee").json()[
                "run_id"
            ]
        )
        final = self.final_path(conversation, run)
        hook, advanced = self.expire_on_final_read(final)
        with hook:
            self.execute(run)
        self.assertTrue(advanced)
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/facts/{old.id}"]["status"], "superseded"
        )
        self.assertNotIn(final, self.store.data)
        self.assertEqual(self.get_run(run).json()["status"], "failed")

    def test_post_commit_expiry_drops_response_without_claiming_rollback(self) -> None:
        self.memory(value="EXPIRING_MEMORY", valid_to=self.clock.now() + timedelta(seconds=1))
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        final = self.final_path(conversation, run)
        original = self.store.run
        advanced: list[str] = []

        def delayed(work: Any) -> Any:
            result = original(work)
            if final in self.store.data and not advanced:
                advanced.append(final)
                self.clock.advance(2)
            return result

        with patch.object(self.store, "run", delayed):
            self.execute(run)
        self.assertTrue(advanced)
        self.assertIn(final, self.store.data)
        self.assertLess(
            datetime.fromisoformat(self.store.data[final]["created_at"]), self.clock.now()
        )
        self.assertTrue(
            all(d["active"] == 0 for p, d in self.store.data.items() if "/usage_slots/" in p)
        )

    def assert_session_deadline(self, *, from_phone: bool) -> None:
        credentials = self.credentials()
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(acknowledge_shared_space=True, allowed_scopes=["conversation"]),
            "deadline-session-key",
            "deadline-request",
        )
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session.id}"]["expires_at"] = (
            self.clock.now() + timedelta(seconds=1)
        )
        conversation = self.conversation(private=True, display=DisplayPolicy.AUTHENTICATED_SESSION)
        body: dict[str, Any] = {"text": "No optional memory access", "output_mode": "text"}
        headers = {"Idempotency-Key": "deadline-device-turn"}
        if from_phone:
            body["output_device_id"] = identity.sub
        else:
            self.app.dependency_overrides[reader_identity] = lambda: identity
            headers["X-Private-Session"] = session.id
        accepted = self.client.post(self.url + f"/{conversation}/turns", json=body, headers=headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        run = ResourceId(accepted.json()["run_id"])
        self.assertEqual(self.store.data[f"tenants/{self.tenant}/runs/{run}"]["memory_scopes"], [])
        final = self.final_path(conversation, run)
        hook, advanced = self.expire_on_final_read(final)
        with hook:
            self.execute(run)
        self.assertTrue(advanced)
        self.assertNotIn(final, self.store.data)
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertTrue(
            all(d["active"] == 0 for p, d in self.store.data.items() if "/usage_slots/" in p)
        )

    def test_caller_session_deadline_retained_without_memory_scopes(self) -> None:
        self.assert_session_deadline(from_phone=False)

    def test_output_session_deadline_retained_without_memory_scopes(self) -> None:
        self.assert_session_deadline(from_phone=True)


class DerivedMessageSourceTests(MemoryWorkerFixture):
    def test_assistant_and_multiturn_derived_sources_are_unsupported_even_with_empty_metadata(
        self,
    ) -> None:
        self.memory(value="SOURCE_SENTINEL")
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        self.execute(first)
        second = ResourceId(
            self.turn(conversation, key="derived-following-run", text="zzz_no_match").json()[
                "run_id"
            ]
        )
        self.execute(second)
        for run in (first, second):
            saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
            source = saved["final_message_id"]
            if run == second:
                self.assertEqual(saved["memory_records"], [])
            with self.assertRaises(APIError):
                self.memory(
                    value="DERIVED_SENTINEL",
                    source_refs=[{"kind": "message", "resource_id": source, "version": 1}],
                )

    def test_legacy_derived_fact_cannot_bypass_source_suppression_deletion_version_or_expiry(
        self,
    ) -> None:
        source_fact = self.memory(value="SOURCE_SENTINEL")
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        self.execute(first)
        source = self.store.data[f"tenants/{self.tenant}/runs/{first}"]["final_message_id"]
        derived = self.memory(value="DERIVED_SENTINEL")
        # Legacy canonical B persisted before assistant-source support was withdrawn.
        self.store.data[f"tenants/{self.tenant}/facts/{derived.id}"]["source_refs"] = [
            {"kind": "message", "resource_id": source, "version": 1}
        ]
        path = f"tenants/{self.tenant}/facts/{source_fact.id}"
        original = copy.deepcopy(self.store.data[path])
        for changes in (
            {"suppress_reuse": True},
            {"deleted": True},
            {"version": 2},
            {
                "valid_from": (self.clock.now() - timedelta(days=1)).isoformat(),
                "valid_to": self.clock.now().isoformat(),
            },
        ):
            self.store.data[path] = {**original, **changes}
            result = self.memories.search(
                self.identity(),
                self.tenant,
                SearchInput(query="DERIVED_SENTINEL"),
                "legacy-derived-source",
            )
            self.assertNotIn(derived.id, [r.id for r in result.items])
        self.store.data[path] = {**original, "suppress_reuse": True}
        other = self.conversation()
        run = ResourceId(
            self.turn(other, key="legacy-derived-second", text="DERIVED_SENTINEL").json()["run_id"]
        )
        self.execute(run)
        references = [
            p.text
            for m in self.model.requests[-1].messages
            for p in m.parts
            if p.type == "text" and p.text.startswith("Reference data")
        ]
        self.assertNotIn(str(derived.id), " ".join(references))
        self.assertEqual(self.get_run(run).json()["status"], "completed")

    def test_direct_user_message_supported_and_derived_metadata_or_cycles_rejected(self) -> None:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="Explicit source").json()["run_id"])
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        source = saved["input_message_id"]
        refs = [{"kind": "message", "resource_id": source, "version": 1}]
        memory = self.memory(value="DIRECT_USER_SOURCE", source_refs=refs)
        self.assertIn(
            memory.id,
            [
                m.id
                for m in self.memories.search(
                    self.identity(),
                    self.tenant,
                    SearchInput(query="DIRECT_USER_SOURCE"),
                    "direct-message-search",
                ).items
            ],
        )
        path = f"tenants/{self.tenant}/conversations/{conversation}/messages/{source}"
        original = copy.deepcopy(self.store.data[path])
        changes: dict[str, Any]
        for changes in (
            {"memory_records": []},
            {"tool_bindings": []},
            {"source_refs": refs},
            {"role": "assistant"},
            {"model_messages": []},
        ):
            self.store.data[path] = {**original, **changes}
            result = self.memories.search(
                self.identity(),
                self.tenant,
                SearchInput(query="DIRECT_USER_SOURCE"),
                "direct-message-search",
            )
            self.assertEqual(result.items, [])
        self.store.data[path] = original
        self.assertTrue(self.memory(value="EMPTY_DIRECT_PROVENANCE").id)


class ScopedResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ScopedRetrievalTests()
        self.fixture.setUp()

    def test_retry_uses_fresh_evidence_instead_of_retaining_abandoned_snapshot_deadline(
        self,
    ) -> None:
        f = self.fixture
        now = f.caller.valid_from
        fact = f.save(valid_to=now + timedelta(seconds=1))
        path = f"tenants/{TENANT}/facts/{fact.id}"

        def concurrent_update() -> None:
            nonlocal now
            now += timedelta(seconds=2)
            f.store.data[path].update(version=2, valid_to=(now + timedelta(hours=1)).isoformat())

        f.store.retry = concurrent_update

        def read(tx: Transaction) -> Any:
            return f.repo.facts(tx, context()).get(fact.id, for_model=True)

        with patch.object(f.repo, "_now", lambda: now):
            current = GuardedTransactions(f.store).run(read)
        self.assertEqual(current.version, 2)
        self.assertGreater(current.valid_to, now)

    def test_exact_global_limit_stops_reported_in_both_scope_orders(self) -> None:
        f = self.fixture
        f.save()
        f.save(ResourceId(str(uuid4())), visibility="private", display_policy="never")
        for scopes in ((SHARED, PRIVATE), (PRIVATE, SHARED)):

            def read(tx: Transaction, scopes: tuple[Scope, ...] = scopes) -> Any:
                access = f.repo.facts(tx, context())
                return retrieve(
                    access, access.entities(), SearchInput(query="Alex", limit=1), scopes=scopes
                )

            result = f.store.run(read)
            self.assertEqual(len(result.records), 1)
            self.assertTrue(result.trace.fact_limit_reached)
        # Hidden data is not counted to decide whether exploration has stopped.
        for path, doc in f.store.data.items():
            if "/facts/" in path and doc.get("visibility") == "private":
                doc["owner_user_id"] = "invisible-owner"

        def read_shared(tx: Transaction) -> Any:
            access = f.repo.facts(tx, context())
            return retrieve(
                access,
                access.entities(),
                SearchInput(query="Alex", limit=1),
                scopes=(SHARED, PRIVATE),
            )

        self.assertTrue(f.store.run(read_shared).trace.fact_limit_reached)

    def test_external_ids_keep_integrations_and_hidden_candidates_separate(self) -> None:
        f = self.fixture
        first = f.save()
        external = "light.fixture_bedroom"
        first_ref = {
            "provider": "home_assistant",
            "integration_id": str(uuid4()),
            "external_id": external,
        }
        f.store.data[f"tenants/{TENANT}/entities/{LIGHT}"]["external_refs"] = [first_ref]
        self.assertEqual([r.id for r in f.search(external).records], [first.id])
        second = ResourceId(str(uuid4()))
        f.repo.create_entity(context(), second, entity_values(kind="topic", label="Second lamp"))
        second_fact = f.save(
            ResourceId(str(uuid4())), subject_entity_id=PERSON, object_entity_id=second
        )
        f.store.data[f"tenants/{TENANT}/entities/{second}"]["external_refs"] = [
            {**first_ref, "integration_id": str(uuid4())}
        ]
        result = f.search(external)
        self.assertEqual({r.id for r in result.records}, {first.id, second_fact.id})
        self.assertTrue(result.trace.ambiguous_entities)
        f.store.data[f"tenants/{TENANT}/entities/{second}"].update(
            visibility="private", display_policy="never", owner_user_id="invisible-owner"
        )
        result = f.search(external)
        self.assertEqual([r.id for r in result.records], [first.id])
        self.assertFalse(result.trace.ambiguous_entities)
