"""MEM-08 / T-011 T-022 resumable deletion authority and physical erasure."""

import copy
import unittest
from datetime import timedelta
from uuid import uuid4

from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import tombstone_path
from brainoid_jobs import Jobs, LeaseLost
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, ForgetMemories, operation_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import ControlLedger
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import history
from brainoid_worker.entry import CallerDenied, Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import PurgeDispatcher, PurgeResource
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    CONVERSATION,
    FACT,
    LIGHT,
    NOW,
    OTHER,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    GraphStore,
    authority_data,
    context,
    entity_values,
    fact_values,
    repository,
)
from fakes.purge import FakeInvalidation, FakePurgePages
from fakes.summaries import ASSISTANT, BASE, MESSAGE, summary_data
from test_embedding_fencing import EmbeddingFixture
from test_summary_provenance import SummaryFixture


class PurgeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = GraphStore()
        self.store.data = authority_data()
        self.repo = repository(self.store)
        self.repo.create_entity(context(), LIGHT, entity_values())
        self.repo.create_entity(context(), PERSON, entity_values(kind="person"))
        self.ledger = DeletionLedger(MemoryObjects())
        self.acceptor = ForgetMemories(self.repo._store, self.ledger, now=lambda: self.now)
        normal = Jobs(
            self.repo._store,
            ControlLedger(MemoryObjects()),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.normal = normal
        self.jobs = PurgeJobs(normal, self.ledger)
        self.pages = FakePurgePages(self.store)
        self.cache, self.export = FakeInvalidation(), FakeInvalidation()
        self.handler = PurgeResource(
            self.jobs, self.pages, hooks={"cache": self.cache, "export": self.export}, page_size=2
        )
        self.queue = LocalQueue()
        self.dispatcher = PurgeDispatcher(self.handler, self.pages, self.queue)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.fact_path = f"tenants/{TENANT}/facts/{FACT}"

    def accept(self, *, message: bool = False, delete: bool = False) -> None:
        if message:
            data = summary_data()
            data[BASE].update(visibility="shared", display_policy="shared_display")
            data[MESSAGE]["created_by"] = USER
            self.store.data.update({p: d for p, d in data.items() if p.startswith(BASE)})
            self.repo._sources = CanonicalSources(SavedMessages())
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(
                source_refs=[
                    {
                        "kind": "message" if message else "note",
                        "resource_id": SOURCE,
                        "version": 1,
                    }
                ]
            ),
        )
        self.operation = self.acceptor.forget(
            lambda tx: self.repo.facts(tx, context()),
            ForgetCreate(
                kind="forget_memory",
                target_id=FACT,
                expected_version=1,
                source_policy="delete_source" if delete else "suppress_reuse",
            ),
            "purge-fixture-request-001",
        )
        self.job_id = job_id(TENANT, KIND, self.operation.operation_id)
        self.op_path = operation_path(TENANT, self.operation.operation_id)

    def acquire(self) -> Execution:
        self.jobs.activate(TENANT, self.job_id)
        delivery = self.jobs.prepare_dispatch(TENANT, self.job_id, queue_name=KIND)
        assert delivery is not None
        self.delivery = delivery
        lease = self.jobs.acquire(delivery, known_handler=lambda k: k == KIND)
        assert lease is not None
        return Execution(self.jobs, lease)

    def drain(self) -> None:
        for _ in range(10):
            self.dispatcher.recover(TENANT)
            self.queue.drain(self.worker)
            if self.store.data[self.op_path]["operation"]["status"] == "succeeded":
                return
            self.now += timedelta(seconds=10)
        self.fail(str(self.jobs.get(TENANT, self.job_id)))


class PurgeTests(PurgeFixture):
    def test_all_generations_children_suppress_and_redelivery(self) -> None:
        self.accept()
        for generation in ("a", "b", "c"):
            base = f"tenants/{TENANT}/memory_vectors_{generation * 32}"
            self.store.data[base + "/" + FACT] = {"id": FACT, "tenant_id": TENANT, "embedding": [1]}
            self.store.data[base + "/" + LIGHT] = {
                "id": LIGHT,
                "tenant_id": TENANT,
                "embedding": [2],
            }
        child = self.fact_path + "/provenance/a/parts/b"
        self.store.data[child] = {"text": "DELETE_NESTED"}
        # Explicit target traversal works when the parent has already disappeared.
        del self.store.data[self.fact_path]
        other = f"tenants/{OTHER}/facts/{FACT}"
        self.store.data[other] = {"value": "OTHER_OWNER"}
        self.drain()
        self.assertNotIn(child, self.store.data)
        self.assertIn(other, self.store.data)
        self.assertEqual(sum("/memory_vectors_" in p for p in self.store.data), 3)
        self.assertIn(f"tenants/{TENANT}/notes/{SOURCE}", self.store.data)
        self.assertTrue(self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]["suppress_reuse"])
        self.assertIn(tombstone_path(TENANT, "facts", FACT), self.store.data)
        self.assertIsNotNone(self.ledger.get(self.operation.operation_id))
        after = copy.deepcopy(self.store.data)
        self.worker.handle(
            LOCAL_AUTHORIZATION,
            {"tenant_id": TENANT, "job_id": self.job_id, "dispatch_generation": 1},
        )
        self.assertEqual(self.store.data, after)
        self.assertEqual(len(self.cache.applied), 1)
        self.assertGreater(self.store.data[self.op_path]["purge"]["pages"], 3)

    def test_each_step_commit_loss_and_lease_expiry_resume_without_duplicate_count(self) -> None:
        self.accept(delete=True)
        execution = self.acquire()
        steps = 0
        while self.fact_path in self.store.data:
            self.handler.step(execution)
            steps += 1
            self.assertLess(steps, 400)
        count = self.store.data[self.op_path]["purge"]["processed"]
        lost = False

        def lose() -> None:
            nonlocal lost
            if not lost:
                lost = True
                raise PersistenceUnavailable()

        self.store.after_commit = lose
        with self.assertRaises(PersistenceUnavailable):
            self.handler.step(execution)
        self.store.after_commit = lambda: None
        self.now += timedelta(minutes=2)
        with self.assertRaises(LeaseLost):
            self.handler.step(execution)
        self.drain()
        self.assertGreaterEqual(self.store.data[self.op_path]["purge"]["processed"], count)
        self.assertNotIn(f"tenants/{TENANT}/notes/{SOURCE}", self.store.data)
        self.assertEqual(self.jobs.get(TENANT, self.job_id).status, "succeeded")  # type: ignore[union-attr]

    def test_revoked_requester_still_purges_but_ordinary_job_cannot_bypass(self) -> None:
        self.accept()
        self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["status"] = "suspended"
        self.jobs.activate(TENANT, self.job_id)
        delivery = self.jobs.prepare_dispatch(TENANT, self.job_id, queue_name=KIND)
        assert delivery is not None
        with self.assertRaises(CallerDenied):
            self.worker.handle("Bearer invalid", delivery.model_dump(mode="json"))
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.job_id, queue_name="default"))
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")

    def test_failed_hook_unknown_response_retries_same_key_and_does_not_complete(self) -> None:
        self.accept()
        self.export.lose_response = True
        self.dispatcher.recover(TENANT)
        self.queue.drain(self.worker)
        self.assertEqual(self.store.data[self.op_path]["operation"]["status"], "running")
        self.assertEqual(len(self.export.applied), 1)
        self.assertEqual(
            self.store.data[self.op_path]["purge"]["retry_reason"], "export_invalidation_failed"
        )
        self.now += timedelta(seconds=10)
        self.drain()
        self.assertEqual(len(self.export.calls), 2)
        self.assertEqual(self.export.calls[0], self.export.calls[1])
        self.assertEqual(len(self.cache.calls), 1)

    def test_mismatched_tenant_author_fence_and_missing_intent_cannot_purge(self) -> None:
        self.accept(delete=True)
        initial = copy.deepcopy(self.store.data)
        for change in ("tenant", "author", "fence", "intent"):
            with self.subTest(change=change):
                self.store.data = copy.deepcopy(initial)
                if change == "tenant":
                    self.store.data[self.fact_path]["tenant_id"] = OTHER
                elif change == "author":
                    self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]["created_by"] = (
                        "someone-else"
                    )
                elif change == "fence":
                    self.store.data[tombstone_path(TENANT, "facts", FACT)]["operation_id"] = str(
                        uuid4()
                    )
                else:
                    self.store.data[self.op_path]["intent"]["request_hash"] = "f" * 64
                self.dispatcher.recover(TENANT)
                self.queue.drain(self.worker)
                self.assertNotEqual(
                    self.store.data[self.op_path]["operation"]["status"], "succeeded"
                )
                if change == "author":
                    self.assertIn(f"tenants/{TENANT}/notes/{SOURCE}", self.store.data)
                else:
                    self.assertIn(self.fact_path, self.store.data)

    def test_monitor_detects_unstarted_and_failed_purge_over_24_hours(self) -> None:
        self.accept()
        self.now += timedelta(hours=24)
        self.assertEqual(self.handler.monitor(TENANT, self.pages).overdue, 0)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.handler.monitor(TENANT, self.pages).overdue, 1)
        self.drain()
        self.assertEqual(self.handler.monitor(TENANT, self.pages).overdue, 0)

    def test_message_derived_bodies_events_summaries_scrubbed_shared_originals_survive(
        self,
    ) -> None:
        self.accept(message=True)
        run = f"tenants/{TENANT}/runs/{FACT}"
        self.store.data[run] = {
            "run_id": FACT,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "input_sequence": 1,
            "created_at": NOW.isoformat(),
            "status": "completed",
            "continuation": [{"text": "DERIVED_SECRET"}],
            "version": 1,
            "memory_records": [{"memory_id": FACT, "tenant_id": TENANT}],
        }
        self.store.data[run + "/events/1"] = {"payload": {"text": "DERIVED_SECRET"}}
        summary = BASE + "/summaries/" + LIGHT
        self.store.data[summary] = {
            "tenant_id": TENANT,
            "text": "DERIVED_SECRET",
            "sources": {
                "conversation_id": CONVERSATION,
                "messages": [{"resource_id": SOURCE, "included": True}],
            },
        }
        self.store.data[BASE + "/messages/" + LIGHT] = {
            **self.store.data[MESSAGE],
            "id": LIGHT,
            "sequence": 3,
            "text": "OTHER_SHARED_ORIGINAL",
            "created_by": "other",
            "suppress_reuse": False,
            "version": 1,
        }
        fresh_run = f"tenants/{TENANT}/runs/{PERSON}"
        self.store.data[fresh_run] = {
            "run_id": PERSON,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "input_sequence": 4,
            "created_at": (NOW + timedelta(seconds=1)).isoformat(),
            "status": "completed",
            "continuation": ["FRESH_INDEPENDENT"],
        }
        self.drain()
        self.assertIn(MESSAGE, self.store.data)
        self.assertNotIn(ASSISTANT, self.store.data)
        self.assertNotIn(summary, self.store.data)
        self.assertNotIn(run + "/events/1", self.store.data)
        self.assertEqual(self.store.data[run]["status"], "failed")
        self.assertIn("FRESH_INDEPENDENT", repr(self.store.data[fresh_run]))
        self.assertNotIn("DERIVED_SECRET", repr(self.store.data))
        _, messages, _ = self.store.run(lambda tx: history(tx, TENANT, CONVERSATION))
        self.assertEqual(len(messages), 1)
        self.assertIn("OTHER_SHARED_ORIGINAL", repr(messages))

    def test_registered_notification_copy_manifest_does_not_match_text_or_other_source(
        self,
    ) -> None:
        from brainoid_worker.purge_handlers import ForgetHandlers, ReferenceCopies

        self.handler.handlers = ForgetHandlers({"notifications": ReferenceCopies()})
        self.accept()
        prefix = f"tenants/{TENANT}/notifications/"
        self.store.data[prefix + "affected"] = {
            "tenant_id": TENANT,
            "preview": "PRIVATE_COPY",
            "purge_sources": [{"kind": "fact", "resource_id": FACT, "parent_id": None}],
        }
        self.store.data[prefix + "unrelated"] = {
            "tenant_id": TENANT,
            "preview": f"Unrelated text mentioning {FACT}",
            "purge_sources": [{"kind": "fact", "resource_id": LIGHT, "parent_id": None}],
        }
        self.drain()
        self.assertNotIn(prefix + "affected", self.store.data)
        self.assertIn(prefix + "unrelated", self.store.data)

    def test_ledger_wait_turn_included_acceptance_bound_new_turn_excluded(self) -> None:
        def wait_then_admit(intent: object) -> None:
            original(intent)  # type: ignore[arg-type]
            self.now += timedelta(seconds=3)
            self.store.data[BASE]["next_sequence"] = 4
            self.store.data[f"tenants/{TENANT}/runs/{LIGHT}"] = {
                "run_id": LIGHT,
                "tenant_id": TENANT,
                "conversation_id": CONVERSATION,
                "input_sequence": 3,
                "created_at": self.now.isoformat(),
                "status": "completed",
                "continuation": ["LEDGER_GAP_COPY"],
            }
            self.now += timedelta(seconds=1)

        original = self.ledger.append
        self.ledger.append = wait_then_admit  # type: ignore[method-assign]
        self.accept(message=True)
        self.assertEqual(self.store.data[self.op_path]["purge_bounds"][CONVERSATION], 3)
        self.store.data[f"tenants/{TENANT}/runs/{PERSON}"] = {
            "run_id": PERSON,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "input_sequence": 4,
            "created_at": self.now.isoformat(),
            "status": "completed",
            "continuation": ["POST_ACCEPTANCE_INDEPENDENT"],
        }
        self.drain()
        self.assertNotIn("LEDGER_GAP_COPY", repr(self.store.data))
        self.assertIn("POST_ACCEPTANCE_INDEPENDENT", repr(self.store.data))

    def test_competing_lease_and_page_budget_preserve_progress(self) -> None:
        self.accept()
        self.handler.step_budget = 1
        self.dispatcher.recover(TENANT)
        delivery = next(iter(self.queue.tasks.values()))
        self.queue.drain(self.worker)
        previous = copy.deepcopy(self.store.data[self.op_path]["purge"])
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(previous, self.store.data[self.op_path]["purge"])
        self.handler.step_budget = 1000
        self.now += timedelta(seconds=10)
        self.drain()
        self.assertEqual(self.jobs.get(TENANT, self.job_id).attempts, 1)  # type: ignore[union-attr]

    def test_memory_effect_record_and_previous_erased_without_deleting_successor_fact(self) -> None:
        self.accept()
        effects = f"tenants/{TENANT}/memory_effects/"
        record = copy.deepcopy(self.store.data[self.fact_path])
        successor = {**record, "id": LIGHT, "value": "INDEPENDENT_SUCCESSOR", "deleted": False}
        self.store.data[f"tenants/{TENANT}/facts/{LIGHT}"] = successor
        self.store.data[effects + FACT] = {
            "effect_id": FACT,
            "record": record,
            "previous": None,
            "request": {"value": "OLD_BODY"},
        }
        self.store.data[effects + LIGHT] = {
            "effect_id": LIGHT,
            "record": successor,
            "previous": record,
            "request": {"value": "NEW_BODY"},
        }
        self.drain()
        self.assertNotIn(effects + FACT, self.store.data)
        self.assertNotIn(effects + LIGHT, self.store.data)
        self.assertEqual(self.store.data[f"tenants/{TENANT}/facts/{LIGHT}"], successor)

    def test_source_marker_from_another_forget_does_not_block_retained_source(self) -> None:
        self.accept()
        marker = self.store.data[tombstone_path(TENANT, "notes", SOURCE)]
        marker["operation_id"] = str(uuid4())
        marker["target"]["version"] = 1
        self.drain()
        self.assertIn(f"tenants/{TENANT}/notes/{SOURCE}", self.store.data)

    def test_infrastructure_outage_defers_and_preserves_fixed_operation_reason(self) -> None:
        from unittest.mock import patch

        self.accept()
        self.dispatcher.recover(TENANT)
        with patch.object(self.pages, "children", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.queue.drain(self.worker)
        self.assertEqual(
            self.store.data[self.op_path]["purge"]["retry_reason"], "persistence_unavailable"
        )
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        self.now += timedelta(seconds=10)
        self.drain()

    def test_slow_ledger_and_last_read_expiry_never_write_or_renew(self) -> None:
        self.accept()
        execution = self.acquire()
        before = copy.deepcopy(self.store.data)

        from unittest.mock import patch

        original = self.ledger.get

        def expire(operation: ResourceId) -> object:
            result = original(operation)
            self.now += timedelta(minutes=2)
            return result

        with patch.object(self.ledger, "get", side_effect=expire):
            with self.assertRaises(LeaseLost):
                self.handler.step(execution)
        self.assertEqual(self.store.data, before)
        self.now = NOW
        self.handler.step(execution)
        before = copy.deepcopy(self.store.data)
        count = 0

        def after_read(path: str) -> None:
            nonlocal count
            if path == self.op_path:
                count += 1
            if count == 4:
                self.now += timedelta(minutes=2)

        self.store.after_get = after_read
        with self.assertRaises(LeaseLost):
            self.handler.step(execution)
        self.assertEqual(self.store.data, before)
        self.store.after_get = lambda path: None
        self.drain()

    def test_tampered_resume_cursor_cannot_add_another_tenant_or_unaccepted_original(self) -> None:
        self.accept()
        execution = self.acquire()
        self.handler.step(execution)
        for target in (f"tenants/{OTHER}/facts/{FACT}", f"tenants/{TENANT}/notes/{LIGHT}"):
            with self.subTest(target=target):
                self.store.data[target] = {"text": "OUT_OF_SCOPE"}
                state = self.store.data[self.op_path]["purge"]
                state["stack"][-1].update(
                    path=target, force=True, done=True, pending=[], entered=True
                )
                with self.assertRaises(ValueError):
                    self.handler.step(execution)
                self.assertEqual(self.store.data[target]["text"], "OUT_OF_SCOPE")

    def test_committed_progress_survives_delayed_response_but_old_lease_stops(self) -> None:
        self.accept()
        execution = self.acquire()
        fired = False

        def delay_committed_response() -> None:
            nonlocal fired
            if not fired and "purge" in self.store.data[self.op_path]:
                fired = True
                self.now += timedelta(minutes=2)

        self.store.after_commit = delay_committed_response
        with self.assertRaises(LeaseLost):
            self.handler.step(execution)
        self.assertTrue(fired)
        progress = copy.deepcopy(self.store.data[self.op_path]["purge"])
        self.store.after_commit = lambda: None
        with self.assertRaises(LeaseLost):
            self.handler.step(execution)
        self.assertEqual(self.store.data[self.op_path]["purge"], progress)
        self.assertEqual(self.cache.calls, [])
        self.drain()

    def test_eight_process_crashes_resume_without_count_duplication_or_old_generation(self) -> None:
        self.accept()
        for i in range(12):
            self.store.data[self.fact_path + f"/parts/{i:02d}"] = {"body": "SYNTHETIC_PART"}
        old = None
        for completed in range(1, 9):
            execution = self.acquire()
            if old is not None:
                with self.assertRaises(LeaseLost):
                    self.handler.step(old)
            for _ in range(200):
                self.handler.step(execution)
                if self.store.data[self.op_path]["purge"]["processed"] == completed:
                    break
            else:
                self.fail("Expected a new committed part before each crash")
            old = execution
            self.now += timedelta(minutes=2)
        self.drain()
        self.assertEqual(self.store.data[self.op_path]["purge"]["processed"], 13)
        self.assertEqual(self.store.data[self.op_path]["purge_recoveries"], 8)
        self.assertEqual(self.jobs.get(TENANT, self.job_id).attempts, 1)  # type: ignore[union-attr]

    def test_legacy_exhausted_deletion_can_recover_but_unknown_effect_cannot(self) -> None:
        self.accept()
        execution = self.acquire()
        self.handler.step(execution)
        location = f"tenants/{TENANT}/jobs/{self.job_id}"
        saved = self.store.data[location]
        saved.update(
            status="failed",
            reason="attempts_exhausted",
            attempts=5,
            lease_expires_at=None,
            checkpoints=[{"key": "unknown", "state": "started", "result": None}],
        )
        self.assertFalse(self.jobs.recover_expired(TENANT, self.job_id))
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        self.store.data[location]["checkpoints"] = []
        self.drain()
        self.assertEqual(self.store.data[self.op_path]["purge_recoveries"], 1)
        self.assertEqual(self.jobs.get(TENANT, self.job_id).status, "succeeded")  # type: ignore[union-attr]


def purge_now(store: GraphStore, jobs: Jobs, ledger: DeletionLedger, operation: ResourceId) -> None:
    purge_jobs = PurgeJobs(jobs, ledger)
    pages = FakePurgePages(store)
    handler = PurgeResource(
        purge_jobs, pages, hooks={"cache": FakeInvalidation(), "export": FakeInvalidation()}
    )
    identity = job_id(TENANT, KIND, operation)
    assert purge_jobs.activate(TENANT, identity)
    delivery = purge_jobs.prepare_dispatch(TENANT, identity, queue_name=KIND)
    assert delivery is not None
    Worker(purge_jobs, LocalCaller(), {KIND: handler}).handle(
        LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
    )
    assert store.data[operation_path(TENANT, operation)]["operation"]["status"] == "succeeded"


class PurgeEmbeddingRaceTests(EmbeddingFixture):
    def test_inflight_embedding_and_restored_old_job_never_recreate_vectors(self) -> None:
        ledger = DeletionLedger(MemoryObjects())
        before = copy.deepcopy(self.store.data[f"tenants/{TENANT}/jobs/{self.identity}"])

        def forget() -> None:
            operation = ForgetMemories(self.jobs.store, ledger, now=lambda: self.now).forget(
                lambda tx: self.repo.facts(tx, context()),
                ForgetCreate(kind="forget_memory", target_id=self.fact.id, expected_version=1),
                "purge-inflight-embedding",
            )
            purge_now(self.store, self.jobs, ledger, operation.operation_id)

        self.embeddings.hook = forget
        self.deliver()
        self.assertNotIn(self.vector_path, self.store.data)
        self.assertEqual(self.state().status, "obsolete")
        self.store.data[f"tenants/{TENANT}/jobs/{self.identity}"] = before
        self.embeddings.hook = lambda: None
        self.deliver()
        self.assertEqual(len(self.embeddings.calls), 1)
        self.assertNotIn(self.vector_path, self.store.data)


class PurgeSummaryRaceTests(SummaryFixture):
    def test_inflight_summary_and_restored_old_job_never_recreate_summary(self) -> None:
        self.repo._sources = CanonicalSources(SavedMessages())
        self.store.data[MESSAGE]["created_by"] = USER
        for identity, kind in ((PERSON, "person"), (LIGHT, "topic")):
            self.repo.create_entity(
                context(),
                identity,
                entity_values(kind=kind, visibility="private", display_policy="never"),
            )
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(
                visibility="private",
                display_policy="never",
                source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}],
            ),
        )
        self.identity = self.handler.schedule(
            lambda tx: self.repo.summaries(tx, context()), CONVERSATION
        )
        from brainoid_memory.summaries import summary_path

        scheduled = self.jobs.get(TENANT, self.identity)
        assert scheduled is not None
        self.path = summary_path(TENANT, CONVERSATION, scheduled.operation_id)
        ledger = DeletionLedger(MemoryObjects())
        before = copy.deepcopy(self.store.data[f"tenants/{TENANT}/jobs/{self.identity}"])

        def forget() -> None:
            operation = ForgetMemories(self.jobs.store, ledger, now=lambda: self.now).forget(
                lambda tx: self.repo.facts(tx, context()),
                ForgetCreate(kind="forget_memory", target_id=FACT, expected_version=1),
                "purge-inflight-summary",
            )
            purge_now(self.store, self.jobs, ledger, operation.operation_id)

        self.model.hook = forget
        self.deliver()
        self.assertNotIn(self.path, self.store.data)
        self.store.data[f"tenants/{TENANT}/jobs/{self.identity}"] = before
        self.model.hook = lambda: None
        self.deliver()
        self.assertEqual(len(self.model.requests), 1)
        self.assertNotIn(self.path, self.store.data)
