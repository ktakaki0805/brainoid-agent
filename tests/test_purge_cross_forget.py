"""MEM08-R2: independent forget operations cannot discard each other's evidence."""

import copy
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch
from uuid import UUID

import test_purge_history
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import (
    Completed,
    ModelEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
    ToolRequested,
)
from brainoid_contracts import ResourceId
from brainoid_jobs import Delivery, LeaseLost, RetryableError
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, operation_path
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.memory_erasure import retained
from brainoid_worker.resume_run import proposal_path
from brainoid_worker.run_turn import RunTurn
from fakes.authz_ledger import MemoryObjects
from fakes.purge import FakePurgePages
from fakes.turn_model import Model

SENTINEL = "SYNTHETIC_CROSS_FORGET_A"


class CrossForgetModel:
    def __init__(self, calls: tuple[ToolCall, ...]) -> None:
        self.calls = calls
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, *, control: CallControl
    ) -> AsyncGenerator[ModelEvent]:
        self.requests.append(request)
        step = sum(m.role == "tool" for m in request.messages)
        call = self.calls[step]
        text = SENTINEL if SENTINEL in request.model_dump_json() else "Search first."
        yield TextDelta(text=text)
        yield ToolRequested(call=call)
        yield ModelResponse(
            message=ModelMessage(role="assistant", parts=(TextPart(text=text), call))
        )
        yield Completed(finish_reason="tool_calls")


class ReceiptFirstPages(FakePurgePages):
    """Exercise a legitimate different collection ordering, with two-item pages."""

    def children(
        self, parent: str, *, collections: bool, token: str = "", limit: int = 50
    ) -> tuple[list[str], str]:
        if collections and len(parent.split("/")) == 2:
            roots = {p.split("/")[2] for p in self.store.data if p.startswith(parent + "/")}
            paths = [
                parent + "/" + r for r in sorted(roots, key=lambda r: (r != "tool_executions", r))
            ]
            start = int(token or "0")
            selected = paths[start : start + limit]
            end = start + len(selected)
            return selected, str(end) if end < len(paths) else ""
        return super().children(parent, collections=collections, token=token, limit=limit)


class CrossForgetFixture(test_purge_history.PurgeHistoryFixture):
    def prepare(
        self,
        *,
        searched: bool,
        legacy: bool = False,
        narrow: bool = False,
        offset: int = 0,
        ledger: DeletionLedger | None = None,
    ) -> dict[str, Any]:
        # Stable canonical IDs make the one-result search select A, independently
        # of the graph's ID sort order; creation still uses the real repository.
        with patch(
            "brainoid_memory.mutations.uuid4",
            side_effect=[UUID(int=i + offset) for i in (101, 102, 103)],
        ):
            a = self.memory(value=SENTINEL)
            b = self.memory(value="SYNTHETIC_OTHER_B")
            unrelated = self.memory(value="UNRELATED_FACT_ORIGINAL")
        forget = ToolCall(
            call_key="forget_b",
            name="memory.forget",
            arguments_json=json.dumps({"memory_id": b.id, "expected_version": 1}),
        )
        calls = (
            (
                ToolCall(
                    call_key="search_a",
                    name="memory.search",
                    arguments_json=json.dumps(
                        {"query": SENTINEL, "limit": 1} if narrow else {"query": "Alex"}
                    ),
                ),
                forget,
            )
            if searched
            else (forget,)
        )
        self.configure_calls(calls)
        model = CrossForgetModel(calls)
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        ledger = ledger or DeletionLedger(MemoryObjects())
        self.handler.memory.deletion_ledger = ledger
        conversation = self.conversation()
        run_id = ResourceId(self.turn(conversation, text=f"Forget memory {b.id}").json()["run_id"])
        self.execute(run_id)
        runpath = f"tenants/{self.tenant}/runs/{run_id}"
        jobpath = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, 'run_turn', run_id)}"
        run = self.store.data[runpath]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["memory_records"], [])
        self.assertEqual(run["tool_bindings"], [])
        self.assertEqual(run["continuation"], [])
        self.assertIn(SENTINEL, model.requests[-1].model_dump_json())
        self.assertIn(SENTINEL, repr(self.store.data[jobpath]["checkpoints"]))
        if searched:
            self.assertIn(SENTINEL, run["streamed_text"])
        evidence = retained(run)
        assert evidence is not None
        self.assertTrue(evidence.complete)
        self.assertIn(a.id, {fact.memory_id for fact in evidence.facts})
        self.assertNotIn(SENTINEL, evidence.model_dump_json())
        if legacy:
            del run["purge_dependencies"]
        return {
            "a": a,
            "b": b,
            "model": model,
            "ledger": ledger,
            "b_operation": run["forget_operation_id"],
            "conversation": conversation,
            "run_id": run_id,
            "runpath": runpath,
            "jobpath": jobpath,
            "old_run": copy.deepcopy(run),
            "old_job": copy.deepcopy(self.store.data[jobpath]),
            "unrelated_path": f"tenants/{self.tenant}/facts/{unrelated.id}",
            "unrelated_id": unrelated.id,
            "unrelated": unrelated.model_dump(mode="json"),
            "users": {
                p: copy.deepcopy(d)
                for p, d in self.store.data.items()
                if "/messages/" in p and d.get("role") == "user"
            },
        }

    def assert_erased(self, case: dict[str, Any]) -> None:
        self.assertNotIn(SENTINEL, repr(self.store.data))
        self.assertEqual(self.store.data[case["runpath"]]["status"], "failed")
        self.assertEqual(self.store.data[case["jobpath"]]["checkpoints"], [])
        self.assertEqual(self.store.data[case["jobpath"]]["result"], None)
        for path, raw in case["users"].items():
            self.assertEqual(self.store.data[path], raw)
        self.assertEqual(
            self.store.data[case["unrelated_path"]]["value"], "UNRELATED_FACT_ORIGINAL"
        )

    def replay(self, case: dict[str, Any]) -> None:
        model = case["model"]
        count = len(model.requests)
        self.store.data[case["runpath"]] = case["old_run"]
        self.store.data[case["jobpath"]] = case["old_job"]
        self.store.data[case["jobpath"]].update(
            status="pending", lease_expires_at=None, dispatch_after=case["old_job"]["created_at"]
        )
        self.execute(case["run_id"])
        self.assertEqual(len(model.requests), count)

    def case(self, *, searched: bool, b_first: bool = False, legacy: bool = False) -> None:
        case = self.prepare(searched=searched, legacy=legacy)
        if b_first:
            self.bind_purge(case["ledger"], case["b_operation"])
            self.finish_purge()
        self.accept_purge(case["a"].id, case["ledger"])
        for _ in range(15):
            self.purge.step(self.purge_execution)
        old = self.purge_execution
        before = copy.deepcopy(self.store.data[self.op_path]["purge"])
        self.clock.advance(120)
        self.purge_execution = self.acquire_purge()
        with self.assertRaises(LeaseLost):
            self.purge.step(old)
        self.assertEqual(
            self.store.data[self.op_path]["purge"], before | {"retry_reason": "lease_expired"}
        )
        self.finish_purge()
        self.assert_erased(case)
        self.assertEqual(
            self.store.data[operation_path(self.tenant, case["b_operation"])]["operation"][
                "status"
            ],
            "succeeded" if b_first else "pending",
        )
        processed = self.store.data[self.op_path]["purge"]["processed"]
        self.assertIsNone(
            self.purge_jobs.prepare_dispatch(self.tenant, self.purge_id, queue_name="memory_purge")
        )
        self.assertEqual(self.store.data[self.op_path]["purge"]["processed"], processed)
        # An independently admitted later turn has no stale model context.
        independent_model = Model()
        independent_model.text = "INDEPENDENT_AFTER_CROSS_FORGET"
        self.handler.model = independent_model
        self.store.data[f"tenants/{self.tenant}/agents/{self.agent_id}"]["allowed_tools"] = []
        response = self.turn(
            case["conversation"], key="post-purge-independent", text="zzz_no_match"
        )
        self.assertEqual(response.status_code, 202, response.text)
        fresh = ResourceId(response.json()["run_id"])
        self.execute(fresh)
        self.assertEqual(self.get_run(fresh).json()["status"], "completed")
        self.assertNotIn(SENTINEL, repr(independent_model.requests))
        # Replaying the finished purge cannot select the independently admitted run.
        self.assertIn(f"tenants/{self.tenant}/runs/{fresh}", self.store.data)
        self.handler.model = case["model"]
        self.replay(case)


class CrossForgetTests(CrossForgetFixture):
    def test_direct_context_a_completes_while_b_pending(self) -> None:
        self.case(searched=False)

    def test_search_a_completes_while_b_pending(self) -> None:
        self.case(searched=True)

    def test_b_first_retains_other_fact_dependencies(self) -> None:
        self.case(searched=True, b_first=True)

    def test_alternating_a_b_steps_converge_without_rewriting_the_run_marker(self) -> None:
        case = self.prepare(searched=True)
        self.bind_purge(case["ledger"], case["b_operation"])
        b = self.purge, self.purge_jobs, self.purge_execution, self.op_path
        self.accept_purge(case["a"].id, case["ledger"])
        a = self.purge, self.purge_jobs, self.purge_execution, self.op_path
        pending = [a, b]
        markers = set()
        for _ in range(5000):
            for resource, jobs, execution, path in pending.copy():
                if resource.step(execution):
                    self.assertTrue(
                        jobs.complete(execution.lease, {"purged": True}, resource.commit)
                    )
                    pending.remove((resource, jobs, execution, path))
                run = self.store.data[case["runpath"]]
                if run.get("purged_by"):
                    markers.add(run["purged_by"])
            if not pending:
                break
        self.assertFalse(pending, "Overlapping purges did not converge")
        self.assertEqual(len(markers), 1)
        for _, _, _, path in (a, b):
            self.assertEqual(self.store.data[path]["operation"]["status"], "succeeded")
        self.assert_erased(case)

    def test_legacy_search_migrates_partial_receipt_before_erasing_it(self) -> None:
        self.case(searched=True, legacy=True)

    def test_receipt_first_partial_evidence_never_claims_missing_direct_inputs_complete(
        self,
    ) -> None:
        case = self.prepare(searched=True, legacy=True, narrow=True)
        self.accept_purge(case["a"].id, case["ledger"])
        self.purge.pages = ReceiptFirstPages(self.pages.store)
        for _ in range(300):
            self.purge.step(self.purge_execution)
            run = self.store.data[case["runpath"]]
            evidence = retained(run)
            if evidence is not None:
                break
        else:
            self.fail("Receipt did not atomically preserve its dependency")
        assert evidence is not None
        self.assertFalse(evidence.complete)
        self.assertEqual({f.memory_id for f in evidence.facts}, {case["a"].id})
        self.assertEqual(run["status"], "completed")
        # The legacy receipt cannot prove absence of a different direct-context Fact.
        other_ledger = case["ledger"]
        self.memories.deletion_ledger = other_ledger
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(kind="forget_memory", target_id=case["unrelated_id"], expected_version=1),
            "partial-evidence-third-fact",
        )
        a = self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path
        self.bind_purge(other_ledger, operation.operation_id)
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        self.assertEqual(
            self.store.data[self.op_path]["purge"]["retry_reason"], "legacy_provenance_missing"
        )
        c = self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path
        self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path = a
        old = self.purge_execution
        self.clock.advance(120)
        self.purge_execution = self.acquire_purge()
        with self.assertRaises(LeaseLost):
            self.purge.step(old)
        self.finish_purge()
        self.assertNotIn(SENTINEL, repr(self.store.data))
        # Once A has erased this run and verified every copy, no missing payload
        # remains for C; it may resume without depending on B's pending operation.
        self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path = c
        self.purge_execution = self.acquire_purge()
        self.finish_purge()
        self.assertEqual(
            self.store.data[operation_path(self.tenant, case["b_operation"])]["operation"][
                "status"
            ],
            "pending",
        )

    def test_foreign_receipt_binding_never_authorizes_run_or_receipt_erasure(self) -> None:
        case = self.prepare(searched=True, legacy=True)
        receipt = next(
            d
            for p, d in self.store.data.items()
            if "/tool_executions/" in p and d.get("name") == "memory.search"
        )
        receipt["authority"]["run_id"] = str(case["a"].id)
        self.accept_purge(case["a"].id, case["ledger"])
        with self.assertRaisesRegex(ValueError, "Invalid erasure receipt binding"):
            self.purge.run(self.purge_execution)
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        self.assertIn(SENTINEL, repr(self.store.data[case["jobpath"]]))

    def test_legacy_direct_context_waits_then_resumes_after_accepted_b_purge(self) -> None:
        case = self.prepare(searched=False, legacy=True)
        self.accept_purge(case["a"].id, case["ledger"])
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        saved = copy.deepcopy(self.store.data[self.op_path])
        self.assertEqual(saved["purge"]["retry_reason"], "legacy_provenance_missing")
        self.assertFalse(saved["purge"]["verified"])
        self.assertEqual(set(saved["purge"]["receipts"]), {"cache", "export"})
        self.assertTrue(saved["purge"]["locally_verified"])
        self.assertIn(SENTINEL, repr(self.store.data[case["jobpath"]]["checkpoints"]))
        a_purge, a_jobs, a_execution, a_path = (
            self.purge,
            self.purge_jobs,
            self.purge_execution,
            self.op_path,
        )
        a_jobs.fail(a_execution.lease, retry=True)
        self.bind_purge(case["ledger"], case["b_operation"])
        self.finish_purge()
        self.purge, self.purge_jobs, self.op_path = a_purge, a_jobs, a_path
        self.purge_id = a_execution.lease.job_id
        self.clock.advance(120)
        self.purge_execution = self.acquire_purge()
        self.assertEqual(self.store.data[a_path]["purge"]["processed"], saved["purge"]["processed"])
        self.finish_purge()
        self.assert_erased(case)
        self.replay(case)

    def test_two_legacy_self_forgets_make_progress_and_converge_with_interleaved_pages(
        self,
    ) -> None:
        first = self.prepare(searched=False, legacy=True)
        second = self.prepare(searched=False, legacy=True, offset=100, ledger=first["ledger"])
        pending: list[dict[str, Any]] = []
        for case in (first, second):
            self.bind_purge(case["ledger"], case["b_operation"])
            pending.append(
                {
                    "resource": self.purge,
                    "jobs": self.purge_jobs,
                    "execution": self.purge_execution,
                    "path": self.op_path,
                    "job_id": self.purge_id,
                }
            )
        states = pending.copy()
        blocked = 0
        for _ in range(5000):
            for state in pending.copy():
                jobs, resource, execution = state["jobs"], state["resource"], state["execution"]
                try:
                    if resource.step(execution):
                        self.assertTrue(
                            jobs.complete(execution.lease, {"purged": True}, resource.commit)
                        )
                        pending.remove(state)
                except RetryableError:
                    blocked += 1
                    jobs.fail(execution.lease, retry=True)
                    self.clock.advance(120)
                except LeaseLost:
                    pass
                if state in pending:
                    job = jobs.get(self.tenant, state["job_id"])
                    if job.status != "running" or job.lease_expires_at <= self.clock.now():
                        delivery = jobs.prepare_dispatch(
                            self.tenant, state["job_id"], queue_name="memory_purge"
                        )
                        lease = jobs.acquire(
                            delivery, known_handler=lambda kind: kind == "memory_purge"
                        )
                        self.assertIsNotNone(lease)
                        state["execution"] = Execution(jobs, lease)
            if not pending:
                break
        self.assertFalse(
            pending, "Legacy operations mutually blocked each other's accepted cleanup"
        )
        self.assertGreater(blocked, 0)
        for state, case in zip(states, (first, second), strict=True):
            saved = self.store.data[state["path"]]
            self.assertEqual(saved["operation"]["status"], "succeeded")
            self.assertTrue(saved["purge"]["locally_verified"])
            self.assertEqual(self.store.data[case["jobpath"]]["checkpoints"], [])
            self.assertNotIn("streamed_text", self.store.data[case["runpath"]])
            processed = saved["purge"]["processed"]
            self.assertIsNone(
                state["jobs"].prepare_dispatch(
                    self.tenant, state["job_id"], queue_name="memory_purge"
                )
            )
            self.assertEqual(self.store.data[state["path"]]["purge"]["processed"], processed)

    def test_old_completed_purge_is_rescanned_instead_of_trusting_its_completion_flag(self) -> None:
        case = self.prepare(searched=False, legacy=True)
        self.bind_purge(case["ledger"], case["b_operation"])
        self.finish_purge()
        b_jobs, b_resource, b_id, b_path = self.purge_jobs, self.purge, self.purge_id, self.op_path
        previous_count = self.store.data[b_path]["purge"]["processed"]
        old_delivery = Delivery(
            tenant_id=self.tenant,
            job_id=b_id,
            dispatch_generation=b_jobs.get(self.tenant, b_id).dispatch_generation,  # type: ignore[union-attr]
        )
        # Old completion shrank the run but missed the actual proposal inventory.
        self.store.data[b_path]["purge"].pop("locally_verified")
        run = self.store.data[case["runpath"]]
        for field in ("purge_dependencies", "forget_operation_id", "tool_call_ids"):
            run.pop(field, None)
        proposal = proposal_path(self.tenant, ResourceId(str(UUID(int=9999))))
        self.store.data[proposal] = {
            "tenant_id": self.tenant,
            "run_id": case["run_id"],
            "request": {"call": {"arguments_json": json.dumps({"text": SENTINEL})}},
        }
        self.store.data[proposal + "/copies/missing/parts/body"] = {"text": SENTINEL}
        self.accept_purge(case["a"].id, case["ledger"])
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        self.assertIn(proposal, self.store.data)
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        self.assertFalse(
            b_jobs.revalidate_completed(self.tenant, ResourceId(case["old_job"]["id"]))
        )
        self.assertTrue(b_jobs.revalidate_completed(self.tenant, b_id))
        self.assertIsNone(
            b_jobs.acquire(old_delivery, known_handler=lambda kind: kind == "memory_purge")
        )
        self.assertFalse(b_jobs.revalidate_completed(self.tenant, b_id))
        self.assertEqual(self.store.data[b_path]["purge"]["processed"], previous_count)
        self.assertEqual(self.store.data[b_path]["purge"]["receipts"], {})
        delivery = b_jobs.prepare_dispatch(self.tenant, b_id, queue_name="memory_purge")
        assert delivery is not None
        lease = b_jobs.acquire(delivery, known_handler=lambda kind: kind == "memory_purge")
        assert lease is not None
        result = b_resource.run(Execution(b_jobs, lease))
        self.assertTrue(b_jobs.complete(lease, result, b_resource.commit))
        self.assertFalse(any(p.startswith(proposal) for p in self.store.data))
        self.assertTrue(self.store.data[b_path]["purge"]["locally_verified"])
        self.assertGreaterEqual(self.store.data[b_path]["purge"]["processed"], previous_count + 2)
        self.finish_purge()
        self.assert_erased(case)

    def test_failed_hook_or_unaccepted_verification_flag_cannot_resolve_legacy_gap(self) -> None:
        case = self.prepare(searched=False, legacy=True)
        self.bind_purge(case["ledger"], case["b_operation"])
        self.purge.hooks["export"].fail = True  # type: ignore[attr-defined]
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        bpath = self.op_path
        self.assertFalse(self.store.data[bpath]["purge"].get("locally_verified", False))
        self.accept_purge(case["a"].id, case["ledger"])
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        # A forged completion flag and receipts do not replace independent B intent.
        self.store.data[bpath]["purge"].update(
            locally_verified=True, receipts={"cache": "fake", "export": "fake"}
        )
        self.purge_jobs.deletion_ledger = DeletionLedger(MemoryObjects())
        # Copy only A's accepted intent to this independent backend.
        intent = case["ledger"].get(
            ResourceId(self.store.data[self.op_path]["operation"]["operation_id"])
        )
        assert intent is not None
        self.purge_jobs.deletion_ledger.append(intent)
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        self.assertEqual(
            self.store.data[self.op_path]["purge"]["retry_reason"], "legacy_provenance_missing"
        )
