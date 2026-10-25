"""T-031 foundation: real atomic jobs, process restart and concurrent lease fencing."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Delivery, Job, Jobs
from brainoid_jobs.repository import path
from brainoid_persistence.firestore import FirestoreTransactions, PersistenceUnavailable
from brainoid_policy.control_ledger import Change, ControlLedger, Entry, LedgerError, MemberTarget
from brainoid_policy.models import Action, ResourceKind, ResourceRef, Role, UserSubject
from brainoid_policy.transactions import transaction_policy
from brainoid_worker.dispatcher import Dispatcher, QueueUnavailable
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from pydantic import JsonValue
from test_authz_ledger_integration import IndependentBucket

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture


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


class JobRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 26, tzinfo=UTC)
        self.objects = IndependentBucket()
        self.ledger = ControlLedger(self.objects)
        documents = load_fixture()["documents"]
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
        self.queue = LocalQueue()
        self.handler = RecordingHandler()
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.db.document(self.member_path).update(
            {"authorization_operation_id": change.operation_id}
        )
        self.database = FirestoreTransactions(self.db)
        self.jobs = Jobs(self.database, self.ledger, now=lambda: self.now)
        self.dispatcher = Dispatcher(self.jobs, self.database, self.queue)
        self.worker = Worker(self.jobs, LocalCaller(), {"fixture": self.handler})

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

    def add(
        self, *, kind: str = "fixture", max_attempts: int = 5, replacement: Document | None = None
    ) -> Job:
        operation = ResourceId(str(uuid4()))

        def work(tx: Transaction) -> Job:
            grant = transaction_policy(tx, ledger=self.ledger, now=lambda: self.now).resource(
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

        return self.database.run(work)

    def test_atomic_source_update_and_job_creation_rollback(self) -> None:
        replacement = {
            **(self.db.document(self.source_path).get().to_dict() or {}),
            "version": 2,
            "body": "FIXTURE_UPDATED",
        }
        job = self.add(replacement=replacement)
        self.assertEqual((self.db.document(self.source_path).get().to_dict() or {})["version"], 2)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual(self.saved(job).status, "succeeded")

        def rollback(tx: Transaction) -> None:
            grant = transaction_policy(tx, ledger=self.ledger, now=lambda: self.now).resource(
                self.tenant,
                self.subject,
                self.ref,
                action=Action.WRITE,
            )
            self.jobs.add_pending(
                tx,
                operation_id=ResourceId(str(uuid4())),
                kind="fixture",
                grant=grant,
                source_replacement={**replacement, "version": 3},
            )
            raise RuntimeError("before commit")

        with self.assertRaises(RuntimeError):
            self.database.run(rollback)
        self.assertEqual(len(self.database.page(f"tenants/{self.tenant}/jobs")), 1)
        self.assertEqual((self.db.document(self.source_path).get().to_dict() or {})["version"], 2)

    def test_restart_in_fresh_process_recovers_job_after_failed_enqueue(self) -> None:
        job = self.add()

        def fail(delivery: Delivery) -> None:
            raise QueueUnavailable()

        self.queue.before = fail
        self.assertEqual(self.dispatcher.recover(self.tenant).enqueue_errors, 1)
        # Persist the independent synthetic ledger too; the child inherits no Python objects.
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "ledger.json"
            ledger_path.write_text(
                json.dumps({k: v.decode() for k, v in self.objects.data.items()})
            )
            source = """
import json, sys
from datetime import UTC, datetime
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from brainoid_contracts import ResourceId
from brainoid_jobs import Jobs
from brainoid_persistence.firestore import FirestoreTransactions
from brainoid_policy.control_ledger import ControlLedger
from brainoid_worker.dispatcher import Dispatcher
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from fakes.authz_ledger import MemoryObjects
from test_job_recovery import RecordingHandler
objects = MemoryObjects()
objects.data = {k: v.encode() for k, v in json.load(open(sys.argv[1])).items()}
client = Client(project="demo-brainoid", database="(default)", credentials=AnonymousCredentials())
try:
    store = FirestoreTransactions(client)
    jobs = Jobs(store, ControlLedger(objects), now=lambda: datetime(2026, 10, 26, tzinfo=UTC))
    queue = LocalQueue()
    assert Dispatcher(jobs, store, queue).recover(ResourceId(sys.argv[2])).dispatched == 1
    assert queue.drain(Worker(jobs, LocalCaller(), {"fixture": RecordingHandler()})) == 1
finally:
    client.close()
"""
            subprocess.run(
                [sys.executable, "-B", "-c", source, str(ledger_path), str(self.tenant)],
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        (str(Path("tests").resolve()), str(Path("tests/emulator").resolve()))
                    ),
                },
                check=True,
                timeout=30,
            )
        self.assertEqual(self.saved(job).status, "succeeded")
        self.assertTrue(self.db.document(self.result_path(job)).get().exists)

    def test_concurrent_delivery_and_expired_lease_generation_fence(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        barrier = Barrier(2)

        def acquire() -> object:
            barrier.wait(timeout=10)
            return self.jobs.acquire(delivery, known_handler=lambda _: True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(acquire)
            second = pool.submit(acquire)
            leases = [first.result(timeout=30), second.result(timeout=30)]
        self.assertEqual(sum(item is not None for item in leases), 1)
        from brainoid_jobs import Lease

        old = next(item for item in leases if isinstance(item, Lease))
        self.now += timedelta(seconds=60)
        delivery2 = self.delivery(job)
        current = self.jobs.acquire(delivery2, known_handler=lambda _: True)
        assert current is not None
        self.assertFalse(self.jobs.complete(old, "stale", self.handler.commit))
        self.assertTrue(self.jobs.complete(current, "new", self.handler.commit))
        self.assertEqual(
            (self.db.document(self.result_path(job)).get().to_dict() or {})["result"], "new"
        )

    def test_ledger_revocation_and_deletion_before_start_and_commit(self) -> None:
        job = self.add()
        delivery = self.delivery(job)
        self.db.document(self.source_path).update({"deleted": True, "deletion_epoch": 1})
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertEqual(self.saved(job).reason, "source_deleted")
        self.assertEqual(self.handler.calls, [])
        self.db.document(self.source_path).update({"deleted": False, "deletion_epoch": 0})
        job = self.add()
        delivery = self.delivery(job)

        def revoke() -> None:
            self.db.document(self.member_path).update(
                {"status": "revoked", "authorization_version": 2}
            )

        self.handler.effect_hook = revoke
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertEqual(self.saved(job).reason, "authorization_changed")
        self.assertEqual(len(self.handler.calls), 1)
        self.assertFalse(self.db.document(self.result_path(job)).get().exists)

    def test_temporary_ledger_failure_preserves_recovery_and_checkpoint_fences(self) -> None:
        for boundary in (
            "acquire",
            "before_effect",
            "before_commit",
            "after_checkpoint",
            "checkpoint_write",
        ):
            with self.subTest(boundary=boundary):
                self._assert_authority_outage_recovers(boundary)

    def _assert_authority_outage_recovers(self, boundary: str) -> None:
        job = self.add(max_attempts=1)
        delivery = self.delivery(job)
        handler = RecordingHandler()
        worker = Worker(self.jobs, LocalCaller(), {"fixture": handler})
        available = boundary != "acquire"
        original_latest = self.ledger.latest

        def latest(key: str) -> Entry | None:
            if not available:
                raise LedgerError()
            return original_latest(key)

        def interrupt() -> None:
            nonlocal available
            available = False

        if boundary in {"before_effect", "before_commit"}:
            handler.run_hook = lambda execution: interrupt()
        if boundary == "before_commit":
            handler.use_effect = False
        if boundary == "checkpoint_write":
            handler.effect_hook = interrupt
        original_run = handler.run

        def run(execution: Execution) -> JsonValue:
            result = original_run(execution)
            if boundary == "after_checkpoint":
                interrupt()
            return result

        with (
            patch.object(self.ledger, "latest", side_effect=latest),
            patch.object(handler, "run", side_effect=run),
        ):
            with self.assertRaises(AuthorizationUnavailable):
                worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        saved = self.saved(job)
        self.assertFalse(self.db.document(self.result_path(job)).get().exists)
        self.assertEqual(
            len(handler.calls), int(boundary in {"after_checkpoint", "checkpoint_write"})
        )
        if boundary == "checkpoint_write":
            self.assertEqual((saved.status, saved.reason), ("failed", "outcome_unknown"))
            self.assertEqual(saved.checkpoints[0].state, "started")
            worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
            self.assertEqual(len(handler.calls), 1)
            return
        self.assertEqual((saved.status, saved.attempts), ("pending", 0))
        handler.run_hook = lambda execution: None
        self.now += timedelta(seconds=5)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(worker)
        self.assertEqual((self.saved(job).status, self.saved(job).attempts), ("succeeded", 1))
        self.assertEqual(len(handler.calls), int(boundary != "before_commit"))

    def test_sdk_read_unavailable_returns_recoverable_job(self) -> None:
        job = self.add(max_attempts=1)
        delivery = self.delivery(job)
        original_check = self.jobs.check
        original_read = self.db._firestore_api.batch_get_documents
        armed = fired = False

        def check(*args: object, **kwargs: Any) -> Job:
            nonlocal armed
            armed = True
            return original_check(*args, **kwargs)  # type: ignore[arg-type]

        def fail_read(*args: Any, **kwargs: Any) -> Any:
            nonlocal fired
            if armed and not fired:
                fired = True
                raise ServiceUnavailable("synthetic unavailable read")  # type: ignore[no-untyped-call]
            return original_read(*args, **kwargs)

        with (
            patch.object(self.jobs, "check", side_effect=check),
            patch.object(self.db._firestore_api, "batch_get_documents", side_effect=fail_read),
        ):
            with self.assertRaises(PersistenceUnavailable):
                self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertTrue(fired)
        self.assertEqual(
            (self.saved(job).status, self.saved(job).reason, self.saved(job).attempts),
            ("pending", "persistence_unavailable", 0),
        )
        self.assertEqual(self.handler.calls, [])
        self.now += timedelta(seconds=5)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)
        self.assertEqual((self.saved(job).status, self.saved(job).attempts), ("succeeded", 1))

    def test_sdk_commit_failures_and_response_loss_reconcile_actual_persisted_state(self) -> None:
        for boundary in ("acquire", "start", "checkpoint", "complete", "failure"):
            for after_commit in (False, True):
                with self.subTest(boundary=boundary, after_commit=after_commit):
                    self._assert_commit_response_loss(boundary, after_commit)

    def _assert_commit_response_loss(self, boundary: str, after_commit: bool) -> None:
        job = self.add(max_attempts=2 if boundary == "acquire" else 1)
        delivery = self.delivery(job)
        handler = RecordingHandler()
        worker = Worker(self.jobs, LocalCaller(), {"fixture": handler})
        if boundary == "failure":

            def permanent(execution: Execution) -> None:
                raise ValueError("synthetic permanent handler failure")

            handler.run_hook = permanent
        original_commit = self.db._firestore_api.commit
        armed = fired = False

        def arm() -> None:
            nonlocal armed
            armed = True

        def fail_commit(*args: Any, **kwargs: Any) -> Any:
            nonlocal fired
            if armed and not fired:
                fired = True
                if after_commit:
                    original_commit(*args, **kwargs)
                raise ServiceUnavailable("synthetic unavailable commit response")  # type: ignore[no-untyped-call]
            return original_commit(*args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(self.db._firestore_api, "commit", side_effect=fail_commit)
            )
            if boundary == "checkpoint":
                handler.effect_hook = arm
            else:
                target = {
                    "acquire": "acquire",
                    "start": "start_effect",
                    "complete": "complete",
                    "failure": "fail",
                }[boundary]
                original: Callable[..., object] = getattr(self.jobs, target)

                def interrupted(*args: object, **kwargs: object) -> object:
                    arm()
                    return original(*args, **kwargs)

                stack.enter_context(patch.object(self.jobs, target, side_effect=interrupted))
            with self.assertRaises(PersistenceUnavailable):
                worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        self.assertTrue(fired)
        saved = self.saved(job)
        self.assertEqual(len(handler.calls), int(boundary in {"checkpoint", "complete"}))
        if boundary == "start" and after_commit or boundary == "checkpoint" and not after_commit:
            self.assertEqual((saved.status, saved.reason), ("failed", "outcome_unknown"))
            self.assertEqual(saved.checkpoints[0].state, "started")
        elif boundary == "complete" and after_commit:
            self.assertEqual(saved.status, "succeeded")
            self.assertTrue(self.db.document(self.result_path(job)).get().exists)
        elif boundary == "failure" and after_commit:
            self.assertEqual((saved.status, saved.reason), ("failed", "handler_error"))
        elif boundary == "acquire":
            self.assertEqual(
                (saved.status, saved.attempts), ("running", 1) if after_commit else ("pending", 0)
            )
        else:
            self.assertEqual(
                (saved.status, saved.reason, saved.attempts),
                ("pending", "persistence_unavailable", 0),
            )
        if saved.status != "succeeded":
            self.assertFalse(self.db.document(self.result_path(job)).get().exists)
        handler.effect_hook = lambda: None
        self.now += timedelta(seconds=60)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(worker)
        worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump())
        if saved.status in {"succeeded", "failed"}:
            self.assertEqual(self.saved(job), saved)
        elif boundary == "failure":
            self.assertEqual(
                (self.saved(job).status, self.saved(job).reason), ("failed", "handler_error")
            )
        else:
            self.assertEqual(self.saved(job).status, "succeeded")
        self.assertEqual(
            len(handler.calls),
            int(boundary != "failure" and not (boundary == "start" and after_commit)),
        )

    def test_enqueue_ack_loss_retained_name_and_opaque_cursor_recovery(self) -> None:
        job = self.add()
        # Bad imported document keys must not prevent a bounded scan reaching valid jobs.
        self.db.document(f"tenants/{self.tenant}/jobs/!invalid-key").set({"status": "pending"})
        page = self.dispatcher.recover(self.tenant, limit=1)
        self.assertEqual(page.next_cursor, "!invalid-key")

        def lost_ack(delivery: Delivery) -> None:
            raise QueueUnavailable()

        self.queue.after = lost_ack
        page = self.dispatcher.recover(self.tenant, after=page.next_cursor, limit=1)
        self.assertEqual(page.enqueue_errors, 1)
        old = next(iter(self.queue.tasks.values()))
        self.queue.tasks.clear()
        self.queue.after = lambda delivery: None
        self.now += timedelta(seconds=120)
        page = self.dispatcher.recover(self.tenant)
        self.assertEqual(page.dispatched, 1)
        self.assertNotEqual(next(iter(self.queue.tasks)), old.task_id)
        self.queue.drain(self.worker)
        self.assertEqual(self.saved(job).status, "succeeded")
        self.assertEqual(
            (self.db.document(path(self.tenant, job.id)).get().to_dict() or {})["attempts"], 1
        )
