"""MEM-07/T-011/T-022: ledger/DB crash boundaries and immediate reuse fences."""

import copy
import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import ToolCall
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import exportable, tombstone_path
from brainoid_contracts.reuse import reusable_text
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, ForgetMemories
from brainoid_memory.models import Fact
from brainoid_policy.control_ledger import LedgerError
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import history
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    CONVERSATION,
    FACT,
    LIGHT,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    context,
    entity_values,
    fact_values,
)
from fakes.summaries import MESSAGE
from test_memories import MemoryFixture
from test_scoped_retrieval import MemoryWorkerFixture
from test_summary_provenance import SummaryFixture


class ForgetTests(MemoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.objects = MemoryObjects()
        self.memories.deletion_ledger = DeletionLedger(self.objects)
        self.operations_url = f"/v1/tenants/{TENANT}/operations"
        self.message()
        self.message_path = f"tenants/{TENANT}/conversations/{CONVERSATION}/messages/{SOURCE}"
        self.store.data[self.message_path]["created_by"] = USER
        self.memory_id = self.create_memory(
            source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}]
        ).json()["id"]
        self.fact_path = f"tenants/{TENANT}/facts/{self.memory_id}"
        self.request = {"kind": "forget_memory", "target_id": self.memory_id, "expected_version": 1}

    def forget(self, **changes: Any) -> Any:
        return self.client.post(
            self.operations_url,
            json={**self.request, **changes},
            headers={"Idempotency-Key": "forget-fixture-key-001"},
        )

    def test_acceptance_is_pending_durable_private_content_free_and_idempotent(self) -> None:
        response = self.forget()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(self.forget().json(), response.json())
        self.assertEqual(
            self.client.get(self.operations_url + "/" + response.json()["operation_id"]).json(),
            response.json(),
        )
        self.assertEqual(len(self.objects.data), 1)
        for forbidden in ("tea", "synthetic source", USER):
            self.assertNotIn(forbidden, repr(self.objects.data))
        source = self.store.data[self.message_path]
        self.assertEqual(source["text"], "synthetic source")
        self.assertFalse(source["deleted"])
        self.assertTrue(
            self.store.run(
                lambda tx: exportable(tx, TENANT, "messages", SOURCE, source, parent=CONVERSATION)
            )
        )
        self.assertIsNone(reusable_text(source))
        self.assertFalse(
            self.store.run(
                lambda tx: exportable(
                    tx, TENANT, "facts", ResourceId(self.memory_id), self.store.data[self.fact_path]
                )
            )
        )
        self.assertEqual(self.client.get(self.url + "/" + self.memory_id).status_code, 404)
        self.assertEqual(self.client.get(self.url).json()["items"], [])
        search = self.client.post(
            self.url + ":search",
            json={"query": "tea"},
            headers={"Idempotency-Key": "after-forget-search"},
        )
        self.assertEqual(search.json()["items"], [])
        purge = [
            d for p, d in self.store.data.items() if "/jobs/" in p and d["kind"] == "memory_purge"
        ]
        self.assertEqual(len(purge), 1)
        self.assertEqual(purge[0]["attributes"], {"dispatch_queue": "memory_purge"})
        self.assertIsNone(
            self.memories.jobs.prepare_dispatch(TENANT, purge[0]["id"], queue_name="default")
        )

    def test_ledger_failures_before_and_after_write_never_accept_then_resume_same_operation(
        self,
    ) -> None:
        initial = copy.deepcopy(self.store.data)
        for after in (False, True):
            with self.subTest(after=after):
                self.store.data = copy.deepcopy(initial)
                self.objects.data.clear()

                def fail(name: str) -> None:
                    raise LedgerError()

                self.objects.before = (lambda name: None) if after else fail
                self.objects.after = fail if after else lambda name: None
                response = self.forget()
                self.assertEqual(response.status_code, 503)
                self.assertFalse(self.store.data[self.fact_path]["deleted"])
                self.assertFalse(self.store.data[self.message_path]["suppress_reuse"])
                saved = [
                    d for p, d in self.store.data.items() if "/memory_forget_reservations/" in p
                ][0]
                self.assertEqual(self.revise_memory(self.memory_id).status_code, 412)
                self.objects.before = self.objects.after = lambda name: None
                accepted = self.forget()
                self.assertEqual(accepted.status_code, 202, accepted.text)
                self.assertEqual(
                    accepted.json()["operation_id"], saved["operation"]["operation_id"]
                )

    def test_db_failure_after_ledger_and_commit_response_loss_are_replayable(self) -> None:
        def fail_db(name: str) -> None:
            self.store.fail_commit = True

        self.objects.after = fail_db
        with self.assertRaises(RuntimeError):
            self.memories.forget(
                self.identity(),
                TENANT,
                ForgetCreate.model_validate(self.request),
                "forget-fixture-key-001",
            )
        self.assertEqual(len(self.objects.data), 1)
        self.assertFalse(self.store.data[self.fact_path]["deleted"])
        self.store.fail_commit = False
        self.objects.after = lambda name: None
        lost: list[bool] = []

        def lose_response() -> None:
            if self.store.data[self.fact_path]["deleted"] and not lost:
                lost.append(True)
                raise RuntimeError("Synthetic commit response loss")

        self.store.after_commit = lose_response
        with self.assertRaises(RuntimeError):
            self.memories.forget(
                self.identity(),
                TENANT,
                ForgetCreate.model_validate(self.request),
                "forget-fixture-key-001",
            )
        self.store.after_commit = lambda: None
        response = self.forget()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(len([p for p in self.store.data if "/operations/" in p]), 1)

    def test_restored_pre_request_database_reconstructs_from_independent_intent(self) -> None:
        before = copy.deepcopy(self.store.data)
        first = self.forget().json()
        self.store.data = before
        self.now += timedelta(seconds=10)
        second = self.forget()
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["operation_id"], first["operation_id"])
        self.assertEqual(second.json()["created_at"], first["created_at"])
        self.assertEqual(len(self.objects.data), 1)

    def test_other_source_delete_scope_escalation_stale_version_and_wrong_kind_rejected(
        self,
    ) -> None:
        self.store.data[self.message_path]["created_by"] = "another-author"
        self.assertEqual(self.forget(source_policy="delete_source").status_code, 404)
        self.assertFalse(self.objects.data)
        self.store.data[self.message_path]["created_by"] = USER
        self.assertEqual(self.forget(expected_version=2).status_code, 412)
        self.store.data[self.fact_path].update(visibility="private", display_policy="never")
        self.assertEqual(self.forget().status_code, 404)
        self.assertFalse(self.objects.data)
        self.assertEqual(self.forget(kind="delete_tenant").status_code, 422)

    def test_member_cannot_suppress_another_authors_shared_source(self) -> None:
        self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["role"] = "member"
        self.store.data[self.message_path]["created_by"] = "another-author"
        self.assertNotEqual(self.forget().status_code, 202)
        self.assertFalse(self.objects.data)
        self.store.data[self.message_path]["created_by"] = USER
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}"]["created_by"] = (
            "another-author"
        )
        self.assertNotEqual(self.forget().status_code, 202)
        self.assertFalse(self.objects.data)
        self.assertFalse(self.store.data[self.message_path]["suppress_reuse"])

    def test_delete_source_and_key_payload_binding(self) -> None:
        response = self.forget(source_policy="delete_source")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(self.store.data[self.message_path]["deleted"])
        self.assertEqual(self.store.data[self.message_path]["deletion_epoch"], 1)
        self.assertEqual(self.forget().status_code, 409)
        self.assertEqual(self.forget(target_id=str(uuid4())).status_code, 409)

    def test_persistent_tombstones_block_stale_canonical_restore_and_fact_recreation(self) -> None:
        old_fact = copy.deepcopy(self.store.data[self.fact_path])
        old_source = copy.deepcopy(self.store.data[self.message_path])
        self.assertEqual(self.forget().status_code, 202)
        markers = [d for p, d in self.store.data.items() if "/deletion_tombstones/" in p]
        self.assertEqual(len(markers), 2)
        self.assertNotIn("synthetic source", repr(markers))
        self.store.data[self.fact_path] = old_fact
        self.store.data[self.message_path] = old_source
        self.assertEqual(self.client.get(self.url + "/" + self.memory_id).status_code, 404)
        self.assertEqual(self.client.get(self.url).json()["items"], [])

        def read(tx: Any) -> None:
            access = self.memories._open(tx, self.identity(), TENANT, None)
            resolved = access._repo._sources.resolve(
                tx, access.principal.context, Fact.model_validate(old_fact).source_refs[0]
            )
            self.assertTrue(resolved.suppress_reuse)

        self.store.run(read)
        del self.store.data[self.fact_path]

        def recreate(tx: Any) -> None:
            access = self.memories._open(tx, self.identity(), TENANT, None)
            access.prepare(ResourceId(self.memory_id), self.memories_body())

        from brainoid_policy.scopes import PolicyDenied

        with self.assertRaises(PolicyDenied):
            self.store.run(recreate)

    def test_fact_cannot_be_ui_retained_by_a_source_only_suppression_marker(self) -> None:
        self.store.data[tombstone_path(TENANT, "facts", ResourceId(self.memory_id))] = {
            "operation_id": str(uuid4()),
            "mode": "suppress_reuse",
            "created_at": self.now.isoformat(),
            "target": {
                "kind": "fact",
                "resource_id": self.memory_id,
                "parent_id": None,
                "version": 1,
                "deletion_epoch": 1,
            },
        }
        self.assertEqual(self.client.get(self.url + "/" + self.memory_id).status_code, 404)
        self.assertFalse(
            self.store.run(
                lambda tx: exportable(
                    tx, TENANT, "facts", ResourceId(self.memory_id), self.store.data[self.fact_path]
                )
            )
        )

    def memories_body(self) -> Any:
        from brainoid_memory.dto import MemoryCreate

        return MemoryCreate.model_validate(self.body).fact(self.now)

    def test_revocation_after_intent_never_uses_reservation_as_authority(self) -> None:
        def revoke(name: str) -> None:
            self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["status"] = "revoked"

        self.objects.after = revoke
        self.assertNotEqual(self.forget().status_code, 202)
        self.assertFalse(self.store.data[self.fact_path]["deleted"])
        self.assertEqual(len(self.objects.data), 1)
        self.objects.after = lambda name: None
        self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["status"] = "active"
        self.assertEqual(self.forget().status_code, 202)
        self.uid = FirebaseUid("other-user")
        with self.assertRaises(APIError) as error:
            self.memories.forget(
                self.identity(),
                TENANT,
                ForgetCreate.model_validate(self.request),
                "forget-fixture-key-001",
            )
        self.assertEqual(error.exception.code, ErrorCode.RESOURCE_NOT_FOUND)

    def test_ledger_read_delay_cannot_start_an_intent_after_caller_expiry(self) -> None:
        def delayed(name: str) -> None:
            self.now += timedelta(hours=2)
            return None

        with patch.object(self.objects, "read", side_effect=delayed):
            self.assertNotEqual(self.forget().status_code, 202)
        self.assertFalse(self.objects.data)
        self.assertFalse(self.store.data[self.fact_path]["deleted"])

    def test_private_pending_reservation_does_not_expose_conflict_to_another_member(self) -> None:
        self.store.data[self.fact_path].update(visibility="private", display_policy="never")
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}"].update(
            visibility="private", display_policy="never"
        )
        with patch.object(self.objects, "create", side_effect=LedgerError()):
            self.assertEqual(self.forget().status_code, 503)
        self.uid = FirebaseUid("another-member")
        self.store.data[f"tenants/{TENANT}/memberships/another"] = {
            "tenant_id": TENANT,
            "user_id": self.uid,
            "role": "member",
            "status": "active",
            "authorization_version": 1,
        }
        self.assertEqual(self.forget().status_code, 404)

    def test_two_facts_using_same_source_serialize_their_forget_reservations(self) -> None:
        second = self.create_memory(
            key="another-memory-key",
            source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}],
        ).json()["id"]
        conflicts: list[ErrorCode] = []

        def other(name: str) -> None:
            try:
                self.memories.forget(
                    self.identity(),
                    TENANT,
                    ForgetCreate(
                        kind="forget_memory", target_id=ResourceId(second), expected_version=1
                    ),
                    "second-forget-key",
                )
            except APIError as error:
                conflicts.append(error.code)

        self.objects.before = other
        self.assertEqual(self.forget().status_code, 202)
        self.assertEqual(conflicts, [ErrorCode.CONFLICT])
        self.objects.before = lambda name: None
        operation = self.memories.forget(
            self.identity(),
            TENANT,
            ForgetCreate(kind="forget_memory", target_id=ResourceId(second), expected_version=1),
            "second-forget-key",
        )
        self.assertEqual(operation.status, "pending")
        self.assertEqual(len(self.objects.data), 2)
        self.assertTrue(self.store.data[self.message_path]["suppress_reuse"])


class ForgetWorkerTests(MemoryWorkerFixture):
    def configure_forget(self, memory: str, **changes: Any) -> None:
        self.configure_calls(
            (
                ToolCall(
                    call_key="forget",
                    name="memory.forget",
                    arguments_json=json.dumps(
                        {"memory_id": memory, "expected_version": 1, **changes}
                    ),
                ),
            )
        )
        self.objects = MemoryObjects()
        self.handler.memory.deletion_ledger = DeletionLedger(self.objects)

    def test_forget_stops_model_continuation_and_returns_pending_ack(self) -> None:
        memory = self.memory(value="FORGOTTEN_SENTINEL")
        self.configure_forget(memory.id)
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {memory.id}").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 1)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved["memory_records"], [])
        self.assertEqual(saved["continuation"], [])
        message = self.store.data[
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
        ]
        self.assertIn("消去処理を待っています", message["text"])
        self.assertTrue(message["suppress_reuse"])
        self.assertTrue(self.store.data[f"tenants/{self.tenant}/facts/{memory.id}"]["deleted"])
        self.assertNotIn("FORGOTTEN_SENTINEL", json.dumps(saved["tool_bindings"]))

    def test_keyword_quote_question_negation_wrong_target_and_delete_expansion_do_not_authorize(
        self,
    ) -> None:
        memory = self.memory()
        for text, policy in (
            ("What does forget mean?", "suppress_reuse"),
            (f'"Forget memory {memory.id}"', "suppress_reuse"),
            (f"Do not forget memory {memory.id}", "suppress_reuse"),
            (f"Forget memory {uuid4()}", "suppress_reuse"),
            (f"Forget memory {memory.id}", "delete_source"),
        ):
            with self.subTest(text=text):
                self.configure_forget(memory.id, source_policy=policy)
                conversation = self.conversation()
                run = ResourceId(self.turn(conversation, text=text).json()["run_id"])
                self.execute(run)
                self.assertFalse(self.objects.data)
                self.assertFalse(
                    self.store.data[f"tenants/{self.tenant}/facts/{memory.id}"]["deleted"]
                )

    def test_same_run_source_suppression_rebinds_history_only_for_atomic_forget(self) -> None:
        conversation = self.conversation()
        original_run = ResourceId(
            self.turn(conversation, text="Alex likes ORIGINAL_SOURCE_SENTINEL").json()["run_id"]
        )
        self.execute(original_run)
        original = self.store.data[f"tenants/{self.tenant}/runs/{original_run}"]
        source = original["input_message_id"]
        memory = self.memory(
            value="ORIGINAL_SOURCE_SENTINEL",
            source_refs=[{"kind": "message", "resource_id": source, "version": 1}],
        )
        self.configure_forget(memory.id)
        accepted = self.turn(
            conversation, key="forget-source-history", text=f"Forget memory {memory.id}"
        )
        run = ResourceId(accepted.json()["run_id"])
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 1)
        self.assertIn("ORIGINAL_SOURCE_SENTINEL", self.model.requests[0].model_dump_json())
        documents, messages, _ = self.store.run(lambda tx: history(tx, self.tenant, conversation))
        self.assertNotIn("ORIGINAL_SOURCE_SENTINEL", repr(messages))
        self.assertTrue(all(d["role"] == "user" for d in documents))
        page = self.conversations.messages(self.identity(), self.tenant, conversation)
        self.assertIn(source, [item.id for item in page.items])

    def test_source_deletion_hides_only_deleted_message_even_after_stale_restore(self) -> None:
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="ORIGINAL_SOURCE_SENTINEL").json()["run_id"])
        self.execute(run)
        source = self.store.data[f"tenants/{self.tenant}/runs/{run}"]["input_message_id"]
        path = f"tenants/{self.tenant}/conversations/{conversation}/messages/{source}"
        original = copy.deepcopy(self.store.data[path])
        memory = self.memory(source_refs=[{"kind": "message", "resource_id": source, "version": 1}])
        self.memories.deletion_ledger = DeletionLedger(MemoryObjects())
        self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(
                kind="forget_memory",
                target_id=memory.id,
                expected_version=1,
                source_policy="delete_source",
            ),
            "delete-original-source",
        )
        for restore in (False, True):
            if restore:
                self.store.data[path] = original
            page = self.conversations.messages(self.identity(), self.tenant, conversation)
            self.assertNotIn(source, [item.id for item in page.items])
            self.assertTrue(page.items)

    def test_ledger_failure_cannot_emit_success_ack_or_resume_the_model(self) -> None:
        memory = self.memory()
        self.configure_forget(memory.id)

        def fail(name: str) -> None:
            raise LedgerError()

        self.objects.before = fail
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {memory.id}").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertEqual(len(self.model.requests), 1)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertNotIn("forget_operation_id", saved)
        self.assertNotIn("final_message_id", saved)

    def test_derived_user_input_is_not_explicit_forget_authority(self) -> None:
        memory = self.memory()
        self.configure_forget(memory.id)
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {memory.id}").json()["run_id"]
        )
        record = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.store.data[
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{record['input_message_id']}"
        ]["source_refs"] = [{"kind": "memory", "resource_id": memory.id, "version": 1}]
        self.execute(run)
        self.assertFalse(self.objects.data)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")


class ForgetSummaryTests(SummaryFixture):
    def test_accepted_forget_invalidates_existing_summary(self) -> None:
        self.deliver()
        self.assertIsNotNone(self.latest())
        self.repo._sources = CanonicalSources(SavedMessages())
        self.store.data[MESSAGE]["created_by"] = USER
        self.repo.create_entity(
            context(),
            PERSON,
            entity_values(kind="person", visibility="private", display_policy="never"),
        )
        self.repo.create_entity(
            context(), LIGHT, entity_values(visibility="private", display_policy="never")
        )
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(
                subject_entity_id=PERSON,
                visibility="private",
                display_policy="never",
                source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}],
            ),
        )
        ledger = DeletionLedger(MemoryObjects())
        result = ForgetMemories(self.jobs.store, ledger, now=lambda: self.now).forget(
            lambda tx: self.repo.facts(tx, context()),
            ForgetCreate(kind="forget_memory", target_id=FACT, expected_version=1),
            "forget-summary-test",
        )
        self.assertEqual(result.status, "pending")
        self.assertIsNone(self.latest())
        self.assertIsNone(reusable_text(self.store.data[MESSAGE]))
