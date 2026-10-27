"""Current authenticated device content access cannot acknowledge a person's reminder."""

from datetime import timedelta

from brainoid_api.auth.runtime import authenticated_identity
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_contracts import ResourceId
from brainoid_memory.notifications import notification_id
from brainoid_memory.reminders import ScheduleReference
from brainoid_worker.notifications import DueReminders
from fakes.memory_repository import TENANT
from fakes.purge import FakePurgePages
from test_notes import NotesFixture


class NotificationDeviceTests(NotesFixture):
    def test_current_device_session_scope_expiry_and_user_only_installation(self) -> None:
        device = self.device()
        self.store.data[f"tenants/{TENANT}/devices/{device.device_id}"]["capabilities"] = [
            "display"
        ]
        created = self.client.post(
            f"/v1/tenants/{TENANT}/reminders",
            json={
                "title": "PRIVATE_DEVICE_NOTIFICATION",
                "due_at": (self.now + timedelta(seconds=1)).isoformat(),
                "timezone": "UTC",
                "delivery_target": {"kind": "device", "id": device.device_id},
                "visibility": "private",
                "display_policy": "authenticated_session",
            },
            headers={"Idempotency-Key": "device-notification-source"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.now += timedelta(seconds=1)
        DueReminders(self.memories.jobs, FakePurgePages(self.store)).scan(TENANT)
        notification = notification_id(
            ScheduleReference(
                tenant_id=TENANT, reminder_id=ResourceId(created.json()["id"]), schedule_version=1
            )
        )
        url = f"/v1/tenants/{TENANT}/notifications/{notification}"
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        )
        session = sessions.start(
            self.identity(),
            TENANT,
            device.device_id,
            PrivateSessionCreate(allowed_scopes=["reminders"], acknowledge_shared_space=True),
            "device-notification-session",
            "device-notification-request",
        )
        del self.app.dependency_overrides[reader_identity]
        del self.app.dependency_overrides[authenticated_identity]
        headers = {
            "Authorization": "Bearer " + device.access_token,
            "X-Private-Session": session.id,
        }
        self.assertEqual(self.client.get(url, headers=headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                url + ":ack",
                json={"action": "displayed"},
                headers={**headers, "Idempotency-Key": "device-display-receipt"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                url + ":ack",
                json={"action": "acknowledged"},
                headers={**headers, "Idempotency-Key": "device-person-receipt"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/v1/users/me/push-installations",
                json={"fcm_token": "DEVICE_TOKEN", "platform": "ios", "app_version": "1"},
                headers={**headers, "Idempotency-Key": "device-registration-1"},
            ).status_code,
            401,
        )
        self.now += timedelta(seconds=61)
        self.assertNotEqual(self.client.get(url, headers=headers).status_code, 200)
        self.assertNotEqual(
            self.client.post(
                url + ":ack",
                json={"action": "displayed"},
                headers={**headers, "Idempotency-Key": "device-display-receipt"},
            ).status_code,
            200,
        )
