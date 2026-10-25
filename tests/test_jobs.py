"""BA-RUN-01: durable recovery, current authorization and side-effect fencing."""

import copy
import io
import json
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from uuid import uuid4

from brainoid_api.auth.authorization import policy_call
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import (
    AuthorizationUnavailable,
    Delivery,
    Job,
    Jobs,
    LeaseLost,
    OutcomeUnknown,
    RetryableError,
)
from brainoid_jobs.models import JobError
from brainoid_jobs.repository import path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import Change, ControlLedger, Entry, LedgerError, MemberTarget
from brainoid_policy.models import Action, ResourceKind, ResourceRef, Role, UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from brainoid_worker.app import create_app
from brainoid_worker.cloud_tasks import CloudTasksQueue, GoogleCaller
from brainoid_worker.dispatcher import Dispatcher, QueueUnavailable
from brainoid_worker.entry import CallerDenied, Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from fakes.authz_ledger import MemoryObjects
from fakes.runtime import FakeClock
from fastapi.testclient import TestClient
from pydantic import JsonValue
from test_common_mutations import MemoryStore

from scripts.demo_emulator import load_fixture


class JobStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.RLock()
        self.in_transaction = False

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        with self.lock:
            self.in_transaction = True
            try:
                return super().run(work)
            finally:
                self.in_transaction = False

    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]:
        with self.lock:
            return [
                (key, copy.deepcopy(value))
                for key, value in sorted(self.data.items())
                if key.rsplit("/", 1)[0] == collection
                and (after is None or key.rsplit("/", 1)[-1] > after)
            ][:limit]


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.run_hook: Callable[[Execution], None] = lambda execution: None
        self.effect_hook: Callable[[], None] = lambda: None
        self.use_effect = True

    def run(self, execution: Execution) -> JsonValue:
        self.run_hook(execution)
        if not self.use_effect:
            return "computed"

        def invoke(key: str) -> JsonValue:
            self.calls.append(key)
            self.effect_hook()
            return "provider_result"

        return execution.effect("effect-1", invoke)

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        tx.set(f"tenants/{job.tenant_id}/job_results/{job.id}", {"result": result})


class JobFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore()
        self.clock = FakeClock()
        self.objects = MemoryObjects()
        self.ledger = ControlLedger(self.objects)
        documents = load_fixture()["documents"]
        self.store.data = {d["path"]: d["fields"] for d in documents}
        note = next(d for d in documents if "/notes/" in d["path"])
        self.source_path = note["path"]
        self.tenant = ResourceId(note["fields"]["tenant_id"])
        self.ref = ResourceRef(
            tenant_id=self.tenant, kind=ResourceKind.NOTE, id=ResourceId(note["fields"]["id"])
        )
        self.subject = UserSubject(user_id=FirebaseUid("demo-a-member"))
        self.member_path = f"tenants/{self.tenant}/memberships/{self.subject.user_id}"
        change = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=0,
            authorization_version=1,
            effect="grant",
            target=MemberTarget(user_id=self.subject.user_id, role=Role.MEMBER, status="active"),
        )
        self.ledger.prepare(change)
        self.ledger.commit(change)
        self.store.data[self.member_path]["authorization_operation_id"] = change.operation_id
        self.jobs = Jobs(self.store, self.ledger, now=self.clock.now)
        self.queue = LocalQueue()
        self.dispatcher = Dispatcher(self.jobs, self.store, self.queue)
        self.handler = RecordingHandler()
        self.worker = Worker(self.jobs, LocalCaller(), {"fixture": self.handler})

    def add(
        self, *, kind: str = "fixture", max_attempts: int = 5, replacement: Document | None = None
    ) -> Job:
        operation = ResourceId(str(uuid4()))

        def work(tx: Transaction) -> Job:
            grant = transaction_policy(tx, ledger=self.ledger, now=self.clock.now).resource(
                self.tenant,
                self.subject,
                self.ref,
                action=Action.WRITE,
            )
            return self.jobs.add_pending(
                tx,
                operation_id=operation,
                kind=kind,
                grant=grant,
                max_attempts=max_attempts,
                source_replacement=replacement,
            )

        return self.store.run(work)

    def delivery(self, job: Job) -> Delivery:
        delivery = self.jobs.prepare_dispatch(job.tenant_id, job.id)
        assert delivery is not None
        return delivery

    def saved(self, job: Job) -> Job:
        saved = self.jobs.get(job.tenant_id, job.id)
        assert saved is not None
        return saved

    def result_path(self, job: Job) -> str:
        return f"tenants/{job.tenant_id}/job_results/{job.id}"


class JobsTests(JobFixture):
    def test_business_write_and_job_rollback_and_atomic_new_source_version(self) -> None:
        before = copy.deepcopy(self.store.data)

        def rollback(tx: Transaction) -> None:
            grant = transaction_policy(tx, ledger=self.ledger, now=self.clock.now).resource(
                self.tenant,
                self.subject,
                self.ref,
            )
            self.jobs.add_pending(
                tx, operation_id=ResourceId(str(uuid4())), kind="fixture", grant=grant
            )
            tx.set(self.source_path, {"invalid": True})
            raise RuntimeError("rollback")

        with self.assertRaises(RuntimeError):
            self.store.run(rollback)
        self.assertEqual(before, self.store.data)
        replacement = {**self.store.data[self.source_path], "version": 2, "body": "FIXTURE_UPDATED"}
        job = self.add(replacement=replacement)
        self.assertEqual(job.grant.resource.version, 2)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.saved(job).status, "succeeded")
        replacement["version"] = 3
        replacement["owner_user_id"] = "someone-else"
        with self.assertRaises((JobError, PolicyDenied)):
            self.add(replacement=replacement)

    def test_enqueue_before_after_failure_restart_and_retained_task_name(self) -> None:
        for after in (False, True):
            with self.subTest(after=after):
                job = self.add()

                def fail(delivery: Delivery) -> None:
                    raise QueueUnavailable()

                if after:
                    self.queue.after = fail
                else:
                    self.queue.before = fail
                page = self.dispatcher.recover(self.tenant)
                self.assertEqual(page.enqueue_errors, 1)
                self.queue.before = self.queue.after = lambda delivery: None
                restart = Dispatcher(
                    Jobs(self.store, self.ledger, now=self.clock.now), self.store, self.queue
                )
                restart.recover(self.tenant)
                delivery = next(v for v in self.queue.tasks.values() if v.job_id == job.id)
                self.assertEqual(delivery.dispatch_generation, 1)
                # Simulate Tasks deleting the delivery while retaining its dedupe name.
                self.queue.tasks.pop(delivery.task_id)
                self.clock.advance(120)
                restart.recover(self.tenant)
                self.queue.drain(self.worker)
                self.assertEqual(self.saved(job).status, "succeeded")
                self.assertEqual(self.saved(job).dispatch_generation, 2)

    def test_duplicate_delivery_one_lease_and_stale_generation_cannot_commit(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        with ThreadPoolExecutor(max_workers=8) as pool:
            leases = list(
                pool.map(
                    lambda _: self.jobs.acquire(delivery, known_handler=lambda _: True), range(8)
                )
            )
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        old = next(lease for lease in leases if lease is not None)
        self.clock.advance(60)
        newer = self.delivery(job)
        self.assertIsNone(self.jobs.acquire(delivery, known_handler=lambda _: True))
        current = self.jobs.acquire(newer, known_handler=lambda _: True)
        assert current is not None
        self.assertEqual(current.generation, old.generation + 1)
        self.assertFalse(self.jobs.complete(old, "stale", self.handler.commit))
        self.assertNotIn(self.result_path(job), self.store.data)
        self.assertTrue(self.jobs.complete(current, "current", self.handler.commit))

    def test_revocation_deletion_source_version_and_account_stop_before_io(self) -> None:
        changes = (
            (self.member_path, {"status": "revoked"}, "authorization_changed"),
            (self.source_path, {"deleted": True}, "source_deleted"),
            (self.source_path, {"version": 99}, "source_changed"),
            (self.source_path, {"deletion_epoch": 99}, "source_changed"),
            ("users/demo-a-member", {"status": "deleting"}, "account_changed"),
        )
        for target, update, reason in changes:
            with self.subTest(reason=reason):
                before = copy.deepcopy(self.store.data[target])
                job = self.add()
                delivery = self.delivery(job)
                self.store.data[target].update(update)
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
                saved = self.saved(job)
                self.assertEqual((saved.status, saved.reason), ("obsolete", reason))
                self.assertEqual(self.handler.calls, [])
                self.store.data[target] = before

    def test_pending_ledger_and_missing_tracked_ledger_deny_without_bootstrap_bypass(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        self.ledger.prepare(
            Change(
                operation_id=ResourceId(str(uuid4())),
                tenant_id=self.tenant,
                expected_version=1,
                authorization_version=2,
                effect="deny",
                target=MemberTarget(
                    user_id=self.subject.user_id, role=Role.MEMBER, status="revoked"
                ),
            )
        )
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertEqual(self.saved(job).reason, "authorization_changed")
        missing = Jobs(
            self.store,
            ControlLedger(MemoryObjects()),
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: missing.add_pending(
                    tx, operation_id=ResourceId(str(uuid4())), kind="fixture", grant=job.grant
                )
            )

    def test_unavailable_ledger_at_acquire_preserves_job_and_api_error_contract(self) -> None:
        job = self.add(max_attempts=1)
        delivery = self.delivery(job)
        before = self.saved(job)
        with patch.object(self.ledger, "latest", side_effect=LedgerError()):
            with TestClient(create_app(self.worker)) as client:
                for _ in range(2):
                    response = client.post(
                        "/tasks",
                        json=delivery.model_dump(mode="json"),
                        headers={"Authorization": LOCAL_AUTHORIZATION},
                    )
                    self.assertEqual((response.status_code, response.content), (503, b""))
            self.assertEqual(self.saved(job), before)
            self.assertEqual(self.handler.calls, [])
            with self.assertRaises(APIError) as denied:
                self.store.run(
                    lambda tx: policy_call(
                        lambda: transaction_policy(
                            tx,
                            ledger=self.ledger,
                            now=self.clock.now,
                        ).worker(job.grant)
                    )
                )
            self.assertEqual(denied.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual((self.saved(job).status, self.saved(job).attempts), ("succeeded", 1))
        self.assertEqual(len(self.handler.calls), 1)

    def test_unavailable_authority_during_execution_retries_without_exhausting_budget(self) -> None:
        for boundary in (
            "before_effect",
            "before_commit",
            "after_checkpoint",
            "handler_error",
            "retry_error",
        ):
            with self.subTest(boundary=boundary):
                self._assert_authority_outage_recovers(boundary)

    def _assert_authority_outage_recovers(self, boundary: str) -> None:
        job = self.add(max_attempts=1)
        delivery = self.delivery(job)
        handler = RecordingHandler()
        worker = Worker(self.jobs, LocalCaller(), {"fixture": handler})
        original_latest = self.ledger.latest
        available = True

        def latest(key: str) -> Entry | None:
            if not available:
                raise LedgerError()
            return original_latest(key)

        def interrupt(execution: Execution) -> None:
            nonlocal available
            available = False
            if boundary == "handler_error":
                raise ValueError("synthetic handler failure")
            if boundary == "retry_error":
                raise RetryableError()

        if boundary == "before_commit":
            handler.use_effect = False
        if boundary != "after_checkpoint":
            handler.run_hook = interrupt
        original_run = handler.run

        def run(execution: Execution) -> JsonValue:
            nonlocal available
            result = original_run(execution)
            if boundary == "after_checkpoint":
                available = False
            return result

        with (
            patch.object(self.ledger, "latest", side_effect=latest),
            patch.object(handler, "run", side_effect=run),
        ):
            with self.assertRaises(AuthorizationUnavailable):
                worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        saved = self.saved(job)
        self.assertEqual(
            (saved.status, saved.reason, saved.attempts),
            ("pending", "authorization_unavailable", 0),
        )
        self.assertNotIn(self.result_path(job), self.store.data)
        self.assertEqual(len(handler.calls), int(boundary == "after_checkpoint"))
        handler.run_hook = lambda execution: None
        self.clock.advance(5)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(worker)
        saved = self.saved(job)
        self.assertEqual(
            (saved.status, saved.attempts, saved.lease_generation), ("succeeded", 1, 2)
        )
        self.assertEqual(len(handler.calls), int(boundary != "before_commit"))

    def test_ledger_outage_after_external_call_keeps_unknown_checkpoint_without_reexecution(
        self,
    ) -> None:
        job = self.add()
        delivery = self.delivery(job)
        original_latest = self.ledger.latest
        available = True

        def latest(key: str) -> Entry | None:
            if not available:
                raise LedgerError()
            return original_latest(key)

        def interrupt() -> None:
            nonlocal available
            available = False

        self.handler.effect_hook = interrupt
        with patch.object(self.ledger, "latest", side_effect=latest):
            with self.assertRaises(AuthorizationUnavailable):
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        saved = self.saved(job)
        self.assertEqual((saved.status, saved.reason), ("failed", "outcome_unknown"))
        self.assertEqual(saved.checkpoints[0].state, "started")
        self.assertEqual(len(self.handler.calls), 1)
        self.assertNotIn(self.result_path(job), self.store.data)
        self.clock.advance(120)
        self.assertEqual(self.dispatcher.recover(self.tenant).dispatched, 0)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertEqual(len(self.handler.calls), 1)

    def test_corrupt_ledger_can_recover_and_stale_defer_cannot_release_new_lease(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        ledger_before = copy.deepcopy(self.objects.data)
        name = next(name for name in self.objects.data if "/intents/" in name)
        self.objects.data[name] = b"{}"
        with self.assertRaises(AuthorizationUnavailable):
            self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertEqual((self.saved(job).status, self.saved(job).attempts), ("pending", 0))
        self.objects.data = ledger_before
        old = self.jobs.acquire(delivery, known_handler=lambda _: True)
        assert old is not None
        self.clock.advance(60)
        current = self.jobs.acquire(self.delivery(job), known_handler=lambda _: True)
        assert current is not None
        before = self.saved(job)
        self.jobs.defer_unavailable(old, reason="authorization_unavailable")
        self.jobs.defer_unavailable(old, reason="persistence_unavailable")
        self.assertEqual(self.saved(job), before)
        self.assertTrue(self.jobs.complete(current, "current", self.handler.commit))

    def test_persistence_faults_and_lost_responses_reconcile_durable_boundaries(self) -> None:
        for boundary in (
            "acquire",
            "read",
            "start",
            "checkpoint",
            "commit_pure",
            "commit_checkpoint",
            "failure",
            "retry_failure",
        ):
            for after_commit in (False, True):
                with self.subTest(boundary=boundary, after_commit=after_commit):
                    self._assert_persistence_boundary(boundary, after_commit)

    def _assert_persistence_boundary(self, boundary: str, after_commit: bool) -> None:
        job = self.add(max_attempts=2 if boundary in {"acquire", "retry_failure"} else 1)
        delivery = self.delivery(job)
        handler = RecordingHandler()
        worker = Worker(self.jobs, LocalCaller(), {"fixture": handler})
        handler.use_effect = boundary != "commit_pure"
        if boundary in {"failure", "retry_failure"}:

            def fail(execution: Execution) -> None:
                if boundary == "retry_failure":
                    raise RetryableError()
                raise ValueError("synthetic permanent handler failure")

            handler.run_hook = fail
        armed = fired = False
        original_store = self.store.run

        def fault[T](work: Callable[[Transaction], T]) -> T:
            nonlocal fired
            if armed and not fired:
                fired = True
                if after_commit:
                    original_store(work)
                raise PersistenceUnavailable()
            return original_store(work)

        def arm() -> None:
            nonlocal armed
            armed = True

        targets = {
            "acquire": "acquire",
            "read": "check",
            "start": "start_effect",
            "commit_pure": "complete",
            "commit_checkpoint": "complete",
            "failure": "fail",
            "retry_failure": "fail",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.store, "run", side_effect=fault))
            if boundary == "checkpoint":
                handler.effect_hook = arm
            else:
                target = targets[boundary]
                original: Callable[..., object] = getattr(self.jobs, target)

                def interrupted(*args: object, **kwargs: object) -> object:
                    arm()
                    return original(*args, **kwargs)

                stack.enter_context(patch.object(self.jobs, target, side_effect=interrupted))
            client = stack.enter_context(TestClient(create_app(worker)))
            response = client.post(
                "/tasks",
                json=delivery.model_dump(mode="json"),
                headers={"Authorization": LOCAL_AUTHORIZATION},
            )
            self.assertEqual((response.status_code, response.content), (503, b""))
        self.assertTrue(fired)
        saved = self.saved(job)
        effects = int(boundary in {"checkpoint", "commit_checkpoint"})
        self.assertEqual(len(handler.calls), effects)
        terminal = saved.status in {"succeeded", "failed"}
        if boundary == "start" and after_commit or boundary == "checkpoint" and not after_commit:
            self.assertEqual((saved.status, saved.reason), ("failed", "outcome_unknown"))
            self.assertEqual(saved.checkpoints[0].state, "started")
        elif boundary.startswith("commit_") and after_commit:
            self.assertEqual(saved.status, "succeeded")
            self.assertIn(self.result_path(job), self.store.data)
        elif boundary == "failure" and after_commit:
            self.assertEqual((saved.status, saved.reason), ("failed", "handler_error"))
        elif boundary == "acquire":
            self.assertEqual(
                (saved.status, saved.attempts), ("running", 1) if after_commit else ("pending", 0)
            )
        elif boundary == "retry_failure" and after_commit:
            self.assertEqual(
                (saved.status, saved.reason, saved.attempts), ("pending", "retry_requested", 1)
            )
        else:
            self.assertEqual(
                (saved.status, saved.reason, saved.attempts),
                ("pending", "persistence_unavailable", 0),
            )
        if saved.status != "succeeded":
            self.assertNotIn(self.result_path(job), self.store.data)
        if boundary != "failure":
            handler.run_hook = lambda execution: None
        handler.effect_hook = lambda: None
        self.clock.advance(60)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(worker)
        worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        if terminal:
            self.assertEqual(self.saved(job), saved)
            self.assertEqual(len(handler.calls), effects)
        elif boundary == "failure":
            self.assertEqual(
                (self.saved(job).status, self.saved(job).reason), ("failed", "handler_error")
            )
            self.assertEqual(handler.calls, [])
        else:
            self.assertEqual(self.saved(job).status, "succeeded")
            self.assertEqual(len(handler.calls), int(boundary != "commit_pure"))

    def test_unavailable_deferral_keeps_durable_lease_and_bounded_expiry_recovery(self) -> None:
        for after_commit, max_attempts in ((False, 2), (False, 1), (True, 1)):
            with self.subTest(after_commit=after_commit, max_attempts=max_attempts):
                self._assert_unavailable_deferral(after_commit, max_attempts)

    def _assert_unavailable_deferral(self, after_commit: bool, max_attempts: int) -> None:
        self.handler = RecordingHandler()
        self.worker = Worker(self.jobs, LocalCaller(), {"fixture": self.handler})
        job = self.add(max_attempts=max_attempts)
        delivery = self.delivery(job)
        original_store = self.store.run
        armed = False

        def unavailable[T](work: Callable[[Transaction], T]) -> T:
            if armed:
                if after_commit:
                    original_store(work)
                raise PersistenceUnavailable()
            return original_store(work)

        def lose_database(execution: Execution) -> None:
            nonlocal armed
            armed = True
            # The next effect preflight fails, then the control write fails as well.

        self.handler.run_hook = lose_database
        with patch.object(self.store, "run", side_effect=unavailable):
            with self.assertRaises(PersistenceUnavailable):
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        saved = self.saved(job)
        self.assertEqual(len(self.handler.calls), 0)
        self.assertEqual(
            (saved.status, saved.attempts), ("pending", 0) if after_commit else ("running", 1)
        )
        self.handler.run_hook = lambda execution: None
        self.clock.advance(60)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        saved = self.saved(job)
        if not after_commit and max_attempts == 1:
            self.assertEqual((saved.status, saved.reason), ("failed", "attempts_exhausted"))
            self.assertEqual(self.handler.calls, [])
        else:
            self.assertEqual(saved.status, "succeeded")
            self.assertEqual(len(self.handler.calls), 1)

    def test_permanent_handler_failure_remains_failed_and_acknowledged(self) -> None:
        job = self.add()
        delivery = self.delivery(job)

        def permanent(execution: Execution) -> None:
            raise ValueError("synthetic permanent handler failure")

        self.handler.run_hook = permanent
        with TestClient(create_app(self.worker)) as client:
            response = client.post(
                "/tasks",
                json=delivery.model_dump(mode="json"),
                headers={"Authorization": LOCAL_AUTHORIZATION},
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            (self.saved(job).status, self.saved(job).reason), ("failed", "handler_error")
        )
        self.assertEqual(self.handler.calls, [])

    def test_fences_before_effect_and_after_external_result_and_during_commit(self) -> None:
        for boundary in ("before_effect", "after_effect", "before_commit"):
            with self.subTest(boundary=boundary):
                job = self.add()
                delivery = self.delivery(job)

                def revoke() -> None:
                    self.store.data[self.member_path]["status"] = "revoked"

                self.handler.run_hook = (
                    (lambda execution: revoke())
                    if boundary == "before_effect"
                    else (lambda execution: None)
                )
                self.handler.effect_hook = revoke if boundary == "after_effect" else lambda: None
                self.handler.use_effect = boundary != "before_commit"
                if boundary == "before_commit":
                    self.handler.run_hook = lambda execution: revoke()
                count = len(self.handler.calls)
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
                self.assertEqual(self.saved(job).status, "obsolete")
                self.assertNotIn(self.result_path(job), self.store.data)
                self.assertEqual(len(self.handler.calls) - count, boundary == "after_effect")
                self.store.data[self.member_path]["status"] = "active"

    def test_checkpoint_replay_and_ambiguous_start_never_reexecute(self) -> None:
        job = self.add()
        lease = self.jobs.acquire(self.delivery(job), known_handler=lambda _: True)
        assert lease is not None
        calls: list[str] = []

        def invoke(key: str) -> JsonValue:
            self.assertFalse(self.store.in_transaction)
            calls.append(key)
            return "done"

        self.assertEqual(self.jobs.effect(lease, "call", invoke), "done")
        self.assertEqual(self.jobs.effect(lease, "call", invoke), "done")
        self.assertEqual(len(calls), 1)
        self.jobs.start_effect(lease, "unresolved")
        with self.assertRaises(OutcomeUnknown):
            self.jobs.effect(lease, "unresolved", invoke)
        self.clock.advance(60)
        self.dispatcher.recover(self.tenant)
        self.assertEqual(
            (self.saved(job).status, self.saved(job).reason), ("failed", "outcome_unknown")
        )
        self.assertEqual(len(calls), 1)

    def test_completed_checkpoint_survives_reclaim_and_late_effect_response_is_fenced(self) -> None:
        job = self.add()
        lease = self.jobs.acquire(self.delivery(job), known_handler=lambda _: True)
        assert lease is not None
        calls: list[str] = []

        def invoke(key: str) -> JsonValue:
            calls.append(key)
            return "completed"

        self.jobs.effect(lease, "effect-1", invoke)
        self.clock.advance(60)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.saved(job).status, "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.handler.calls, [])
        late = self.add()
        old = self.jobs.acquire(self.delivery(late), known_handler=lambda _: True)
        assert old is not None

        def delayed(key: str) -> JsonValue:
            self.clock.advance(60)
            self.dispatcher.recover(self.tenant)
            return "late-provider-result"

        with self.assertRaises(LeaseLost):
            self.jobs.effect(old, "slow", delayed)
        self.assertEqual(self.saved(late).reason, "outcome_unknown")
        self.assertEqual(self.saved(late).checkpoints[0].state, "started")

    def test_two_same_lease_effect_callers_only_one_invokes(self) -> None:
        job = self.add()
        lease = self.jobs.acquire(self.delivery(job), known_handler=lambda _: True)
        assert lease is not None
        gate = threading.Barrier(2)
        original = self.jobs.start_effect

        def synchronized(lease_arg: object, key: str):  # type: ignore[no-untyped-def]
            gate.wait(timeout=5)
            return original(lease, key)

        calls: list[str] = []

        def invoke(key: str) -> JsonValue:
            calls.append(key)
            return "done"

        def call() -> object:
            try:
                return self.jobs.effect(lease, "concurrent", invoke)
            except OutcomeUnknown:
                return "unknown"

        with (
            patch.object(self.jobs, "start_effect", synchronized),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            list(pool.map(lambda _: call(), range(2)))
        self.assertEqual(len(calls), 1)

    def test_retry_limit_unknown_handler_poison_and_bounded_cursor_progress(self) -> None:
        job = self.add(max_attempts=2)

        def retry(execution: Execution) -> None:
            raise RetryableError()

        self.handler.run_hook = retry
        for _ in range(2):
            self.dispatcher.recover(self.tenant)
            self.queue.drain(self.worker)
            self.clock.advance(5)
        self.assertEqual(
            (self.saved(job).status, self.saved(job).reason), ("failed", "attempts_exhausted")
        )
        unknown = self.add(kind="unregistered")
        poisoned = self.add()
        self.store.data[path(self.tenant, poisoned.id)]["grant"] = {"invalid": True}
        self.store.data[path(self.tenant, poisoned.id)]["status"] = {"invalid": True}
        cursor = None
        inspected = 0
        while True:
            page = self.dispatcher.recover(self.tenant, after=cursor, limit=1)
            inspected += page.inspected
            cursor = page.next_cursor
            if cursor is None:
                break
        self.queue.drain(self.worker)
        self.assertEqual(inspected, 3)
        self.assertEqual(self.saved(unknown).reason, "unknown_handler")
        self.assertEqual(self.store.data[path(self.tenant, poisoned.id)]["reason"], "invalid_job")
        with self.assertRaises(ValueError):
            self.dispatcher.recover(self.tenant, limit=101)

    def test_lease_expiry_attempt_limit_and_before_completion_fencing(self) -> None:
        job = self.add(max_attempts=1)
        lease = self.jobs.acquire(self.delivery(job), known_handler=lambda _: True)
        assert lease is not None
        self.clock.advance(60)
        with self.assertRaises(LeaseLost):
            self.jobs.check(lease, renew=True)
        self.assertFalse(self.jobs.complete(lease, "late", self.handler.commit))
        self.dispatcher.recover(self.tenant)
        self.assertEqual(self.saved(job).reason, "attempts_exhausted")

    def test_worker_http_caller_tenant_and_untrusted_payload(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        with TestClient(create_app(self.worker)) as client:
            self.assertEqual(
                client.post("/tasks", json=delivery.model_dump(mode="json")).status_code, 401
            )
            spoof = {**delivery.model_dump(mode="json"), "grant": job.grant.principal.stamp}
            self.assertEqual(
                client.post(
                    "/tasks", json=spoof, headers={"Authorization": LOCAL_AUTHORIZATION}
                ).status_code,
                400,
            )
            other = {**delivery.model_dump(mode="json"), "tenant_id": str(uuid4())}
            self.assertEqual(
                client.post(
                    "/tasks", json=other, headers={"Authorization": LOCAL_AUTHORIZATION}
                ).status_code,
                204,
            )
            self.assertEqual(self.saved(job).attempts, 0)
            for body, status in (
                (b'["PRIVATE_FIXTURE"]', 400),
                (b'"PRIVATE_FIXTURE"', 400),
                (b"x" * 4097, 413),
            ):
                response = client.post(
                    "/tasks", content=body, headers={"Authorization": LOCAL_AUTHORIZATION}
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, b"")
            self.assertEqual(
                client.post(
                    "/tasks",
                    json=delivery.model_dump(mode="json"),
                    headers={"Authorization": LOCAL_AUTHORIZATION},
                ).status_code,
                204,
            )
        self.assertEqual(self.saved(job).status, "succeeded")

    def test_cloud_task_request_and_duplicate_ack(self) -> None:
        delivery = self.delivery(self.add())
        queue = CloudTasksQueue(
            queue="projects/demo-brainoid/locations/us-central1/queues/jobs",
            target_url="https://worker.example/tasks",
            audience="worker-audience",
            service_account_email="tasks@demo-brainoid.iam.gserviceaccount.com",
            access_token=lambda: "synthetic-token",
        )
        response = MagicMock()
        response.__enter__.return_value.status = 200
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(queue, "_opener", opener):
            queue.enqueue(delivery)
            request = opener.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertTrue(body["task"]["name"].endswith(delivery.task_id))
            self.assertEqual(
                body["task"]["httpRequest"]["oidcToken"]["audience"], "worker-audience"
            )
            opener.open.side_effect = HTTPError(
                "https://cloudtasks.googleapis.com",
                409,
                "",
                Message(),
                io.BytesIO(b'{"error":{"status":"ALREADY_EXISTS"}}'),
            )
            queue.enqueue(delivery)
            opener.open.side_effect = HTTPError(
                "https://cloudtasks.googleapis.com", 403, "", Message(), io.BytesIO(b"{}")
            )
            with self.assertRaises(QueueUnavailable):
                queue.enqueue(delivery)

    def test_oidc_verification_requires_signature_audience_and_allowlisted_identity(self) -> None:
        caller = GoogleCaller(
            audience="worker",
            service_account_email="tasks@demo.iam.gserviceaccount.com",
            subject="123",
        )
        claims = {
            "iss": "https://accounts.google.com",
            "aud": "worker",
            "sub": "123",
            "email": "tasks@demo.iam.gserviceaccount.com",
            "email_verified": True,
        }
        with patch(
            "brainoid_worker.cloud_tasks.verify_oauth2_token", return_value=claims
        ) as verifier:
            caller.verify("Bearer synthetic")
            self.assertEqual(verifier.call_args.kwargs["audience"], "worker")
            for key, value in (
                ("aud", "other"),
                ("sub", "456"),
                ("email_verified", False),
                ("email", "attacker@example.test"),
            ):
                verifier.return_value = {**claims, key: value}
                with self.assertRaises(CallerDenied):
                    caller.verify("Bearer synthetic")
            verifier.side_effect = ValueError("bad signature")
            with self.assertRaises(CallerDenied):
                caller.verify("Bearer synthetic")
