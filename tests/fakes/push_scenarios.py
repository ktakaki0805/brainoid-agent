"""APP-05 actual HTTP/Jobs/Dispatcher/Worker scenarios shared with Firestore."""

import copy
import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.routes.agents import reader_identity
from brainoid_api.settings import load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import lifecycle_account_path
from brainoid_memory.notifications import KIND, Notification, delivery_path, notification_path
from brainoid_memory.push_installations import claim_path, installation_path
from brainoid_memory.reminders import Reminder, reminder_path, schedule_intent_path
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.notifications import DispatchNotification
from brainoid_worker.push_notifications import GENERIC_BODY, PushNotifications
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV

from fakes.memory_repository import OTHER, TENANT, USER
from fakes.notification_scenarios import NotificationFixture


class PushScenarios(NotificationFixture):
    store: Any

    def setup_push(self) -> None:
        self.setup_notifications()
        self.uid = USER
        self.cursors = Cursors(b"p" * 32, now=self.clock.now)
        self.tenants = Tenants(self.store, self.cursors, now=self.clock.now, ledger=self.ledger)
        self.memories = Memories(
            self.store,
            self.tenants,
            self.cursors,
            now=self.clock.now,
            ledger=self.ledger,
            allow_untracked_bootstrap=True,
        )
        self.app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, memories=self.memories
        )
        self.app.dependency_overrides[authenticated_identity] = self.auth_identity
        self.app.dependency_overrides[reader_identity] = self.auth_identity
        # Route tests inject all services; startup isolation is tested separately.
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.push_url = "/v1/users/me/push-installations"
        self.url = f"/v1/tenants/{TENANT}/notifications"
        self.transport = FakePush()
        self.push = PushNotifications(self.jobs, TENANT, self.transport)
        self.handler = DispatchNotification(self.jobs, self.push)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    def auth_identity(self) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=self.uid,
            issued_at=self.clock.now() - timedelta(minutes=1),
            authenticated_at=self.clock.now(),
            expires_at=self.clock.now() + timedelta(hours=1),
        )

    def register(self, token: str = "SYNTHETIC_FCM_TOKEN", key: str | None = None) -> Any:
        return self.client.post(
            self.push_url,
            json={"fcm_token": token, "platform": "ios", "app_version": "1"},
            headers={"Idempotency-Key": key or str(uuid4())},
        )

    def refresh(
        self, installation: str, token: str, *, version: int = 1, key: str | None = None
    ) -> Any:
        return self.client.patch(
            self.push_url + "/" + installation,
            json={"fcm_token": token},
            headers={"Idempotency-Key": key or str(uuid4()), "If-Match": f'"{version}"'},
        )

    def ack_http(
        self, record: Any, action: str = "acknowledged", key: str = "notification-ack-key-01"
    ) -> Any:
        return self.client.post(
            self.url + "/" + self.identity(record) + ":ack",
            json={"action": action},
            headers={"Idempotency-Key": key},
        )

    def edit_reminder(self, record: Reminder, **fields: Any) -> Reminder:
        current = self.get(reminder_path(TENANT, record.id))
        response = self.client.patch(
            f"/v1/tenants/{TENANT}/reminders/{record.id}",
            json=fields,
            headers={"Idempotency-Key": str(uuid4()), "If-Match": f'"{current["version"]}"'},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return Reminder.model_validate(self.get(reminder_path(TENANT, record.id)))

    def notification_http(self, record: Reminder, version: int) -> Any:
        response = self.client.get(self.url + "/" + self.identity(record))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version"], version)
        self.assertEqual(response.headers["etag"], f'"{version}"')
        return response.json()

    def member(self, uid: FirebaseUid) -> None:
        self.put(
            f"tenants/{TENANT}/memberships/other",
            {
                "tenant_id": TENANT,
                "user_id": uid,
                "role": "owner",
                "status": "active",
                "authorization_version": 1,
            },
        )

    def test_http_registration_replay_transfer_refresh_remove_and_no_secrets(self) -> None:
        response = self.register(key="registration-key-one")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["etag"], '"1"')
        item = response.json()
        self.assertEqual(set(item), {"id", "version", "created_at", "platform"})
        self.assertEqual(self.register(key="registration-key-one").json(), item)
        self.assertEqual(self.register("different", "registration-key-one").status_code, 409)
        refreshed = self.refresh(item["id"], "NEW_SYNTHETIC_TOKEN", key="refresh-registration-one")
        self.assertEqual(refreshed.json()["version"], 2, refreshed.text)
        self.assertEqual(
            self.refresh(item["id"], "NEW_SYNTHETIC_TOKEN", key="refresh-registration-one").json(),
            refreshed.json(),
        )
        self.assertEqual(self.refresh(item["id"], "other").status_code, 412)
        self.uid = FirebaseUid("other/person")
        self.assertEqual(
            self.refresh(item["id"], "NEW_SYNTHETIC_TOKEN", version=2).status_code, 404
        )
        transferred = self.register("NEW_SYNTHETIC_TOKEN").json()
        self.assertNotEqual(transferred["id"], item["id"])
        self.assertIsNone(self.get(installation_path(ResourceId(item["id"])))["fcm_token"])
        self.assertEqual(self.get(claim_path("NEW_SYNTHETIC_TOKEN"))["user_id"], self.uid)
        removed = self.client.delete(
            self.push_url + "/" + transferred["id"],
            headers={"Idempotency-Key": "remove-installation-1", "If-Match": '"1"'},
        )
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(
            self.client.delete(
                self.push_url + "/" + transferred["id"],
                headers={"Idempotency-Key": "remove-installation-1", "If-Match": '"1"'},
            ).status_code,
            204,
        )
        self.assertIsNone(self.store.run(lambda tx: tx.get(claim_path("NEW_SYNTHETIC_TOKEN"))))
        from brainoid_contracts.access import fingerprint

        receipts = self.store.run(
            lambda tx: tx.collection(
                f"user_operations/{fingerprint(['user', USER])}/idempotency_records"
            )
        )
        self.assertTrue(receipts)
        self.assertNotIn("SYNTHETIC", json.dumps(receipts, default=str))

    def test_http_current_content_ack_pagination_and_old_schedule(self) -> None:
        item = self.register().json()
        records = [self.reminder(due_at=self.clock.now()) for _ in range(2)]
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(len(self.transport.calls), 2)
        record = records[0]
        body = self.client.get(self.url + "/" + self.identity(record))
        self.assertEqual(body.status_code, 200, body.text)
        self.assertEqual(body.json()["status"], "sent")
        self.assertEqual(body.json()["title"], record.title)
        payload = self.transport.calls[0][1]
        self.assertEqual(set(payload), {"notification_id", "tenant_id", "body"})
        self.assertEqual(payload["body"], GENERIC_BODY)
        self.assertNotIn(record.title, json.dumps(self.transport.calls))
        page = self.client.get(self.url, params={"limit": 1}).json()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(
            len(
                self.client.get(
                    self.url, params={"limit": 1, "cursor": page["next_cursor"]}
                ).json()["items"]
            ),
            1,
        )
        self.assertEqual(self.ack_http(record, "displayed").json()["status"], "displayed")
        ack = self.ack_http(record, key="ack-person-confirm-1")
        self.assertEqual(ack.json()["status"], "acknowledged")
        self.assertEqual(self.ack_http(record, key="ack-person-confirm-1").json(), ack.json())
        self.assertEqual(
            self.ack_http(record, "displayed", key="ack-person-confirm-1").status_code, 409
        )
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            400,
        )
        self.update(
            reminder_path(TENANT, record.id),
            schedule_version=2,
            version=4,
            status="scheduled",
            due_at=(self.clock.now() + timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(self.ack_http(record, key="ack-person-confirm-1").status_code, 404)
        self.assertEqual(self.client.get(self.url + "/" + self.identity(record)).status_code, 404)
        self.assertEqual(self.get(reminder_path(TENANT, record.id))["status"], "scheduled")
        self.assertIsNotNone(item)

    def test_public_version_tracks_current_projection_without_changing_source_grant(self) -> None:
        record = self.reminder()
        for title in ("before scan one", "before scan two"):
            record = self.edit_reminder(record, title=title)
        self.assertEqual(record.version, 3)
        self.clock.advance(1)
        self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        source_path = notification_path(TENANT, self.identity(record))
        source, grant = self.get(source_path), self.job(record).grant
        first = self.notification_http(record, 1)
        page = self.client.get(self.url, params={"limit": 1}).json()
        self.assertIsNotNone(page["next_cursor"])
        for version in (2, 3):
            record = self.edit_reminder(record, title=f"same clock edit {version}")
            current = self.notification_http(record, version)
            self.assertEqual(current["title"], record.title)
            self.assertEqual(current["updated_at"], first["updated_at"])
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            400,
        )
        self.clock.advance(1)
        record = self.edit_reminder(record, title="later edit")
        current = self.notification_http(record, 4)
        self.assertGreater(current["updated_at"], first["updated_at"])
        self.register()
        self.deliver()
        sent = self.notification_http(record, 5)
        self.assertEqual(sent["status"], "sent")
        for action, version in (("displayed", 6), ("acknowledged", 7)):
            key = f"version-{action}-key"
            ack = self.ack_http(record, action, key)
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["status"], action)
            self.assertEqual(ack.json()["version"], version)
            self.assertEqual(ack.headers["etag"], f'"{version}"')
            self.assertEqual(ack.json()["updated_at"], sent["updated_at"])
            for replay_key in (key, key + "-other"):
                replay = self.ack_http(record, action, replay_key)
                self.assertEqual(replay.json(), ack.json())
                self.assertEqual(replay.headers["etag"], ack.headers["etag"])
        self.clock.advance(1)
        for action in ("displayed", "acknowledged"):
            replay = self.ack_http(record, action, f"later-{action}-key")
            self.assertEqual(replay.json(), ack.json())
            self.assertEqual(replay.headers["etag"], '"7"')
        self.assertEqual(self.get(source_path), source)
        self.assertEqual(source["version"], 1)
        self.assertEqual(self.job(record).grant, grant)
        self.assertEqual(grant.resource.version, 1)
        old = record
        record = self.edit_reminder(
            record, due_at=(self.clock.now() + timedelta(seconds=1)).isoformat()
        )
        self.assertEqual(self.client.get(self.url + "/" + self.identity(old)).status_code, 404)
        self.assertEqual(self.ack_http(old, key="version-acknowledged-key").status_code, 404)
        self.assertEqual(record.status, "scheduled")
        self.clock.advance(1)
        self.scanner.scan(TENANT)
        self.assertEqual(self.notification_http(record, 1)["status"], "pending")

    def test_legacy_projection_receipt_requires_explicit_replan(self) -> None:
        self.register()
        for invalid in ("projection_version", "reminder_version", "future_reminder_version"):
            with self.subTest(metadata=invalid):
                record = self.reminder(due_at=self.clock.now())
                self.scanner.scan(TENANT)
                source = Notification.model_validate(
                    self.get(notification_path(TENANT, self.identity(record)))
                )
                receipt_path = delivery_path(source)
                receipt = self.get(receipt_path)
                if invalid == "future_reminder_version":
                    receipt["reminder_version"] = record.version + 1
                else:
                    receipt.pop(invalid)
                self.put(receipt_path, receipt)
                self.assertEqual(self.client.get(self.url + "/" + source.id).status_code, 404)
                self.assertEqual(self.ack_http(record, key=str(uuid4())).status_code, 404)
                self.assertNotIn(
                    source.id, [r["id"] for r in self.client.get(self.url).json()["items"]]
                )
                self.dispatcher.dispatch(TENANT, self.job(record).id)
                self.queue.drain(self.worker)
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(
                    self.client.get(f"/v1/tenants/{TENANT}/reminders/{record.id}").status_code, 200
                )
                record = self.edit_reminder(record, status="cancelled")
                self.assertEqual(record.status, "cancelled")
                record = self.edit_reminder(
                    record, due_at=(self.clock.now() + timedelta(seconds=1)).isoformat()
                )
                self.clock.advance(1)
                self.scanner.scan(TENANT)
                self.notification_http(record, 1)
                self.assertEqual(self.get(receipt_path), receipt)

    def test_public_version_when_content_and_ack_change_during_provider_call(self) -> None:
        self.register()
        for action in ("displayed", "acknowledged"):
            with self.subTest(action=action):
                record = self.reminder(due_at=self.clock.now())
                self.scanner.scan(TENANT)
                self.notification_http(record, 1)
                source_path = notification_path(TENANT, self.identity(record))
                source, grant = self.get(source_path), self.job(record).grant

                def concurrent_update(
                    token: str, record: Reminder = record, action: str = action
                ) -> None:
                    self.edit_reminder(record, title="edited while provider request is in flight")
                    self.notification_http(record, 2)
                    response = self.ack_http(record, action, f"in-flight-{action}-key")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["version"], 3)
                    self.assertEqual(response.headers["etag"], '"3"')

                self.transport.after = concurrent_update
                self.deliver()
                # Current person acknowledgement stops result persistence and
                # remaining fanout; no aggregate receipt update is fabricated.
                confirmed = action == "acknowledged"
                current = self.notification_http(record, 3 if confirmed else 4)
                self.assertEqual(current["status"], action)
                self.assertEqual(current["title"], "edited while provider request is in flight")
                self.assertEqual(self.get(source_path), source)
                self.assertEqual(self.job(record).grant, grant)
                self.assertEqual(self.job(record).status, "obsolete" if confirmed else "succeeded")
                self.assertEqual(
                    self.ack_http(record, action, f"in-flight-{action}-key").json(), current
                )
                calls = len(self.transport.calls)
                self.deliver()
                self.assertEqual(len(self.transport.calls), calls)
                self.assertEqual(self.notification_http(record, 3 if confirmed else 4), current)

    def test_recipient_tenant_membership_rejoin_account_and_legacy_denials(self) -> None:
        self.register()
        record = self.reminder(due_at=self.clock.now(), visibility="shared", owner_user_id=None)
        self.scanner.scan(TENANT)
        self.member(FirebaseUid("other/uid"))
        self.uid = FirebaseUid("other/uid")
        self.assertEqual(self.client.get(self.url + "/" + self.identity(record)).status_code, 404)
        self.assertEqual(self.ack_http(record).status_code, 404)
        self.assertEqual(self.client.get(self.url).json()["items"], [])
        self.uid = USER
        self.assertEqual(
            self.client.get(
                f"/v1/tenants/{OTHER}/notifications/{self.identity(record)}"
            ).status_code,
            404,
        )
        self.update(f"tenants/{TENANT}/memberships/safe-key", status="revoked")
        self.assertNotEqual(
            self.client.get(self.url + "/" + self.identity(record)).status_code, 200
        )
        self.update(
            f"tenants/{TENANT}/memberships/safe-key", status="active", authorization_version=2
        )
        self.assertEqual(self.client.get(self.url + "/" + self.identity(record)).status_code, 404)
        self.deliver()
        self.assertEqual(self.transport.calls, [])
        self.put(lifecycle_account_path(USER), {"mode": "delete_source"})
        self.assertEqual(self.register().status_code, 403)
        self.store.run(lambda tx: tx.delete(lifecycle_account_path(USER)))
        fresh = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        path = notification_path(TENANT, self.identity(fresh))
        self.update(path, recipient_stamp=None, recipient_account_stamp=None)
        self.assertEqual(self.client.get(self.url + "/" + self.identity(fresh)).status_code, 404)
        self.deliver()
        self.assertEqual(self.transport.calls, [])

    def test_partial_success_invalid_and_refresh_during_callback(self) -> None:
        good = self.register("GOOD").json()
        bad = self.register("BAD").json()
        self.transport.results = {"GOOD": "accepted", "BAD": "invalid"}
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        self.transport.after = lambda token: (
            self.assertEqual(self.refresh(bad["id"], "REFRESHED").status_code, 200)
            if token == "BAD"
            else None
        )
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(
            self.get(installation_path(ResourceId(bad["id"])))["fcm_token"], "REFRESHED"
        )
        self.assertEqual(
            self.client.get(self.url + "/" + self.identity(record)).json()["status"], "sent"
        )
        self.assertEqual(self.get(installation_path(ResourceId(good["id"])))["status"], "active")
        self.transport.after = lambda token: None
        self.transport.results = {"GOOD": "accepted", "REFRESHED": "invalid"}
        next_record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(self.get(installation_path(ResourceId(bad["id"])))["status"], "disabled")
        self.assertIsNone(self.get(installation_path(ResourceId(bad["id"])))["fcm_token"])
        self.assertEqual(self.job(next_record).status, "succeeded")

    def test_partial_unknown_never_retries_or_fabricates_lookup_evidence(self) -> None:
        self.register("GOOD")
        self.register("UNKNOWN")
        self.transport.results = {"GOOD": "accepted", "UNKNOWN": "unknown"}
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        self.deliver()
        self.assertEqual(self.job(record).reason, "outcome_unknown")
        self.assertFalse(self.handler.reconcile_unknown(TENANT, self.job(record).id))
        before = copy.deepcopy(self.transport.calls)
        self.deliver()
        self.assertEqual(self.transport.calls, before)
        self.assertEqual(
            self.client.get(self.url + "/" + self.identity(record)).json()["status"], "pending"
        )
        path = notification_path(TENANT, self.identity(record))
        records = self.store.run(lambda tx: tx.collection(path + "/push_batches"))
        self.assertEqual({x["state"] for x in records[0][1]["targets"]}, {"accepted", "unknown"})
        self.assertNotIn("GOOD", json.dumps(records))

    def test_no_installation_still_has_inbox_and_limit_is_explicit(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        self.notification_http(record, 1)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(self.notification_http(record, 2)["status"], "pending")
        self.deliver()
        self.notification_http(record, 2)
        for index in range(20):
            self.assertEqual(self.register(f"CAP-{index}").status_code, 201)
        self.assertEqual(self.register("CAP-21").status_code, 403)
        self.assertEqual(self.register("CAP-0").status_code, 201)

    def test_shared_target_rejoin_before_first_scan_and_legacy_intent_require_replan(self) -> None:
        other = FirebaseUid("recipient/other")
        self.member(other)
        record = self.reminder(
            due_at=self.clock.now(),
            visibility="shared",
            owner_user_id=None,
            delivery_target={"kind": "user", "id": other},
        )
        self.update(f"tenants/{TENANT}/memberships/other", authorization_version=2)
        self.assertEqual(self.scanner.scan(TENANT).created, 0)
        self.update(
            reminder_path(TENANT, record.id),
            schedule_version=2,
            version=2,
            due_at=(self.clock.now() + timedelta(seconds=1)).isoformat(),
        )
        self.clock.advance(1)
        self.assertEqual(self.scanner.scan(TENANT).created, 1)
        fresh = self.reminder(due_at=self.clock.now())
        from brainoid_memory.reminders import ScheduleReference

        path = schedule_intent_path(
            ScheduleReference(tenant_id=TENANT, reminder_id=fresh.id, schedule_version=1)
        )
        self.update(path, recipient_stamp=None, recipient_account_stamp=None)
        self.assertEqual(self.scanner.scan(TENANT).created, 0)

    def test_real_fcm_http_transport_payload_single_post_and_error_classification(self) -> None:
        from brainoid_api.adapters.fcm import FCM
        from google.auth.credentials import AnonymousCredentials
        from requests import Response

        transport = FCM(project="demo-brainoid", credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(transport.close)
        self.push.transport = transport
        self.register("SYNTHETIC_HTTP_TOKEN")
        captured = []
        response = Response()
        response.status_code = 200
        response._content = b'{"name":"projects/demo-brainoid/messages/synthetic"}'

        def http(request: Any, **options: Any) -> Response:
            captured.append((request, options))
            return response

        with patch.object(transport.session, "send", side_effect=http):
            record = self.reminder(due_at=self.clock.now())
            self.scanner.scan(TENANT)
            self.deliver()
            self.assertEqual(self.job(record).status, "succeeded")
            self.assertEqual(len(captured), 1)
            request, options = captured[0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url, "https://fcm.googleapis.com/v1/projects/demo-brainoid/messages:send"
            )
            message = json.loads(request.body)["message"]
            self.assertEqual(set(message), {"token", "data", "notification"})
            self.assertEqual(set(message["data"]), {"notification_id", "tenant_id", "body"})
            self.assertEqual(message["notification"], {"body": GENERIC_BODY})
            self.assertNotIn(record.title, request.body.decode())
            self.assertEqual(options["timeout"], 10)
            self.assertFalse(options["allow_redirects"])
            from requests.adapters import HTTPAdapter

            adapter = transport.session.get_adapter(request.url)
            assert isinstance(adapter, HTTPAdapter)
            self.assertEqual(adapter.max_retries.total, 0)
            data = message["data"]
            for status, payload, expected in (
                (401, {"error": {}}, "rejected"),
                (400, {"error": {"status": "INVALID_ARGUMENT"}}, "rejected"),
                (404, {"error": {}}, "rejected"),
                (
                    404,
                    {
                        "error": {
                            "details": [
                                {
                                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                                    "errorCode": "UNREGISTERED",
                                }
                            ]
                        }
                    },
                    "invalid",
                ),
                (503, {"error": {}}, "unknown"),
                (302, {}, "unknown"),
            ):
                response.status_code = status
                response._content = json.dumps(payload).encode()
                before = len(captured)
                self.assertEqual(transport.send("SYNTHETIC_HTTP_TOKEN", data), expected)
                self.assertEqual(len(captured), before + 1)
        with patch.object(
            transport.session, "send", side_effect=TimeoutError("SYNTHETIC_HTTP_TOKEN")
        ) as called:
            self.assertEqual(transport.send("SYNTHETIC_HTTP_TOKEN", data), "unknown")
            self.assertEqual(called.call_count, 1)
        with self.assertRaises(ValueError):
            transport.send("token", {**data, "title": "PRIVATE"})

    def test_ledger_deny_between_installations_and_before_body_fetch(self) -> None:
        from brainoid_policy.control_ledger import Change, MemberTarget
        from brainoid_policy.models import Role

        self.register("ONE")
        self.register("TWO")
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        change = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=TENANT,
            expected_version=1,
            authorization_version=2,
            effect="deny",
            target=MemberTarget(user_id=USER, role=Role.OWNER, status="revoked"),
        )
        self.transport.after = lambda token: self.ledger.prepare(change)
        self.deliver()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.job(record).status, "obsolete")
        self.assertNotEqual(
            self.client.get(self.url + "/" + self.identity(record)).status_code, 200
        )
        self.assertNotEqual(self.ack_http(record).status_code, 200)

    def test_registration_and_ack_unknown_commit_replay(self) -> None:
        from brainoid_api.errors import APIError, ErrorCode

        original = self.store.run
        lost: list[bool] = []

        def unknown(work: Any) -> Any:
            result = original(work)
            from brainoid_contracts.idempotency import MutationResult

            if isinstance(result, MutationResult) and not lost:
                lost.append(True)
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            return result

        # API's shared adapter maps real unavailable DB commits. This deliberately
        # injects the same ambiguous result behind actual endpoint authorization.
        with patch.object(self.store, "run", side_effect=unknown):
            self.assertEqual(self.register("LOST", "registration-lost-key").status_code, 503)
        self.assertEqual(self.register("LOST", "registration-lost-key").status_code, 201)
        self.assertEqual(
            len(self.store.run(lambda tx: tx.query("push_installations", {"user_id": USER}))), 1
        )
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        lost.clear()
        with patch.object(self.store, "run", side_effect=unknown):
            self.assertEqual(self.ack_http(record).status_code, 503)
        before = self.get(reminder_path(TENANT, record.id))["version"]
        self.assertEqual(self.ack_http(record).status_code, 200)
        self.assertEqual(self.get(reminder_path(TENANT, record.id))["version"], before)
        self.notification_http(record, 2)
        self.deliver()
        self.assertEqual(self.transport.calls, [])
        self.notification_http(record, 2)

    def test_local_confirmed_evidence_recovers_lost_aggregate_response(self) -> None:
        self.register()
        record = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        send = self.push.send

        def lost(key: str, payload: Any) -> Any:
            send(key, payload)
            raise RuntimeError("synthetic lost aggregate response")

        with patch.object(self.push, "send", side_effect=lost):
            self.deliver()
        self.assertEqual(self.job(record).reason, "outcome_unknown")
        self.assertEqual(self.notification_http(record, 1)["status"], "pending")
        record = self.edit_reminder(record, title="current title during unknown outcome")
        self.notification_http(record, 2)
        self.assertTrue(self.handler.reconcile_unknown(TENANT, self.job(record).id))
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.notification_http(record, 3)["status"], "sent")
        self.assertEqual(self.job(record).grant.resource.version, 1)

    def test_exact_lease_generation_before_prepare_and_during_fanout(self) -> None:
        for phase in ("prepare", "fanout"):
            with self.subTest(phase=phase):
                self.register("LEASE_ONE", str(uuid4()))
                self.register("LEASE_TWO", str(uuid4()))
                record = self.reminder(due_at=self.clock.now())
                self.scanner.scan(TENANT)
                original = self.push.send
                changed: list[Any] = []

                def change(record: Any = record, changed: list[Any] = changed) -> None:
                    path = f"tenants/{TENANT}/jobs/{self.job(record).id}"
                    job = self.get(path)
                    self.update(path, lease_generation=job["lease_generation"] + 1)
                    changed.append(self.get(path))

                def delayed(key: str, payload: Any, original: Any = original) -> Any:
                    change()
                    return original(key, payload)

                before = len(self.transport.calls)
                if phase == "prepare":
                    with patch.object(self.push, "send", side_effect=delayed):
                        self.deliver()
                else:
                    self.transport.after = lambda token: change()
                    self.deliver()
                    self.transport.after = lambda token: None
                self.assertEqual(len(self.transport.calls) - before, 0 if phase == "prepare" else 1)
                self.assertEqual(
                    self.get(f"tenants/{TENANT}/jobs/{self.job(record).id}"), changed[0]
                )

    def test_snapshot_refresh_skips_old_destination_and_unknown_start_never_sends(self) -> None:
        from brainoid_persistence.firestore import PersistenceUnavailable

        registrations = {
            token: self.register(token).json() for token in ("BEFORE_ONE", "BEFORE_TWO")
        }
        self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)

        def refreshed(token: str) -> None:
            other = next(t for t in registrations if t != token)
            self.assertEqual(
                self.refresh(registrations[other]["id"], "AFTER_REFRESH").status_code, 200
            )

        self.transport.after = refreshed
        self.deliver()
        self.assertEqual(len(self.transport.calls), 1)
        self.transport.after = lambda token: None
        pending = self.reminder(due_at=self.clock.now())
        self.scanner.scan(TENANT)
        original = self.store.run
        lost: list[bool] = []
        path = notification_path(TENANT, self.identity(pending)) + "/push_batches"

        def unknown(work: Any) -> Any:
            result = original(work)
            batches = original(lambda tx: tx.collection(path))
            if not lost and any(t["state"] == "started" for _, b in batches for t in b["targets"]):
                lost.append(True)
                raise PersistenceUnavailable()
            return result

        before = len(self.transport.calls)
        with patch.object(self.store, "run", side_effect=unknown):
            with self.assertRaises(PersistenceUnavailable):
                self.deliver()
        self.clock.advance(5)
        self.deliver()
        self.assertEqual(len(self.transport.calls), before)
        self.assertEqual(self.job(pending).reason, "outcome_unknown")
        self.assertFalse(self.handler.reconcile_unknown(TENANT, self.job(pending).id))


class FakePush:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.results: dict[str, Any] = {}
        self.after: Any = lambda token: None

    def send(self, token: str, data: dict[str, str]) -> Any:
        self.calls.append((token, data))
        self.after(token)
        return self.results.get(token, "accepted")
