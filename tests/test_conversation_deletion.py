"""UI12 / T-022 T-034: actual HTTP producer, immutable intent and existing purge."""

import copy
from typing import cast
from unittest.mock import patch

from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.repositories.memories import Memories
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import tombstone_path
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import DeleteConversation, operation_path
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeDispatcher, PurgeResource
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    CONVERSATION,
    FACT,
    NOW,
    OTHER,
    SOURCE,
    TENANT,
    GraphStore,
    context,
    fact_values,
)
from fakes.purge import FakePurgePages
from fakes.summaries import BASE, MESSAGE, summary_data
from httpx2 import Response
from test_exports import ExportFixture
from test_memory_purge import PurgeFixture
from test_turn_run import TurnRunFixture


class ConversationDeletionHTTPTests(TurnRunFixture):
    def setUp(self) -> None:
        super().setUp()
        self.deletion = DeletionLedger(MemoryObjects())
        self.memories = Memories(
            self.store,
            self.tenants,
            self.conversations.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
            deletion_ledger=self.deletion,
        )
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            conversations=self.conversations,
            memories=self.memories,
        )

    def delete(
        self, identity: ResourceId, key: str = "conversation-delete-key", **changes: object
    ) -> Response:
        return self.client.post(
            f"/v1/tenants/{self.tenant}/operations",
            json={
                "kind": "delete_resource",
                "target_type": "conversation",
                "target_id": identity,
                "expected_version": 1,
                **changes,
            },
            headers={"Idempotency-Key": key},
        )

    def test_real_producer_immediate_fence_exact_replay_and_full_children_purge(self) -> None:
        conversation = self.conversation(private=True)
        run = ResourceId(self.turn(conversation).json()["run_id"])
        self.execute(run)
        base = f"tenants/{self.tenant}/conversations/{conversation}"
        original = copy.deepcopy(self.store.data[base])
        self.store.data[base + "/speech_refs/ghost/chunks/one"] = {
            "tenant_id": self.tenant,
            "ref": "SPEECH_REF",
        }
        version = original["version"]
        response = self.delete(conversation, expected_version=version)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(
            self.delete(conversation, expected_version=version).json(), response.json()
        )
        self.assertEqual(
            self.delete(
                conversation, expected_version=version, include_derived_memories=False
            ).status_code,
            409,
        )
        for url in (
            self.url + f"/{conversation}",
            self.url + f"/{conversation}/messages",
            f"/v1/tenants/{self.tenant}/runs/{run}",
        ):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.turn(conversation, key="late-message-key").status_code, 404)
        operation = ResourceId(response.json()["operation_id"])
        # Both fake stores expose the same data-only paging interface.
        pages = FakePurgePages(cast(GraphStore, self.store))
        jobs = PurgeJobs(self.jobs, self.deletion)
        handler = PurgeResource(
            jobs,
            pages,
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=2,
        )
        queue = LocalQueue()
        dispatcher = PurgeDispatcher(handler, pages, queue)
        worker = Worker(jobs, LocalCaller(), {KIND: handler})
        for _ in range(20):
            dispatcher.recover(self.tenant)
            queue.drain(worker)
            if (
                self.store.data[operation_path(self.tenant, operation)]["operation"]["status"]
                == "succeeded"
            ):
                break
            self.clock.advance(10)
        self.assertEqual(
            self.store.data[operation_path(self.tenant, operation)]["operation"]["status"],
            "succeeded",
        )
        self.assertFalse(any(p == base or p.startswith(base + "/") for p in self.store.data))
        run_path = f"tenants/{self.tenant}/runs/{run}"
        self.assertFalse(any(p.startswith(run_path + "/") for p in self.store.data))
        self.assertNotIn("completed_text", self.store.data[run_path])
        self.store.data[base] = original
        self.assertEqual(self.client.get(self.url + f"/{conversation}").status_code, 404)
        self.assertEqual(self.turn(conversation, key="restored-message-key").status_code, 404)

    def test_exact_version_ledger_failure_and_no_unaccepted_success(self) -> None:
        conversation = self.conversation()
        self.assertEqual(self.delete(conversation, expected_version=2).status_code, 412)
        with patch.object(self.deletion, "append", side_effect=LedgerError()):
            self.assertEqual(self.delete(conversation).status_code, 503)
        self.assertEqual(self.client.get(self.url + f"/{conversation}").status_code, 200)
        self.assertEqual(self.turn(conversation).status_code, 409)
        self.assertEqual(self.delete(conversation).status_code, 202)

    def test_shared_write_policy_private_and_foreign_tenant_boundaries(self) -> None:
        shared, private = self.conversation(), self.conversation(private=True)
        author = self.uid
        self.uid = "demo-a-member"
        self.assertIn(self.delete(shared).status_code, (403, 404))
        self.assertIn(self.delete(private).status_code, (403, 404))
        self.uid = "demo-b-owner"
        self.assertIn(self.delete(shared).status_code, (403, 404))
        self.uid = author
        self.store.data[f"tenants/{self.tenant}/conversations/{shared}"]["created_by"] = (
            "other-contributor"
        )
        self.assertEqual(self.delete(shared).status_code, 202)
        self.store.data[f"tenants/{self.tenant}/conversations/{private}"]["owner_user_id"] = (
            "other-contributor"
        )
        self.assertEqual(self.delete(private, key="other-private-delete-key").status_code, 404)


class ConversationDeletionPurgeTests(PurgeFixture):
    def accept_conversation(self, include: bool) -> None:
        data = summary_data()
        data[BASE].update(
            visibility="shared",
            display_policy="shared_display",
            owner_user_id=None,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        self.store.data.update({p: d for p, d in data.items() if p.startswith(BASE)})
        self.repo._sources = CanonicalSources(SavedMessages())
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}]),
        )
        self.repo.create_fact(
            context(),
            SOURCE,
            fact_values(source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}]),
        )
        self.own_fact = f"tenants/{TENANT}/facts/{SOURCE}"
        self.own_original = copy.deepcopy(self.store.data[self.own_fact])
        self.assertEqual(self.repo.get_fact(context(), SOURCE, for_model=True).id, SOURCE)
        other_base = f"tenants/{TENANT}/conversations/{OTHER}"
        self.store.data[other_base] = {**data[BASE], "id": OTHER}
        self.store.data[other_base + "/messages/" + OTHER] = {
            **data[MESSAGE],
            "id": OTHER,
            "conversation_id": OTHER,
        }
        self.repo.create_fact(
            context(),
            OTHER,
            fact_values(source_refs=[{"kind": "message", "resource_id": OTHER, "version": 1}]),
        )
        self.store.data[self.fact_path].update(
            visibility="private", display_policy="never", owner_user_id="other-private-copy"
        )
        self.store.data[self.fact_path + "/versions/one"] = {
            "tenant_id": TENANT,
            "value": "PRIVATE_DERIVED_COPY",
        }
        self.operation = self.acceptor.forget(
            lambda tx: self.repo.facts(tx, context()),
            DeleteConversation(
                kind="delete_resource",
                target_type="conversation",
                target_id=CONVERSATION,
                expected_version=1,
                include_derived_memories=include,
            ),
            "delete-conversation-purge-key",
        )
        self.job_id = job_id(TENANT, KIND, self.operation.operation_id)
        self.op_path = operation_path(TENANT, self.operation.operation_id)

    def test_derived_true_erases_only_exact_conversation_sources_with_durable_fences(self) -> None:
        self.accept_conversation(True)
        original = copy.deepcopy(self.store.data[self.fact_path])
        self.drain()
        self.assertFalse(any(p == BASE or p.startswith(BASE + "/") for p in self.store.data))
        self.assertFalse(
            any(p == self.fact_path or p.startswith(self.fact_path + "/") for p in self.store.data)
        )
        self.assertIn(f"tenants/{TENANT}/facts/{OTHER}", self.store.data)
        self.assertIn(f"tenants/{TENANT}/conversations/{OTHER}/messages/{OTHER}", self.store.data)
        self.assertIn(tombstone_path(TENANT, "facts", FACT), self.store.data)
        self.store.data[self.fact_path] = original
        self.assertNotIn(self.own_fact, self.store.data)
        self.store.data[self.own_fact] = self.own_original
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(), SOURCE)

        self.queue.drain(self.worker)

    def test_derived_false_retains_fact_but_deletes_whole_conversation(self) -> None:
        self.accept_conversation(False)
        self.drain()
        self.assertIn(self.fact_path, self.store.data)
        self.assertIn(self.fact_path + "/versions/one", self.store.data)
        self.assertNotIn(BASE, self.store.data)
        self.assertIn(self.own_fact, self.store.data)
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(), SOURCE, for_model=True)


class ConversationExportInvalidationTests(ExportFixture):
    def test_ready_export_status_remains_succeeded_but_real_download_rejects_deleted_conversation(
        self,
    ) -> None:
        self.message(created_at=NOW.isoformat(), updated_at=NOW.isoformat())
        export = self.ready()
        self.assertEqual(self.download(export).status_code, 200)
        deleted = self.client.post(
            self.operations_url,
            json={
                "kind": "delete_resource",
                "target_type": "conversation",
                "target_id": CONVERSATION,
                "expected_version": 1,
            },
            headers={"Idempotency-Key": "conversation-export-race-key"},
        )
        self.assertEqual(deleted.status_code, 202, deleted.text)
        status = self.client.get(self.operations_url + "/" + export)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "succeeded")
        self.assertIn(self.download(export).status_code, (403, 404))
