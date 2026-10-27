"""Actual MEM-09 archive includes the recipient projection and rechecks its manifest."""

import json
from datetime import timedelta

from brainoid_api.repositories.push_installations import PushInstallations
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_jobs.models import job_id
from brainoid_memory.notifications import KIND, acknowledge, notification_id
from brainoid_memory.push_installations import PushCreate
from brainoid_memory.reminders import ReminderCreate, ReminderPatch, ScheduleReference
from brainoid_worker.entry import Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.notifications import DispatchNotification, DueReminders
from brainoid_worker.push_notifications import PushNotifications
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
from fakes.push_scenarios import FakePush
from test_exports import ExportFixture


class NotificationExportTests(ExportFixture):
    def test_archive_uses_current_body_and_ack_invalidates_download(self) -> None:
        record = ReminderRequests(self.memories).create(
            self.identity(),
            TENANT,
            ReminderCreate.model_validate(
                {
                    "title": "NOTIFICATION_EXPORT_TITLE",
                    "due_at": self.now + timedelta(seconds=1),
                    "timezone": "UTC",
                    "visibility": "private",
                    "display_policy": "never",
                    "delivery_target": {"kind": "user", "id": self.identity().user_id},
                }
            ),
            "notification-export-create",
        )
        self.now += timedelta(seconds=1)
        self.assertEqual(
            DueReminders(self.memories.jobs, FakePurgePages(self.store)).scan(TENANT).created, 1
        )
        PushInstallations(self.tenants).mutate(
            self.identity(),
            PushCreate(fcm_token="SYNTHETIC_EXPORT_TOKEN", platform="ios", app_version="1"),
            "export-push-registration",
        )
        notification = notification_id(
            ScheduleReference(tenant_id=TENANT, reminder_id=record.id, schedule_version=1)
        )
        push = PushNotifications(self.memories.jobs, TENANT, FakePush())
        worker = Worker(
            self.memories.jobs,
            LocalCaller(),
            {KIND: DispatchNotification(self.memories.jobs, push)},
        )
        delivery = self.memories.jobs.prepare_dispatch(TENANT, job_id(TENANT, KIND, notification))
        assert delivery is not None
        worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
        operation = self.ready()
        archive = self.archive(operation)
        notification = next(r["data"] for r in archive["records"] if r["kind"] == "notifications")
        self.assertEqual(notification["title"], record.title)
        self.assertNotIn("scheduled_by", notification)
        self.assertNotIn("SYNTHETIC_EXPORT_TOKEN", json.dumps(archive))
        self.assertNotIn("push_batches", json.dumps(archive))
        self.assertNotIn("recipient_stamp", json.dumps(archive))
        self.assertEqual(notification["version"], 2)
        ReminderRequests(self.memories).update(
            self.identity(),
            TENANT,
            record.id,
            ReminderPatch(title="CURRENT_EXPORT_TITLE"),
            "notification-export-title-edit",
            '"2"',
        )
        self.assertNotEqual(self.download(operation).status_code, 200)
        operation = self.create_export(key="notification-export-after-title")
        self.generate(operation)
        archive = self.archive(operation)
        notification = next(r["data"] for r in archive["records"] if r["kind"] == "notifications")
        self.assertEqual(notification["title"], "CURRENT_EXPORT_TITLE")
        self.assertEqual(notification["version"], 3)
        from brainoid_contracts import ResourceId

        self.store.run(
            lambda tx: acknowledge(
                self.memories._open(tx, self.identity(), TENANT, None),
                ResourceId(notification["id"]),
                "acknowledged",
            )
        )
        self.assertNotEqual(self.download(operation).status_code, 200)
