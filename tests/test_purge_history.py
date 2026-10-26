"""MEM08-R1 real admission/RunTurn history, confirmation and compatibility regressions."""

import copy
import json
from typing import cast

from brainoid_agent_core.types import ToolCall
from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import purge_barrier_path
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, operation_path
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import PurgeResource
from brainoid_worker.resume_run import proposal_path
from brainoid_worker.run_turn import RunTurn
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import GraphStore
from fakes.purge import FakeInvalidation, FakePurgePages
from fakes.run_confirmation import ConfirmationAuthority, ConfirmationEffect, ConfirmationModel
from test_scoped_retrieval import MemoryWorkerFixture


class PurgeHistoryFixture(MemoryWorkerFixture):
    def accept_purge(self, memory: ResourceId, ledger: DeletionLedger | None = None) -> None:
        ledger = ledger or DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(kind="forget_memory", target_id=memory, expected_version=1),
            "history-forget-key-001",
        )
        self.bind_purge(ledger, operation.operation_id)

    def bind_purge(self, ledger: DeletionLedger, operation_id: ResourceId) -> None:
        self.purge_jobs = PurgeJobs(self.jobs, ledger)
        self.pages = FakePurgePages(cast(GraphStore, self.store))
        self.purge = PurgeResource(
            self.purge_jobs,
            self.pages,
            hooks={"cache": FakeInvalidation(), "export": FakeInvalidation()},
            page_size=2,
            step_budget=10000,
        )
        self.purge_id = job_id(self.tenant, KIND, operation_id)
        self.op_path = operation_path(self.tenant, operation_id)
        self.purge_jobs.activate(self.tenant, self.purge_id)
        self.purge_execution = self.acquire_purge()

    def acquire_purge(self) -> Execution:
        delivery = self.purge_jobs.prepare_dispatch(self.tenant, self.purge_id, queue_name=KIND)
        assert delivery is not None
        lease = self.purge_jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        return Execution(self.purge_jobs, lease)

    def finish_purge(self) -> None:
        result = self.purge.run(self.purge_execution)
        self.assertTrue(
            self.purge_jobs.complete(self.purge_execution.lease, result, self.purge.commit)
        )
        self.assertEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")


class PurgeHistoryTests(PurgeHistoryFixture):
    def history_chain(self, conversation: ResourceId) -> tuple[ResourceId, ResourceId]:
        self.model.text = "PURGE_DIRECT_BODY"
        first = ResourceId(self.turn(conversation, text="Alex").json()["run_id"])
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        self.model.text = "PURGE_TRANSITIVE_BODY"
        second = ResourceId(
            self.turn(conversation, key="followup-" + conversation, text="zzz_no_match").json()[
                "run_id"
            ]
        )
        self.execute(second)
        self.assertEqual(self.get_run(second).json()["status"], "completed")
        self.assertIn("PURGE_DIRECT_BODY", repr(self.model.requests[-1].messages))
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/runs/{second}"]["memory_records"], []
        )
        return first, second

    def history_case(self, *, legacy: bool) -> None:
        memory = self.memory(value="FACT_MEMORY_SENTINEL")
        first_conversation, second_conversation = self.conversation(), self.conversation()
        first, followup = self.history_chain(first_conversation)
        other_first, other_followup = self.history_chain(second_conversation)
        run_path = f"tenants/{self.tenant}/runs/{followup}"
        job_path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, 'run_turn', followup)}"
        old_run, old_job = (
            copy.deepcopy(self.store.data[run_path]),
            copy.deepcopy(self.store.data[job_path]),
        )
        if legacy:
            for identity in (first, followup, other_first, other_followup):
                run = self.store.data[f"tenants/{self.tenant}/runs/{identity}"]
                run.pop("purge_epoch")
                run.pop("history_run_ids")
        user_originals = {
            p: copy.deepcopy(d)
            for p, d in self.store.data.items()
            if "/messages/" in p and d.get("role") == "user"
        }
        self.accept_purge(memory.id)
        if legacy:
            self.store.data[self.op_path].pop("purge_admission_epoch")
            del self.store.data[purge_barrier_path(self.tenant)]
        for _ in range(3000):
            self.purge.step(self.purge_execution)
            if "PURGE_TRANSITIVE_BODY" not in repr(
                self.store.data
            ) and "PURGE_DIRECT_BODY" not in repr(self.store.data):
                break
        else:
            self.fail("History closure did not physically erase both conversations")
        # New independent turn, same fake timestamp, while old purge is incomplete.
        self.model.text = "INDEPENDENT_AFTER_ACCEPTANCE"
        independent = ResourceId(
            self.turn(
                first_conversation, key="independent-after-purge", text="zzz_no_match"
            ).json()["run_id"]
        )
        self.execute(independent)
        self.assertEqual(self.get_run(independent).json()["status"], "completed")
        fresh = self.store.data[f"tenants/{self.tenant}/runs/{independent}"]
        self.assertEqual(fresh["history_run_ids"], [])
        self.assertEqual(fresh["created_at"], old_run["created_at"])
        # Crash/resume after provenance bodies were removed; markers survive.
        self.clock.advance(120)
        self.purge_execution = self.acquire_purge()
        self.finish_purge()
        self.assertIn("INDEPENDENT_AFTER_ACCEPTANCE", repr(self.store.data))
        for identity in (first, followup, other_first, other_followup):
            self.assertEqual(
                self.store.data[f"tenants/{self.tenant}/runs/{identity}"]["status"], "failed"
            )
        for path, document in user_originals.items():
            self.assertEqual(self.store.data[path], document)
        self.assertIn(
            f"tenants/{self.tenant}/conversations/{first_conversation}/messages/{fresh['input_message_id']}",
            self.store.data,
        )
        self.assertNotIn("PURGE_TRANSITIVE_BODY", repr(self.store.data))
        self.assertNotIn("PURGE_DIRECT_BODY", repr(self.store.data))
        calls = len(self.model.requests)
        self.store.data[run_path], self.store.data[job_path] = old_run, old_job
        self.store.data[job_path].update(
            status="pending", lease_expires_at=None, dispatch_after=old_job["created_at"]
        )
        self.execute(followup)
        self.assertEqual(len(self.model.requests), calls)

    def test_real_history_closure_across_conversations_and_same_time_new_turn(self) -> None:
        self.history_case(legacy=False)

    def test_legacy_run_receipt_and_new_derived_only_history_provenance(self) -> None:
        self.history_case(legacy=True)

    def test_real_confirmation_proposal_and_children_with_unrelated_proposal_preserved(
        self,
    ) -> None:
        memory = self.memory()
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = [
            "notes.delete"
        ]
        note = ResourceId(
            next(
                d["id"]
                for p, d in self.store.data.items()
                if p.startswith(f"tenants/{self.tenant}/notes/") and d["visibility"] == "shared"
            )
        )
        locations = []
        for text, key in (("Alex", "affected-proposal"), ("zzz_no_match", "unrelated-proposal")):
            conversation = self.conversation()
            run = ResourceId(self.turn(conversation, key=key, text=text).json()["run_id"])
            model, effect = ConfirmationModel(note), ConfirmationEffect()
            authority = ConfirmationAuthority(self.jobs, run)
            self.handler = RunTurn(
                self.jobs,
                model,
                credential_epoch=self.tokens.epoch,
                tool_registry=effect.registry,
                tool_authorizer=authority,
            )
            self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
            self.execute(run)
            saved = self.get_run(run).json()
            self.assertEqual(saved["status"], "waiting_confirmation")
            location = proposal_path(self.tenant, ResourceId(saved["waiting_proposal_id"]))
            self.assertIn("request", self.store.data[location])
            self.store.data[location + "/copies/missing/parts/one"] = {"body": key}
            locations.append(location)
        untouched = copy.deepcopy(self.store.data[locations[1]])
        self.accept_purge(memory.id)
        for _ in range(100):
            self.purge.step(self.purge_execution)
        self.clock.advance(120)
        self.purge_execution = self.acquire_purge()
        self.finish_purge()
        self.assertFalse(any(p.startswith(locations[0]) for p in self.store.data))
        self.assertEqual(self.store.data[locations[1]], untouched)
        self.assertIn(locations[1] + "/copies/missing/parts/one", self.store.data)

    def test_self_forget_proof_selects_streamed_copies_after_snapshots_are_cleared(self) -> None:
        memory = self.memory()
        self.configure_calls(
            (
                ToolCall(
                    call_key="forget",
                    name="memory.forget",
                    arguments_json=json.dumps({"memory_id": memory.id, "expected_version": 1}),
                ),
            )
        )
        ledger = DeletionLedger(MemoryObjects())
        self.handler.memory.deletion_ledger = ledger
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {memory.id}").json()["run_id"]
        )
        self.execute(run)
        record = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["memory_records"], [])
        self.assertEqual(record["tool_bindings"], [])
        self.assertIn("FIXTURE_stage_0", repr(self.store.data))
        self.bind_purge(ledger, ResourceId(record["forget_operation_id"]))
        self.finish_purge()
        self.assertNotIn("FIXTURE_stage_0", repr(self.store.data))
        self.assertEqual(self.store.data[f"tenants/{self.tenant}/runs/{run}"]["status"], "failed")

    def test_sequential_forgets_order_turns_with_monotonic_epoch(self) -> None:
        first_memory = self.memory()
        conversation = self.conversation()
        run1, _ = self.history_chain(conversation)
        self.accept_purge(first_memory.id)
        self.assertEqual(self.store.data[self.op_path]["purge_admission_epoch"], 1)
        self.finish_purge()
        second_memory = self.memory(value="SECOND_FACT")
        self.model.text = "SECOND_DERIVED_BODY"
        run2 = ResourceId(
            self.turn(conversation, key="second-epoch-turn", text="Alex").json()["run_id"]
        )
        self.execute(run2)
        self.assertEqual(self.get_run(run2).json()["status"], "completed")
        self.assertEqual(self.store.data[f"tenants/{self.tenant}/runs/{run2}"]["purge_epoch"], 1)
        # New key because the earlier request already used the helper's stable key.
        # Both acceptances use the deployment's same independent ledger backend.
        ledger = self.memories.deletion_ledger
        assert ledger is not None
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(kind="forget_memory", target_id=second_memory.id, expected_version=1),
            "second-epoch-forget",
        )
        self.bind_purge(ledger, operation.operation_id)
        self.assertEqual(self.store.data[self.op_path]["purge_admission_epoch"], 2)
        self.finish_purge()
        self.assertNotIn("SECOND_DERIVED_BODY", repr(self.store.data))
        self.assertEqual(self.store.data[f"tenants/{self.tenant}/runs/{run1}"]["purge_epoch"], 0)
