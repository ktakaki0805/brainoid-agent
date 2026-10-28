"""DEV04 current session/run snapshot and shared delivery sequence on official Firestore."""

from datetime import timedelta
from uuid import uuid4

from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_notifications import DeviceNotifications
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, IanaTimezone, Visibility
from brainoid_contracts.transactions import Transaction
from brainoid_memory.notifications import read_delivery, read_notification
from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
from brainoid_runs.models import RunAccepted, TurnCreate
from brainoid_worker.notifications import DueReminders
from test_turn_run_integration import TurnRunIntegrationFixture


class DeviceSnapshotIntegrationTests(TurnRunIntegrationFixture):
    def test_committed_paging_private_revocation_and_notification_receipt(self) -> None:
        pairing = self.pairing()
        device = self.confirm(pairing)
        credentials = self.exchange(pairing)
        self.db.document(f"tenants/{self.tenant}/devices/{device.id}").update(
            {"capabilities": ["display"]}
        )
        identity = self.repo.authenticate(credentials.access_token)
        memories = Memories(
            self.store,
            self.tenants,
            self.conversations.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            allow_untracked_bootstrap=True,
            now=lambda: self.now,
        )
        snapshots = DeviceSnapshots(self.conversations, memories, self.sessions)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            device.id,
            PrivateSessionCreate(
                allowed_scopes=["conversation", "reminders"], acknowledge_shared_space=True
            ),
            "snapshot-integration-session",
            "integration-start",
        )
        runs = set()
        for _ in range(21):
            run = self.conversations.turn(
                self.identity(),
                self.tenant,
                self.conversation(private=True),
                TurnCreate(
                    text="SYNTHETIC_EMULATOR_RUN", output_mode="text", output_device_id=device.id
                ),
                str(uuid4()),
            )
            runs.add(run.run_id)

            def release(tx: Transaction, run: RunAccepted = run) -> None:
                self.conversations.usage.prepare_release(tx, self.tenant, run.run_id)()

            self.store.run(release)
        first = snapshots.page(identity).release()
        self.assertIsNotNone(first.next_cursor)
        second = snapshots.page(identity, first.next_cursor).release()
        self.assertEqual({r.run_id for r in first.assigned_runs + second.assigned_runs}, runs)
        self.assertIsNone(second.next_cursor)
        ReminderRequests(memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="SYNTHETIC_EMULATOR_REMINDER",
                due_at=self.now + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
                delivery_target=DeliveryTarget(kind="device", id=device.id),
            ),
            "snapshot-emulator-notification",
        )
        self.now += timedelta(seconds=1)
        DueReminders(memories.jobs, self.persistence).scan(self.tenant)
        current = snapshots.page(identity).release()
        while current.next_cursor:
            current = snapshots.page(identity, current.next_cursor).release()
        self.assertEqual(len(current.pending_notification_ids), 1)
        notification = current.pending_notification_ids[0]
        leases = DeviceLeases(self.store, self.repo, now=lambda: self.now)
        binding = leases.acquire(identity, "snapshot-emulator-connection")
        delivery = DeviceNotifications(snapshots)
        frame = leases.run(binding, lambda tx: delivery.notification(tx, binding, notification))
        assert frame is not None
        self.assertEqual(frame["delivery_seq"], 1)
        self.assertEqual(
            leases.run(binding, lambda tx: delivery.notification(tx, binding, notification)), frame
        )
        leases.run(
            binding,
            lambda tx: delivery.ack(
                tx, binding, {"notification_id": notification, "status": "displayed"}
            ),
        )
        receipt = self.store.run(
            lambda tx: read_delivery(tx, read_notification(tx, self.tenant, notification))
        )
        self.assertIsNotNone(receipt.displayed_at)
        self.assertIsNone(receipt.acknowledged_at)
        self.now += timedelta(seconds=60)
        expired = snapshots.page(identity).release()
        self.assertIsNone(expired.active_private_session)
        self.assertFalse(expired.assigned_runs)
        self.assertFalse(expired.pending_notification_ids)
        # Expiry is persisted only after every snapshot read, as required by SDK transactions.
        stored = (
            self.db.document(f"tenants/{self.tenant}/private_sessions/{session.id}").get().to_dict()
        )
        assert stored is not None
        self.assertEqual(stored["status"], "expired")

    def test_large_history_bounded_continuation_final_validation_and_offline_text(self) -> None:
        import copy
        from typing import Any
        from unittest.mock import patch

        from brainoid_contracts import ResourceId
        from brainoid_contracts.device_snapshot import SnapshotSelection
        from brainoid_jobs.models import job_id
        from brainoid_persistence.firestore import _Transaction
        from brainoid_runs.state import KIND, run_path

        pairing = self.pairing()
        device = self.confirm(pairing)
        credentials = self.exchange(pairing)
        self.db.document(f"tenants/{self.tenant}/devices/{device.id}").update(
            {"capabilities": ["display"]}
        )
        identity = self.repo.authenticate(credentials.access_token)
        memories = Memories(
            self.store,
            self.tenants,
            self.conversations.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            allow_untracked_bootstrap=True,
            now=lambda: self.now,
        )
        snapshots = DeviceSnapshots(self.conversations, memories, self.sessions)
        conversation = self.conversation()
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(
                text="BOUNDED_HISTORY_SYNTHETIC", output_mode="text", output_device_id=device.id
            ),
            str(uuid4()),
        ).run_id
        source_job = (
            self.db.document(f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, run)}")
            .get()
            .to_dict()
        )
        source_run = self.db.document(run_path(self.tenant, run)).get().to_dict()
        assert source_job is not None and source_run is not None
        expected = {run}
        batch = self.db.batch()
        for index in range(180):
            while True:
                candidate = ResourceId(str(uuid4()))
                key = job_id(self.tenant, KIND, candidate)
                if index >= 140 or key < source_job["id"]:
                    break
            job, record = copy.deepcopy(source_job), copy.deepcopy(source_run)
            job.update(id=key, operation_id=candidate)
            record.update(run_id=candidate)
            if index < 140:
                record["created_at"] = (self.now - timedelta(days=2)).isoformat()
            else:
                expected.add(candidate)
            batch.set(self.db.document(f"tenants/{self.tenant}/jobs/{key}"), job)
            batch.set(self.db.document(run_path(self.tenant, candidate)), record)
        batch.commit()
        ReminderRequests(memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="OFFLINE_TEXT_SYNTHETIC",
                due_at=self.now + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
                delivery_target=DeliveryTarget(kind="device", id=device.id),
            ),
            "history-offline-reminder",
        )
        self.now += timedelta(seconds=1)
        DueReminders(memories.jobs, self.persistence).scan(self.tenant)
        self.now += timedelta(seconds=301)
        original_page, original_get = _Transaction.query_page, _Transaction.get
        reads: list[str] = []
        sizes: list[int] = []

        def page(
            tx: Any, path: str, filters: dict[str, str], *, after: str | None, limit: int
        ) -> Any:
            self.assertLessEqual(limit, 21)
            values = original_page(tx, path, filters, after=after, limit=limit)
            sizes.append(len(values))
            return values

        def get(tx: Any, path: str) -> Any:
            reads.append(path)
            return original_get(tx, path)

        with patch.object(_Transaction, "query_page", page), patch.object(_Transaction, "get", get):
            cursor, found, notices = None, [], []
            pages = 0
            while True:
                sizes.clear()
                reads.clear()
                result = snapshots.page(identity, cursor).release()
                pages += 1
                self.assertLessEqual(sum(sizes), 23)
                self.assertLessEqual(sum("/runs/" in path for path in reads), 20)
                self.assertLess(
                    len(reads), 1500
                )  # Includes all 20 current original/destination source gates.
                found.extend(result.assigned_runs)
                notices.extend(result.pending_notification_ids)
                cursor = result.next_cursor
                if not cursor:
                    break
                # Reconstruct the service to demonstrate no process-local scan state.
                snapshots = DeviceSnapshots(self.conversations, memories, self.sessions)
            self.assertGreater(pages, 8)
            self.assertEqual({r.run_id for r in found}, expected)
            self.assertEqual(len(notices), 1)
            selection = SnapshotSelection(
                snapshot_id=result.snapshot_id,
                run_ids=list(expected),
                command_ids=[],
                notification_ids=notices,
            )
            sizes.clear()
            reads.clear()
            validated = snapshots.validate(identity, selection).release()
            self.assertFalse(sizes)
            self.assertEqual(sum("/runs/" in path for path in reads), 41)
            self.assertLess(len(reads), 3000)
            self.assertEqual({r.run_id for r in validated.assigned_runs}, expected)
            self.assertEqual(validated.pending_notification_ids, notices)
            self.db.document(f"tenants/{self.tenant}/conversations/{conversation}").update(
                {"deleted": True}
            )
            self.assertEqual(snapshots.validate(identity, selection).release().assigned_runs, [])
        leases = DeviceLeases(self.store, self.repo, now=lambda: self.now)
        binding = leases.acquire(identity, "history-bounded-connection")
        delivery = DeviceNotifications(snapshots)
        frame = leases.run(binding, lambda tx: delivery.notification(tx, binding, notices[0]))
        assert frame is not None
        self.assertLess(frame["expires_at"], self.now.isoformat())
        leases.run(
            binding,
            lambda tx: delivery.ack(
                tx, binding, {"notification_id": notices[0], "status": "displayed"}
            ),
        )
        self.assertEqual(
            snapshots.validate(identity, selection).release().pending_notification_ids, []
        )
