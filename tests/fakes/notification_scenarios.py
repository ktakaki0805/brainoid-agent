"""Synthetic notification scenarios shared by strict fake and Firestore adapters."""

import json
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import (
    lifecycle_account_path,
    lifecycle_tenant_path,
    tombstone_path,
)
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Jobs
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_memory.export_projection import project
from brainoid_memory.notifications import (
    KIND,
    acknowledge,
    notification_id,
    notification_path,
    public_notification,
    read_delivery,
    read_notification,
)
from brainoid_memory.reminders import Reminder, Reminders, ScheduleReference, reminder_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.accounts import account_stamp
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.models import DeviceSubject, UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from brainoid_worker.dispatcher import Dispatcher, QueueUnavailable
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller, LocalQueue
from brainoid_worker.notifications import DispatchNotification, DueReminders, Receipt
from pydantic import JsonValue

from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import TENANT, USER, AuthenticatedCaller, authority_data, context
from fakes.runtime import FakeClock


class FakeNotifications:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []
        self.receipts: dict[str, Receipt] = {}
        self.result: Receipt = "accepted"
        self.after: Callable[[], None] = lambda: None

    def send(self, key: str, payload: dict[str, JsonValue]) -> Receipt:
        self.calls.append((key, payload))
        self.receipts.setdefault(key, self.result)
        self.after()
        return self.receipts[key]

    def lookup(self, key: str) -> Receipt | None:
        return self.receipts.get(key)


class NotificationFixture(unittest.TestCase):
    store: Any

    def setup_notifications(self) -> None:
        self.clock = FakeClock()
        self.ledger = ControlLedger(MemoryObjects())
        for path, doc in authority_data().items():
            self.put(path, doc)
        self.jobs = Jobs(
            self.store, self.ledger, now=self.clock.now, allow_untracked_bootstrap=True
        )
        self.provider = FakeNotifications()
        self.handler = DispatchNotification(self.jobs, self.provider)
        self.queue = LocalQueue()
        self.dispatcher = Dispatcher(self.jobs, self.store, self.queue)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.scanner = DueReminders(self.jobs, self.store)
        self.caller = AuthenticatedCaller()
        self.repository = MemoryRepository(
            self.store,
            authorizer=self.caller,
            sources=CanonicalSources(),
            ledger=self.ledger,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )

    def put(self, path: str, doc: Document) -> None:
        self.store.run(lambda tx: tx.set(path, doc))

    def get(self, path: str) -> Document:
        value = self.store.run(lambda tx: tx.get(path))
        assert value is not None
        return value  # type: ignore[no-any-return]

    def update(self, path: str, **fields: Any) -> None:
        def work(tx: Transaction) -> None:
            doc = tx.get(path)
            assert doc is not None

            def save() -> None:
                pass

            if "/reminders/" in path:
                old = Reminder.model_validate(doc)
                updated = Reminder.model_validate({**doc, **fields})
                save = Reminders(self.store, now=self.clock.now).prepare_write(tx, old, updated)
            tx.set(path, {**doc, **fields})
            save()

        self.store.run(work)

    def reminder(self, **changes: Any) -> Reminder:
        now = self.clock.now()
        record = Reminder.model_validate(
            {
                "id": str(uuid4()),
                "tenant_id": TENANT,
                "title": "SYNTHETIC_PRIVATE_TITLE",
                "due_at": now + timedelta(seconds=1),
                "timezone": "Asia/Tokyo",
                "delivery_target": {"kind": "user", "id": USER},
                "visibility": "private",
                "display_policy": "never",
                "owner_user_id": USER,
                "created_by": USER,
                "updated_by": USER,
                "version": 1,
                "deletion_epoch": 0,
                "schema_version": 1,
                "created_at": now - timedelta(seconds=1),
                "updated_at": now,
                "deleted": False,
                "suppress_reuse": False,
                **changes,
            }
        )

        def accepted(tx: Transaction) -> None:
            save = Reminders(self.store, now=lambda: record.created_at).prepare_write(
                tx, None, record
            )
            tx.set(reminder_path(TENANT, record.id), record.model_dump(mode="json"))
            save()

        self.store.run(accepted)
        return record

    def identity(self, record: Reminder) -> ResourceId:
        return notification_id(
            ScheduleReference(
                tenant_id=TENANT, reminder_id=record.id, schedule_version=record.schedule_version
            )
        )

    def job(self, record: Reminder) -> Any:
        return self.jobs.get(TENANT, job_id(TENANT, KIND, self.identity(record)))

    def public(self, record: Reminder) -> Document:
        result: Document = self.store.run(
            lambda tx: public_notification(
                self.repository.facts(tx, context()), self.identity(record)
            )
        )
        return result

    def ack(self, record: Reminder, action: Any = "acknowledged") -> None:
        self.store.run(
            lambda tx: acknowledge(
                self.repository.facts(tx, context()), self.identity(record), action
            )
        )

    def deliver(self) -> None:
        self.dispatcher.recover(TENANT)
        self.queue.drain(self.worker)


class NotificationScenarios(NotificationFixture):
    def test_due_boundary_double_scan_duplicate_queue_and_separate_receipts(self) -> None:
        record = self.reminder()
        self.assertEqual(self.scanner.scan(TENANT).created, 0)
        self.clock.advance(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = list(pool.map(lambda _: self.scanner.scan(TENANT), range(2)))
        self.assertEqual(sum(p.created for p in pages), 1)
        self.assertEqual(self.scanner.scan(TENANT).created, 0)
        self.dispatcher.recover(TENANT)
        delivery = next(iter(self.queue.tasks.values()))
        self.queue.drain(self.worker)
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertNotIn(record.title, json.dumps(self.provider.calls))
        self.assertNotIn(
            record.title, json.dumps(self.get(notification_path(TENANT, self.identity(record))))
        )
        self.assertEqual(self.public(record)["status"], "sent")
        self.ack(record, "displayed")
        self.assertEqual(self.public(record)["status"], "displayed")
        self.ack(record)
        self.ack(record)
        self.assertEqual(self.public(record)["status"], "acknowledged")
        notification = self.store.run(
            lambda tx: read_notification(tx, TENANT, self.identity(record))
        )
        state = self.store.run(lambda tx: read_delivery(tx, notification))
        self.assertIsNotNone(state.accepted_at)
        self.assertIsNotNone(state.displayed_at)
        self.assertIsNotNone(state.acknowledged_at)
        self.assertEqual(self.get(reminder_path(TENANT, record.id))["status"], "acknowledged")

    def test_title_edit_current_schedule_and_old_snooze_ack_jobs(self) -> None:
        record = self.reminder()
        self.clock.advance(1)
        self.scanner.scan(TENANT)
        self.update(reminder_path(TENANT, record.id), version=2, title="CURRENT_TITLE")
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(self.public(record)["title"], "CURRENT_TITLE")
        self.update(
            reminder_path(TENANT, record.id),
            version=4,
            schedule_version=2,
            status="scheduled",
            due_at=(self.clock.now() + timedelta(seconds=1)).isoformat(),
        )
        before = self.get(reminder_path(TENANT, record.id))
        with self.assertRaises(PolicyDenied):
            self.ack(record)
        self.assertEqual(self.get(reminder_path(TENANT, record.id)), before)
        self.clock.advance(1)
        self.scanner.scan(TENANT)
        fresh = Reminder.model_validate(before)
        self.deliver()
        self.assertEqual(self.job(fresh).status, "succeeded")
        self.assertEqual(len(self.provider.calls), 2)
        self.assertNotEqual(self.identity(record), self.identity(fresh))

    def test_cancel_old_generation_and_revoked_user_never_send(self) -> None:
        records = [self.reminder() for _ in range(3)]
        self.clock.advance(1)
        self.scanner.scan(TENANT)
        self.update(
            reminder_path(TENANT, records[0].id), status="cancelled", schedule_version=2, version=2
        )
        self.update(
            reminder_path(TENANT, records[1].id),
            schedule_version=2,
            version=2,
            due_at=(self.clock.now() + timedelta(hours=1)).isoformat(),
        )
        # Only dispatch old/cancelled jobs first; then revoke the remaining owner.
        for record in records[:2]:
            self.dispatcher.dispatch(TENANT, self.job(record).id)
        self.queue.drain(self.worker)
        self.update(f"tenants/{TENANT}/memberships/safe-key", status="revoked")
        self.deliver()
        self.assertEqual(self.provider.calls, [])
        self.assertTrue(all(self.job(r).status == "obsolete" for r in records))

    def test_five_minute_boundary_offline_retains_unconfirmed_inbox(self) -> None:
        for elapsed, auto_play in ((299.999, True), (300, True), (300.001, False)):
            record = self.reminder(due_at=self.clock.now())
            self.scanner.scan(TENANT)
            self.clock.advance(elapsed)
            self.provider.result = "offline"
            self.deliver()
            self.assertEqual(self.provider.calls[-1][1]["auto_play"], auto_play)
            self.assertEqual(self.public(record)["status"], "pending")
            self.assertEqual(self.job(record).status, "succeeded")
        self.clock.advance(1000)
        before = len(self.provider.calls)
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(len(self.provider.calls), before)

    def test_enqueue_before_after_response_loss_uses_one_logical_notification(self) -> None:
        for hook in ("before", "after"):
            record = self.reminder(due_at=self.clock.now())
            self.scanner.scan(TENANT)

            def fail(_: Any) -> None:
                raise QueueUnavailable()

            setattr(self.queue, hook, fail)
            self.assertEqual(self.dispatcher.recover(TENANT).enqueue_errors, 1)
            setattr(self.queue, hook, lambda _: None)
            self.deliver()
            self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(len(self.provider.calls), 2)

    def test_lease_reclaim_completed_reuse_and_unknown_reconciliation(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        delivery = self.jobs.prepare_dispatch(TENANT, self.job(record).id)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        self.clock.advance(61)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(self.job(record).lease_generation, 2)
        unknown = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)

        def lost() -> None:
            raise RuntimeError("synthetic accepted response loss")

        self.provider.after = lost
        self.deliver()
        self.assertEqual(self.job(unknown).reason, "outcome_unknown")
        self.provider.after = lambda: None
        before = len(self.provider.calls)
        self.deliver()
        self.assertEqual(len(self.provider.calls), before)
        self.assertTrue(self.handler.reconcile_unknown(TENANT, self.job(unknown).id))
        self.deliver()
        self.assertEqual(self.job(unknown).status, "succeeded")
        self.assertEqual(len(self.provider.calls), before)

    def test_cursor_and_abandoned_scan_resume(self) -> None:
        records = [self.reminder(due_at=self.clock.now()) for _ in range(3)]
        first = self.scanner.scan(TENANT, limit=1)
        self.assertEqual((first.inspected, first.created), (1, 1))
        self.assertIsNotNone(first.next_cursor)
        next_scan = DueReminders(self.jobs, self.store)
        self.assertEqual(next_scan.scan(TENANT, limit=1).created, 1)
        state = self.get(self.scanner.path(TENANT))
        self.update(
            self.scanner.path(TENANT),
            lease="abandoned",
            expires_at=(self.clock.now() + timedelta(seconds=1)).isoformat(),
        )
        self.assertTrue(next_scan.scan(TENANT, limit=1).busy)
        self.clock.advance(1)
        self.assertEqual(next_scan.scan(TENANT, limit=1).created, 1)
        self.assertIsNotNone(state["cursor"])
        self.assertIsNone(next_scan.scan(TENANT, limit=1).next_cursor)
        self.deliver()
        self.assertTrue(all(self.job(r).status == "succeeded" for r in records))

    def test_recipient_only_current_task_and_deletion_fences(self) -> None:
        record = self.reminder(due_at=self.clock.now(), visibility="shared", owner_user_id=None)
        self.scanner.scan(TENANT)
        other = FirebaseUid("another/opaque")
        self.put(
            f"tenants/{TENANT}/memberships/other",
            {
                "tenant_id": TENANT,
                "user_id": other,
                "role": "owner",
                "status": "active",
                "authorization_version": 1,
            },
        )
        self.caller.subject = self.caller.subject.model_copy(update={"user_id": other})
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: public_notification(
                    self.repository.facts(tx, context(user=other)), self.identity(record)
                )
            )
        self.caller.subject = self.caller.subject.model_copy(update={"user_id": USER})
        self.update(reminder_path(TENANT, record.id), deleted=True)
        self.deliver()
        self.assertEqual(self.provider.calls, [])
        with self.assertRaises(PolicyDenied):
            self.public(record)

    def test_current_device_session_owner_scope_capabilities_and_display_policy(self) -> None:
        device, session = ResourceId(str(uuid4())), ResourceId(str(uuid4()))
        device_path = f"tenants/{TENANT}/devices/{device}"
        session_path = f"tenants/{TENANT}/private_sessions/{session}"
        device_doc = {
            "id": device,
            "tenant_id": TENANT,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "allowed_scopes": ["shared", "reminders"],
            "allowed_entity_ids": [],
            "allowed_room_ids": [],
            "capabilities": ["display", "audio_output"],
        }
        self.put(device_path, device_doc)
        stamp = self.store.run(lambda tx: account_stamp(tx, USER))
        session_doc = {
            "id": session,
            "tenant_id": TENANT,
            "device_id": device,
            "user_id": USER,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "membership_authorization_version": 1,
            "device_authorization_version": 1,
            "user_authorization_stamp": stamp,
            "allowed_scopes": ["reminders"],
            "expires_at": (self.clock.now() + timedelta(minutes=5)).isoformat(),
            "idle_expires_at": (self.clock.now() + timedelta(seconds=60)).isoformat(),
        }
        self.put(session_path, session_doc)
        self.put(f"tenants/{TENANT}/private_session_slots/{device}", {"session_id": session})
        record = self.reminder(
            due_at=self.clock.now(),
            delivery_target={"kind": "device", "id": device},
            display_policy="authenticated_session",
        )
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertTrue(self.provider.calls[-1][1]["auto_play"])
        self.assertEqual(self.provider.calls[-1][1]["private_session_id"], session)
        self.caller.subject = DeviceSubject(
            device_id=device, connection_generation=1, private_session_id=session
        )
        device_context = self.store.run(
            lambda tx: (
                transaction_policy(
                    tx, ledger=self.ledger, now=self.clock.now, allow_untracked_bootstrap=True
                )
                .principal(TENANT, self.caller.subject)
                .context
            )
        )
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: acknowledge(
                    self.repository.facts(tx, device_context), self.identity(record), "acknowledged"
                )
            )
        self.store.run(
            lambda tx: acknowledge(
                self.repository.facts(tx, device_context), self.identity(record), "displayed"
            )
        )
        self.caller.subject = UserSubject(user_id=USER)
        for state_path, changes in (
            (device_path, {"capabilities": []}),
            (device_path, {"status": "revoked"}),
            (device_path, {"allowed_scopes": ["shared", "notes"]}),
            (device_path, {"authorization_operation_id": str(uuid4())}),
            (session_path, {"allowed_scopes": ["notes"]}),
            (session_path, {"user_id": "other"}),
            (session_path, {"idle_expires_at": self.clock.now().isoformat()}),
        ):
            with self.subTest(changes=changes):
                self.put(device_path, device_doc)
                self.put(session_path, session_doc)
                pending = self.reminder(
                    due_at=self.clock.now(),
                    delivery_target={"kind": "device", "id": device},
                    display_policy="authenticated_session",
                )
                self.scanner.scan(TENANT)
                self.update(state_path, **changes)
                self.deliver()
                self.assertEqual(self.job(pending).status, "obsolete")
        self.assertEqual(len(self.provider.calls), 1)

    def test_task_target_account_and_tenant_current_fences(self) -> None:
        for mode in ("task", "target_account", "tenant"):
            with self.subTest(mode=mode):
                record = self.reminder(
                    due_at=self.clock.now(), visibility="shared", owner_user_id=None
                )
                task = ResourceId(str(uuid4()))
                task_path = f"tenants/{TENANT}/tasks/{task}"
                self.put(
                    task_path,
                    {
                        "id": task,
                        "tenant_id": TENANT,
                        "visibility": "shared",
                        "display_policy": "never",
                        "owner_user_id": None,
                        "created_by": USER,
                        "version": 1,
                        "deletion_epoch": 0,
                        "deleted": False,
                    },
                )
                self.update(reminder_path(TENANT, record.id), task_id=task)
                self.scanner.scan(TENANT)
                fence = (
                    tombstone_path(TENANT, "tasks", task)
                    if mode == "task"
                    else lifecycle_account_path(USER)
                    if mode == "target_account"
                    else lifecycle_tenant_path(TENANT)
                )
                self.put(fence, {"mode": "delete_source"})
                self.deliver()
                self.assertEqual(self.job(record).status, "obsolete")
                with self.assertRaises(PolicyDenied):
                    self.public(record)
                self.store.run(lambda tx, fence=fence: tx.delete(fence))
        self.assertEqual(self.provider.calls, [])

    def test_shared_user_target_is_current_recipient_not_implicit_delegation(self) -> None:
        recipient = FirebaseUid("recipient/with/slashes")
        member_path = f"tenants/{TENANT}/memberships/recipient"
        self.put(
            member_path,
            {
                "tenant_id": TENANT,
                "user_id": recipient,
                "role": "member",
                "status": "active",
                "authorization_version": 1,
            },
        )
        record = self.reminder(
            due_at=self.clock.now(),
            visibility="shared",
            owner_user_id=None,
            delivery_target={"kind": "user", "id": recipient},
        )
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        with self.assertRaises(PolicyDenied):
            self.public(record)
        self.caller.subject = UserSubject(user_id=recipient)
        visible = self.store.run(
            lambda tx: public_notification(
                self.repository.facts(tx, context(user=recipient)), self.identity(record)
            )
        )
        self.assertEqual(visible["title"], record.title)
        pending = self.reminder(
            due_at=self.clock.now(),
            visibility="shared",
            owner_user_id=None,
            delivery_target={"kind": "user", "id": recipient},
        )
        self.scanner.scan(TENANT)
        self.update(member_path, status="revoked")
        self.deliver()
        self.assertEqual(self.job(pending).status, "obsolete")
        self.assertEqual(len(self.provider.calls), 1)

    def test_export_projection_manifest_tracks_current_title_and_ack(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        path = notification_path(TENANT, self.identity(record))

        def read(tx: Transaction) -> Any:
            return project(tx, self.repository.facts(tx, context()), path, scope="my_data")

        first = self.store.run(read)
        self.assertNotIn(record.title, first[0].model_dump_json())
        self.assertNotIn("created_by", first[1])
        self.update(reminder_path(TENANT, record.id), title="CURRENT_TITLE", version=2)
        second = self.store.run(read)
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(second[1]["title"], "CURRENT_TITLE")
        self.ack(record)
        self.assertNotEqual(second[0], self.store.run(read)[0])
        self.deliver()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.public(record)["status"], "acknowledged")

    def test_cancel_during_start_and_after_provider_does_not_commit_old_result(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        start = self.jobs.start_effect

        def cancelled(*args: Any, **kwargs: Any) -> Any:
            result = start(*args, **kwargs)
            self.update(
                reminder_path(TENANT, record.id), schedule_version=2, status="cancelled", version=2
            )
            return result

        with patch.object(self.jobs, "start_effect", side_effect=cancelled):
            self.deliver()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.job(record).status, "obsolete")
        later = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        self.provider.after = lambda: self.update(
            reminder_path(TENANT, later.id), schedule_version=2, status="cancelled", version=2
        )
        self.deliver()
        self.assertEqual(self.job(later).status, "obsolete")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.get(reminder_path(TENANT, later.id))["version"], 2)

    def test_policy_and_persistence_unavailable_preserve_completed_checkpoint_budget(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        with patch.object(self.ledger, "latest", side_effect=LedgerError()):
            with self.assertRaises(AuthorizationUnavailable):
                self.deliver()
        self.assertEqual(self.job(record).attempts, 0)
        for exception in (AuthorizationUnavailable(), PersistenceUnavailable()):
            with patch.object(self.handler, "_commit", side_effect=exception):
                with self.assertRaises(type(exception)):
                    self.deliver()
            self.assertEqual(self.job(record).status, "pending")
            self.assertEqual(self.job(record).attempts, 0)
            self.assertEqual(len(self.provider.calls), 1)
            self.clock.advance(5)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_future_schedule_does_not_extend_original_http_credential(self) -> None:
        self.reminder(due_at=self.clock.now() + timedelta(hours=2))
        self.clock.advance(7200)
        self.assertLess(self.caller.valid_to, self.clock.now())
        self.assertEqual(self.scanner.scan(TENANT).created, 1)
        self.deliver()
        self.assertEqual(len(self.provider.calls), 1)
