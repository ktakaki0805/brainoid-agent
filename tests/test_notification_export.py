"""Actual MEM-09 archive includes the recipient projection and rechecks its manifest."""

from datetime import timedelta

from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_memory.notifications import acknowledge
from brainoid_memory.reminders import ReminderCreate
from brainoid_worker.notifications import DueReminders
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
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
        operation = self.ready()
        archive = self.archive(operation)
        notification = next(r["data"] for r in archive["records"] if r["kind"] == "notifications")
        self.assertEqual(notification["title"], record.title)
        self.assertNotIn("scheduled_by", notification)
        from brainoid_contracts import ResourceId

        self.store.run(
            lambda tx: acknowledge(
                self.memories._open(tx, self.identity(), TENANT, None),
                ResourceId(notification["id"]),
                "acknowledged",
            )
        )
        self.assertNotEqual(self.download(operation).status_code, 200)
