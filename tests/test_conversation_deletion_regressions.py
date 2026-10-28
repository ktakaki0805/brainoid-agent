"""R1-F1/F2/F3: erased sources, reservation writers and orphaned copies."""

import copy
import unittest
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import patch

import test_conversation_deletion as deletion_tests
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import tombstone_path
from brainoid_contracts.transactions import Transaction
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.dto import MemoryCreate
from brainoid_memory.forget import DeleteConversation, ForgetCreate, operation_path
from brainoid_memory.mutations import MemoryMutations, effect_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.control import cancellation
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import KIND as RUN_KIND
from brainoid_worker.entry import Execution
from brainoid_worker.purge_authority import KIND
from fakes.memory_repository import (
    CONVERSATION,
    FACT,
    NOW,
    OTHER,
    SOURCE,
    TENANT,
    USER,
    context,
    fact_values,
)
from fakes.summaries import BASE, MESSAGE, summary_data
from test_memory_purge import PurgeFixture


class ConversationProvenanceTests(PurgeFixture):
    def setUp(self) -> None:
        super().setUp()
        data = summary_data()
        data[BASE].update(
            visibility="shared",
            display_policy="shared_display",
            owner_user_id=None,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        data[MESSAGE]["created_by"] = USER
        self.store.data.update({p: d for p, d in data.items() if p.startswith(BASE)})
        other = f"tenants/{TENANT}/conversations/{OTHER}"
        self.store.data[other] = {**data[BASE], "id": OTHER}
        self.store.data[other + "/messages/" + OTHER] = {
            **data[MESSAGE],
            "id": OTHER,
            "conversation_id": OTHER,
        }
        self.repo._sources = CanonicalSources(SavedMessages())

    def fact(self, identity: ResourceId, source: ResourceId = SOURCE) -> None:
        self.repo.create_fact(
            context(),
            identity,
            fact_values(source_refs=[{"kind": "message", "resource_id": source, "version": 1}]),
        )

    def accept_body(self, body: ForgetCreate | DeleteConversation, key: str) -> None:
        self.operation = self.acceptor.forget(lambda tx: self.repo.facts(tx, context()), body, key)
        self.job_id = job_id(TENANT, KIND, self.operation.operation_id)
        self.op_path = operation_path(TENANT, self.operation.operation_id)

    def forget_first(self, *, drain: bool = True) -> None:
        self.accept_body(
            ForgetCreate(
                kind="forget_memory",
                target_id=FACT,
                expected_version=1,
                source_policy="delete_source",
            ),
            "first-message-forget-key",
        )
        self.first = (self.operation, self.job_id, self.op_path)
        if drain:
            self.drain()
            self.assertNotIn(MESSAGE, self.store.data)

    def conversation_delete(self, *, include: bool = True) -> None:
        self.accept_body(
            DeleteConversation(
                kind="delete_resource",
                target_type="conversation",
                target_id=CONVERSATION,
                expected_version=1,
                include_derived_memories=include,
            ),
            "second-conversation-delete-key",
        )

    def pending(self) -> None:
        for _ in range(3):
            self.dispatcher.recover(TENANT)
            self.queue.drain(self.worker)
            self.now += timedelta(seconds=10)
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")

    def test_prior_forget_erases_all_remaining_derived_facts_and_unrelated_stays(self) -> None:
        for identity in (FACT, SOURCE, CONVERSATION):
            self.fact(identity)
        self.fact(OTHER, OTHER)
        self.forget_first()
        self.conversation_delete()
        self.drain()
        for identity in (FACT, SOURCE, CONVERSATION):
            self.assertNotIn(f"tenants/{TENANT}/facts/{identity}", self.store.data)
        self.assertIn(f"tenants/{TENANT}/facts/{OTHER}", self.store.data)
        self.assertNotIn(BASE, self.store.data)

    def test_pending_prior_forget_conversation_purge_first_then_replay(self) -> None:
        self.fact(FACT)
        self.fact(SOURCE)
        self.forget_first(drain=False)
        self.conversation_delete()
        self.drain()
        self.assertNotIn(self.fact_path, self.store.data)
        self.assertNotIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)
        self.operation, self.job_id, self.op_path = self.first
        self.drain()

    def test_prior_forget_include_false_keeps_remaining_derived_fact(self) -> None:
        self.fact(FACT)
        self.fact(SOURCE)
        self.forget_first()
        self.conversation_delete(include=False)
        self.drain()
        self.assertIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)
        self.assertNotIn(BASE, self.store.data)

    def test_missing_proof_keeps_pending_cleans_known_scope_and_recovers_after_root_gone(
        self,
    ) -> None:
        self.fact(FACT)
        self.fact(SOURCE)
        self.forget_first()
        marker_path = tombstone_path(TENANT, "messages", SOURCE, CONVERSATION)
        marker = self.store.data.pop(marker_path)
        self.conversation_delete()
        self.pending()
        self.assertNotIn(BASE, self.store.data)
        self.assertIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)
        self.store.data[marker_path] = marker
        self.drain()
        self.assertNotIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)

    def test_wrong_parent_tenant_version_and_unaccepted_marker_do_not_authorize_erasure(
        self,
    ) -> None:
        self.fact(FACT)
        self.fact(SOURCE)
        self.forget_first()
        marker_path = tombstone_path(TENANT, "messages", SOURCE, CONVERSATION)
        original = copy.deepcopy(self.store.data[marker_path])
        first_path = self.first[2]
        saved = copy.deepcopy(self.store.data[first_path])
        self.conversation_delete()
        for defect in ("parent", "tenant", "version", "unaccepted"):
            with self.subTest(defect=defect):
                self.store.data[marker_path] = copy.deepcopy(original)
                self.store.data[first_path] = copy.deepcopy(saved)
                if defect == "parent":
                    self.store.data[marker_path]["target"]["parent_id"] = OTHER
                if defect == "tenant":
                    self.store.data[first_path]["tenant_id"] = OTHER
                if defect == "version":
                    self.store.data[marker_path]["target"]["version"] = 99
                if defect == "unaccepted":
                    self.store.data.pop(first_path)
                self.pending()
                self.assertIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)
        self.store.data[marker_path] = original
        self.store.data[first_path] = saved
        self.drain()

    def test_future_source_version_is_unknown_not_unrelated(self) -> None:
        self.fact(SOURCE)
        self.store.data[f"tenants/{TENANT}/facts/{SOURCE}"]["source_refs"][0]["version"] = 99
        self.conversation_delete()
        self.pending()
        self.assertNotIn(BASE, self.store.data)
        self.assertIn(f"tenants/{TENANT}/facts/{SOURCE}", self.store.data)

    def effect(self, *, previous: bool = False, source: ResourceId = SOURCE) -> str:
        if previous:
            self.fact(SOURCE)
        values = fact_values(
            source_refs=[
                {"kind": "message", "resource_id": OTHER if previous else source, "version": 1}
            ]
        )
        body = MemoryCreate.model_validate(
            {
                k: v
                for k, v in values.model_dump(exclude_none=True).items()
                if k in MemoryCreate.model_fields
            }
        )
        MemoryMutations(self.repo._store, self.normal, now=lambda: self.now).mutate(
            lambda tx: self.repo.facts(tx, context()),
            body,
            "canonical-effect-request-key",
            effect=FACT,
            predecessor=SOURCE if previous else None,
            expected=1 if previous else None,
        )
        return effect_path(TENANT, FACT)

    def test_orphan_record_and_children_erased_with_source_already_forgotten(self) -> None:
        receipt = self.effect()
        self.fact(CONVERSATION)
        self.accept_body(
            ForgetCreate(
                kind="forget_memory",
                target_id=CONVERSATION,
                expected_version=1,
                source_policy="delete_source",
            ),
            "source-forget-with-effect-key",
        )
        self.drain()
        self.assertNotIn(MESSAGE, self.store.data)
        self.store.data.pop(self.fact_path)
        self.store.data[receipt + "/copies/one"] = {"tenant_id": TENANT, "body": "receipt-child"}
        original = copy.deepcopy(self.store.data[receipt])
        foreign = f"tenants/{OTHER}/memory_effects/{FACT}"
        self.store.data[foreign] = copy.deepcopy(original)
        self.conversation_delete()
        self.drain()
        self.assertFalse(any(p == receipt or p.startswith(receipt + "/") for p in self.store.data))
        self.assertIn(foreign, self.store.data)
        # Restore only the same immutable receipt and child; restart old progress.
        self.store.data[receipt] = original
        self.store.data[receipt + "/copies/restored"] = {"tenant_id": TENANT, "body": "restored"}
        self.store.data[self.op_path]["purge"]["locally_verified"] = False
        self.assertTrue(self.jobs.revalidate_completed(TENANT, self.job_id))
        self.drain()
        self.assertFalse(any(p == receipt or p.startswith(receipt + "/") for p in self.store.data))

    def test_previous_copy_erased_without_selecting_unrelated_current_fact_or_vector(self) -> None:
        receipt = self.effect(previous=True)
        self.store.data.pop(f"tenants/{TENANT}/facts/{SOURCE}")
        current = copy.deepcopy(self.store.data[self.fact_path])
        vector = f"tenants/{TENANT}/memory_vectors_{'a' * 32}/{FACT}"
        self.store.data[vector] = {"id": FACT, "tenant_id": TENANT, "embedding": [1]}
        self.conversation_delete()
        self.drain()
        self.assertNotIn(receipt, self.store.data)
        self.assertEqual(self.store.data[self.fact_path], current)
        self.assertIn(vector, self.store.data)
        self.assertNotIn(
            operation_path(TENANT, self.operation.operation_id) + "/purge_facts/" + FACT,
            self.store.data,
        )

    def test_unknown_orphan_source_does_not_complete_or_erase_other_conversation_receipt(
        self,
    ) -> None:
        receipt = self.effect()
        self.store.data.pop(self.fact_path)
        self.store.data.pop(MESSAGE)
        self.conversation_delete()
        self.pending()
        self.assertIn(receipt, self.store.data)
        self.assertNotIn(BASE, self.store.data)

    def test_selected_receipt_ghost_parent_resume_uses_copy_proof_only(self) -> None:
        receipt = self.effect()
        self.store.data.pop(self.fact_path)
        child = receipt + "/children/one"
        self.store.data[child] = {"tenant_id": TENANT, "body": "child"}
        self.conversation_delete()
        execution = self.acquire()
        proof = self.op_path + "/purge_effects/" + FACT
        for _ in range(1000):
            self.handler.step(execution)
            if proof in self.store.data:
                break
        self.assertIn(proof, self.store.data)
        self.store.data.pop(receipt)
        self.now += timedelta(minutes=2)
        self.drain()
        self.assertNotIn(child, self.store.data)
        self.assertNotIn(tombstone_path(TENANT, "facts", FACT), self.store.data)

    def test_record_copy_cannot_delete_different_restored_current_fact_or_vector(self) -> None:
        receipt = self.effect()
        self.fact(OTHER, OTHER)
        current = {**self.store.data[f"tenants/{TENANT}/facts/{OTHER}"], "id": FACT}
        self.store.data[self.fact_path] = current
        vector = f"tenants/{TENANT}/memory_vectors_{'b' * 32}/{FACT}"
        self.store.data[vector] = {"tenant_id": TENANT, "id": FACT, "embedding": [2]}
        self.conversation_delete()
        self.drain()
        self.assertNotIn(receipt, self.store.data)
        self.assertEqual(self.store.data[self.fact_path], current)
        self.assertIn(vector, self.store.data)

    def test_receipt_from_other_conversation_is_retained(self) -> None:
        receipt = self.effect(source=OTHER)
        original = copy.deepcopy(self.store.data[receipt])
        self.conversation_delete()
        self.drain()
        self.assertEqual(self.store.data[receipt], original)


class ConversationReservationTests(unittest.TestCase):
    def fixture(self) -> deletion_tests.ConversationDeletionHTTPTests:
        fixture = deletion_tests.ConversationDeletionHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_accepted_turn_cannot_finish_between_reservation_and_append(self) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = ResourceId(f.turn(conversation).json()["run_id"])
        path = f"tenants/{f.tenant}/conversations/{conversation}"
        append = f.deletion.append

        def racing(intent: DeletionIntent) -> None:
            f.execute(run)
            self.assertEqual(f.store.data[path]["version"], 1)
            self.assertNotEqual(
                f.store.data[f"tenants/{f.tenant}/runs/{run}"]["status"], "completed"
            )
            append(intent)

        with patch.object(f.deletion, "append", side_effect=racing):
            result = f.delete(conversation)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(f.delete(conversation).json(), result.json())
        self.assertEqual(f.turn(conversation, key="after-delete-late-turn-key").status_code, 404)
        self.assertFalse(f.model.requests)

    def test_completed_failed_and_cancel_commit_are_fenced_after_reservation(self) -> None:
        for action in ("completed", "failed", "cancelled"):
            for after in (False, True):
                with self.subTest(action=action, after_append=after):
                    self.commit_race(action, after)

    def commit_race(self, action: str, after: bool) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = ResourceId(f.turn(conversation).json()["run_id"])
        delivery = f.delivery(run)
        lease = f.jobs.acquire(delivery, known_handler=lambda kind: kind == RUN_KIND)
        assert lease is not None
        job, _, _ = f.handler._start(Execution(f.jobs, lease))
        append = f.deletion.append

        def racing(intent: DeletionIntent) -> None:
            if after:
                append(intent)
            before = copy.deepcopy(f.store.data[f"tenants/{f.tenant}/conversations/{conversation}"])
            with self.assertRaises(PolicyDenied):
                if action == "cancelled":
                    f.store.run(
                        lambda tx: cancellation(
                            tx, f.jobs, f.store.data[f"tenants/{f.tenant}/runs/{run}"]
                        )
                    )
                else:
                    f.store.run(
                        lambda tx: f.handler.commit(tx, job, {"status": action, "text": ""})
                    )
            self.assertEqual(
                f.store.data[f"tenants/{f.tenant}/conversations/{conversation}"], before
            )
            if not after:
                append(intent)

        with patch.object(f.deletion, "append", side_effect=racing):
            self.assertEqual(f.delete(conversation).status_code, 202)
        self.assertEqual(f.delete(conversation).status_code, 202)
        with self.assertRaises(PolicyDenied):
            f.store.run(
                lambda tx: cancellation(tx, f.jobs, f.store.data[f"tenants/{f.tenant}/runs/{run}"])
            )

    def test_ledger_failure_then_same_request_recovers_but_changed_request_cannot_take_lock(
        self,
    ) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = ResourceId(f.turn(conversation).json()["run_id"])
        with patch.object(f.deletion, "append", side_effect=LedgerError()):
            self.assertEqual(f.delete(conversation).status_code, 503)
        f.execute(run)
        self.assertEqual(
            f.store.data[f"tenants/{f.tenant}/conversations/{conversation}"]["version"], 1
        )
        self.assertEqual(
            f.delete(conversation, key="different-reservation-request").status_code, 409
        )
        self.assertEqual(f.delete(conversation).status_code, 202)

    def test_completion_before_reservation_requires_fresh_version_and_does_not_leave_lock(
        self,
    ) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = ResourceId(f.turn(conversation).json()["run_id"])
        f.execute(run)
        self.assertEqual(f.delete(conversation).status_code, 412)
        self.assertFalse(any("conversation_delete_reservations" in p for p in f.store.data))
        self.assertEqual(
            f.delete(
                conversation, key="fresh-confirmation-version-key", expected_version=2
            ).status_code,
            202,
        )

    def test_append_response_loss_keeps_fixed_intent_for_same_key_recovery(self) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        append = f.deletion.append

        def lose(intent: DeletionIntent) -> None:
            append(intent)
            raise LedgerError()

        with patch.object(f.deletion, "append", side_effect=lose):
            self.assertEqual(f.delete(conversation).status_code, 503)
        self.assertEqual(f.delete(conversation).status_code, 202)
        self.assertEqual(f.delete(conversation).status_code, 202)

    def test_acceptance_commit_response_loss_replays_same_operation(self) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = f.store.run
        lost = False

        def lose[T](work: Callable[[Transaction], T]) -> T:
            nonlocal lost
            result = run(work)
            if not lost and any(
                d.get("operation", {}).get("kind") == "delete_resource" and "/operations/" in p
                for p, d in f.store.data.items()
            ):
                lost = True
                raise PersistenceUnavailable()
            return result

        with patch.object(f.store, "run", side_effect=lose):
            # The API's generic DB boundary returns 500, never accepted/succeeded.
            self.assertEqual(f.delete(conversation).status_code, 500)
        self.assertTrue(lost)
        accepted = f.delete(conversation)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(f.delete(conversation).json(), accepted.json())

    def test_failure_or_cancel_before_reservation_can_be_reconfirmed(self) -> None:
        for action in ("failed", "cancelled"):
            with self.subTest(action=action):
                self.before_race(action)

    def before_race(self, action: str) -> None:
        f = self.fixture()
        conversation = f.conversation(private=True)
        run = ResourceId(f.turn(conversation).json()["run_id"])
        if action == "failed":
            f.model.failure = True
            f.execute(run)
        else:
            f.store.run(
                lambda tx: cancellation(tx, f.jobs, f.store.data[f"tenants/{f.tenant}/runs/{run}"])
            )
        self.assertEqual(f.delete(conversation).status_code, 412)
        self.assertFalse(any("conversation_delete_reservations" in p for p in f.store.data))
        self.assertEqual(
            f.delete(
                conversation, key="fresh-after-terminal-version-key", expected_version=2
            ).status_code,
            202,
        )
