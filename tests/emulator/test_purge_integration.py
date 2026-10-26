"""MEM-08 actual hierarchy RPC pagination, transaction retries, and lease fencing."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid5

from brainoid_agent_core.types import ToolResult
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_contracts import ResourceId
from brainoid_jobs import LeaseLost
from brainoid_jobs.models import Lease, job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, operation_path
from brainoid_memory.models import Fact
from brainoid_persistence.firestore import FirestoreTransactions, PersistenceUnavailable
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_tools import Authority
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from brainoid_worker.memory import model_record
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeDispatcher, PurgeResource
from google.api_core.exceptions import Aborted, ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture


class PurgeIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.parent, self.child, self.message = self.source()
        self.db.document(self.child).update({"created_by": self.identity().user_id})
        self.memory = self.create_memory()
        self.original_memory = Fact.model_validate(
            self.db.document(f"tenants/{self.tenant}/facts/{self.memory.id}").get().to_dict()
        )
        self.deletions = DeletionLedger(LocalAuthzObjects(self.directory))
        self.memories.deletion_ledger = self.deletions
        self.operation = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(
                kind="forget_memory",
                target_id=self.memory.id,
                expected_version=1,
                source_policy="delete_source",
            ),
            "purge-emulator-key",
        )
        self.memories.jobs.store = FirestoreTransactions(self.db)
        self.jobs = PurgeJobs(self.memories.jobs, self.deletions)
        self.pages = FirestorePurgePages(self.db)
        self.handler = PurgeResource(
            self.jobs,
            self.pages,
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=2,
        )
        self.queue = LocalQueue()
        self.dispatcher = PurgeDispatcher(self.handler, self.store, self.queue)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.job_id = job_id(self.tenant, KIND, self.operation.operation_id)
        self.op_path = operation_path(self.tenant, self.operation.operation_id)
        self.fact_path = f"tenants/{self.tenant}/facts/{self.memory.id}"

    def state(self) -> Any:
        return self.db.document(self.op_path).get().to_dict()

    def test_real_pages_remove_all_generations_ghost_descendants_and_replay(self) -> None:
        for generation in "abcdef":
            collection = f"tenants/{self.tenant}/memory_vectors_{generation * 32}"
            self.db.document(collection + "/" + self.memory.id).set(
                {
                    "id": self.memory.id,
                    "tenant_id": self.tenant,
                    "embedding": [1],
                }
            )
        leaves = [self.fact_path + f"/children/{i}/parts/content" for i in range(7)]
        for leaf in leaves:
            self.db.document(leaf).set({"text": "SYNTHETIC_NESTED_BODY"})
        self.db.document(self.fact_path).delete()
        # Missing parent documents must be returned by show_missing, even when
        # deleting the previous page removes its containing collection entirely.
        children, token = self.pages.children(
            self.fact_path + "/children", collections=False, limit=2
        )
        self.assertEqual(len(children), 2)
        self.assertTrue(token)
        self.dispatcher.recover(self.tenant)
        delivery = next(iter(self.queue.tasks.values()))
        self.queue.drain(self.worker)
        self.assertEqual(self.state()["operation"]["status"], "succeeded", self.state())
        self.assertFalse(self.db.document(self.child).get().exists)
        for leaf in leaves:
            self.assertFalse(self.db.document(leaf).get().exists)
        for generation in "abcdef":
            self.assertFalse(
                self.db.document(
                    f"tenants/{self.tenant}/memory_vectors_{generation * 32}/{self.memory.id}"
                )
                .get()
                .exists
            )
        before = self.state()["purge"]["processed"]
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.state()["purge"]["processed"], before)

    def test_commit_retry_lost_response_page_progress_and_concurrent_delivery(self) -> None:
        self.jobs.activate(self.tenant, self.job_id)
        delivery = self.jobs.prepare_dispatch(self.tenant, self.job_id, queue_name=KIND)
        assert delivery is not None
        leases = self.concurrent(
            lambda: self.jobs.acquire(delivery, known_handler=lambda k: k == KIND),
            lambda: self.jobs.acquire(delivery, known_handler=lambda k: k == KIND),
        )
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        lease = next(lease for lease in leases if lease is not None)
        assert isinstance(lease, Lease)
        execution = Execution(self.jobs, lease)
        original = self.db._firestore_api.commit
        attempts = 0

        def contention(**kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": kwargs["request"]["transaction"],
                    }
                )
                raise Aborted("synthetic contention")  # type: ignore[no-untyped-call]
            return original(**kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=contention):
            self.handler.step(execution)
        self.assertGreater(attempts, 1)
        before = self.state()["purge"]
        lost = False

        def lose(**kwargs: Any) -> Any:
            nonlocal lost
            result = original(**kwargs)
            if not lost and kwargs["request"].get("writes"):
                lost = True
                raise ServiceUnavailable("synthetic commit response loss")  # type: ignore[no-untyped-call]
            return result

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            with self.assertRaises(PersistenceUnavailable):
                self.handler.step(execution)
        self.assertTrue(lost)
        after = self.state()["purge"]
        self.assertNotEqual(before, after)
        self.now += timedelta(minutes=2)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.state()["operation"]["status"], "succeeded")

    def test_ghost_vector_parents_multiple_generations_pages_and_resume(self) -> None:
        other = "00000000-0000-0000-0000-000000000099"
        affected: list[str] = []
        untouched: list[str] = []
        for generation in "abc":
            base = f"tenants/{self.tenant}/memory_vectors_{generation * 32}"
            for i in range(5):
                for identity, paths in ((self.memory.id, affected), (other, untouched)):
                    leaf = f"{base}/{identity}/chunks/{i:02d}/parts/ghost/deeper/body"
                    self.db.document(leaf).set(
                        {"tenant_id": self.tenant, "text": "SYNTHETIC_VECTOR_COPY"}
                    )
                    paths.append(leaf)
            records, _ = self.pages.children(base, collections=False, limit=2)
            self.assertIn(base + "/" + self.memory.id, records)
            self.assertFalse(self.db.document(base + "/" + self.memory.id).get().exists)
        self.jobs.activate(self.tenant, self.job_id)
        delivery = self.jobs.prepare_dispatch(self.tenant, self.job_id, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda k: k == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        for _ in range(90):
            self.handler.step(execution)
        self.now += timedelta(minutes=2)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.state()["operation"]["status"], "succeeded")
        self.assertGreater(self.state()["purge"]["passes"], 0)
        for leaf in affected:
            self.assertFalse(self.db.document(leaf).get().exists)
        for leaf in untouched:
            self.assertTrue(self.db.document(leaf).get().exists)

    def paused_missing_receipt(self, *, collection: bool, remaining: bool) -> tuple[Execution, str]:
        # Canonical typed metadata fixture for the real RPC boundary. Normal
        # HTTP/RunTurn/second-operation production is covered in test_purge_overlap.
        run_id, tool_id = ResourceId(str(UUID(int=701))), ResourceId(str(UUID(int=702)))
        effect = ResourceId(str(uuid5(UUID(tool_id), "brainoid-tool-effect")))
        job = self.jobs.get(self.tenant, self.job_id)
        assert job is not None
        record = model_record(self.original_memory).model_dump(mode="json")
        self.db.document(f"tenants/{self.tenant}/runs/{run_id}").set(
            {
                "tenant_id": self.tenant,
                "run_id": run_id,
                "conversation_id": self.parent.rsplit("/", 1)[-1],
                "input_sequence": 1,
                "created_at": self.now.isoformat(),
                "memory_records": [record],
                "tool_call_ids": [tool_id],
                "status": "completed",
            }
        )
        receipt = f"tenants/{self.tenant}/tool_executions/{tool_id}"
        self.db.document(receipt).set(
            {
                "tool_call_id": tool_id,
                "effect_id": effect,
                "name": "memory.search",
                "authority": Authority(
                    context=job.grant.context,
                    run_id=run_id,
                    receipt="0" * 64,
                    allowed_tools=frozenset({"memory.search"}),
                    max_calls=64,
                    confirmation="not_required",
                ).model_dump(mode="json"),
                "result": ToolResult(
                    tool_call_id=tool_id,
                    operation_id=effect,
                    status="succeeded",
                    result={"memories": [record]},
                ).model_dump(mode="json"),
            }
        )
        child = receipt + "/copies/part"
        self.db.document(child).set({"body": "SYNTHETIC_RPC_CHILD"})
        self.jobs.activate(self.tenant, self.job_id)
        delivery = self.jobs.prepare_dispatch(self.tenant, self.job_id, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        cursor = receipt + "/copies" if collection else child
        for _ in range(600):
            self.handler.step(execution)
            if self.state()["purge"]["stack"][-1]["path"] == cursor:
                break
        else:
            self.fail("Expected selected real Firestore child cursor")
        self.assertTrue(self.state()["purge"]["stack"][-1]["force"])
        # Model peer commit removing the selected ancestor. Only these synthetic
        # child copies are injected; no claim that search currently produces them.
        self.db.document(receipt).delete()
        if not remaining:
            self.db.document(child).delete()
        self.now += timedelta(minutes=2)
        self.dispatcher.recover(self.tenant)
        with self.assertRaises(LeaseLost):
            self.handler.step(execution)
        self.queue.drain(self.worker)
        return execution, child

    def test_missing_selected_receipt_document_cursor_resumes_when_subtree_empty(self) -> None:
        _, child = self.paused_missing_receipt(collection=False, remaining=False)
        self.assertEqual(self.state()["operation"]["status"], "succeeded")
        self.assertFalse(self.db.document(child).get().exists)
        count = self.state()["purge"]["processed"]
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.state()["purge"]["processed"], count)

    def test_missing_selected_receipt_collection_cursor_rejects_live_ghost_child(self) -> None:
        _, child = self.paused_missing_receipt(collection=True, remaining=True)
        self.assertNotEqual(self.state()["operation"]["status"], "succeeded")
        self.assertEqual(self.state()["purge"]["retry_reason"], "derived_ancestor_missing")
        self.assertTrue(self.db.document(child).get().exists)
        ancestor = child.rsplit("/copies/", 1)[0]
        self.assertFalse(self.db.document(ancestor).get().exists)
        roots, _ = self.pages.children(ancestor.rsplit("/", 1)[0], collections=False, limit=2)
        self.assertIn(ancestor, roots)
        children, _ = self.pages.children(ancestor + "/copies", collections=False, limit=2)
        self.assertIn(child, children)
