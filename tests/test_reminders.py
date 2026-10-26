"""APP-03 / FR-009 / T-038: canonical schedules via HTTP and default tools."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_agent_core.types import ToolCall
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.repositories.tasks import TaskRequests
from brainoid_contracts import ResourceId
from brainoid_memory.reminders import (
    Reminder,
    ReminderCreate,
    ReminderPatch,
    ReminderSnooze,
    ScheduleReference,
    current_schedule,
    reminder_path,
)
from brainoid_memory.tasks import TaskCreate
from brainoid_worker.reminders import RemindersRuntime
from fakes.memory_repository import OTHER, TENANT, USER
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_exports import ExportFixture
from test_notes import NotesFixture, NotesWorkerFixture

REMINDER = {
    "title": "Alex reminder",
    "due_at": "2026-10-28T00:30:00+09:00",
    "timezone": "Asia/Tokyo",
    "delivery_target": {"kind": "user", "id": USER},
    "visibility": "shared",
    "display_policy": "shared_display",
}
FUTURE = {"due_at": "2026-11-01T01:30:00-05:00", "timezone": "America/New_York"}


def command(body: dict[str, Any]) -> str:
    normalized = ReminderCreate.model_validate(body).model_dump(mode="json", exclude_unset=True)
    return "Create reminder " + json.dumps(normalized, ensure_ascii=False, sort_keys=True)


class RemindersHTTPTests(NotesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = f"/v1/tenants/{TENANT}/reminders"
        self.reminders = ReminderRequests(self.memories)

    def create(self, key: str = "reminder-create-key-001", **changes: Any) -> Any:
        return self.client.post(
            self.url, json={**REMINDER, **changes}, headers={"Idempotency-Key": key}
        )

    def update(
        self, identity: str, version: int = 1, key: str = "reminder-update-key-001", **changes: Any
    ) -> Any:
        return self.client.patch(
            self.url + "/" + identity,
            json=changes,
            headers={"Idempotency-Key": key, "If-Match": f'"{version}"'},
        )

    def snooze(
        self, identity: str, version: int = 1, key: str = "reminder-snooze-key-001", **changes: Any
    ) -> Any:
        return self.client.post(
            self.url + "/" + identity + ":snooze",
            json={**FUTURE, "expected_version": version, **changes},
            headers={"Idempotency-Key": key},
        )

    def test_http_contract_normalization_title_dates_cancel_snooze_and_replay(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        identity = record["id"]
        self.assertEqual(record["due_at"], "2026-10-27T15:30:00Z")
        self.assertEqual(record["timezone"], "Asia/Tokyo")
        self.assertEqual(
            (record["version"], record["schedule_version"], record["status"]), (1, 1, "scheduled")
        )
        self.assertEqual(self.create().json(), record)
        self.assertEqual(self.client.get(self.url + "/" + identity).json(), record)
        page = self.client.get(self.url).json()
        self.assertIsNone(page["next_cursor"])
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        for name, value in (("Reminder", record), ("ReminderPage", page)):
            Draft202012Validator(
                {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
            ).validate(value)
        title = self.update(identity, title="New title")
        self.assertEqual((title.json()["version"], title.json()["schedule_version"]), (2, 1))
        dates = self.update(identity, 2, "reminder-dates-key-01", **FUTURE)
        self.assertEqual(dates.json()["due_at"], "2026-11-01T06:30:00Z")
        self.assertEqual((dates.json()["version"], dates.json()["schedule_version"]), (3, 2))
        timezone = self.update(identity, 3, "reminder-timezone-01", timezone="UTC")
        self.assertEqual(timezone.json()["schedule_version"], 3)
        cancel = self.update(identity, 4, "reminder-cancel-key-01", status="cancelled")
        self.assertEqual(
            (cancel.json()["version"], cancel.json()["schedule_version"], cancel.json()["status"]),
            (5, 4, "cancelled"),
        )
        self.assertEqual(
            self.update(identity, 4, "reminder-cancel-key-01", status="cancelled").json(),
            cancel.json(),
        )
        snoozed = self.snooze(identity, 5)
        self.assertEqual(snoozed.status_code, 200, snoozed.text)
        self.assertEqual(
            (
                snoozed.json()["version"],
                snoozed.json()["schedule_version"],
                snoozed.json()["status"],
            ),
            (6, 5, "scheduled"),
        )
        self.assertEqual(self.snooze(identity, 5).json(), snoozed.json())
        self.assertEqual(self.snooze(identity, 1, "stale-snooze-key-001").status_code, 412)
        self.assertEqual(
            self.update(identity, key="stale-cancel-key-001", status="cancelled").status_code, 412
        )
        self.assertEqual(
            self.client.get(self.url, params={"status": "cancelled"}).json()["items"], []
        )

    def test_t038_notified_schedule_rejects_delayed_job_ack_and_completion(self) -> None:
        identity = self.create().json()["id"]
        path = reminder_path(TENANT, ResourceId(identity))
        self.store.data[path]["status"] = "dispatched"
        old = ScheduleReference(tenant_id=TENANT, reminder_id=identity, schedule_version=1)
        self.assertTrue(current_schedule(Reminder.model_validate(self.store.data[path]), old))
        response = self.snooze(identity)
        self.assertEqual(response.status_code, 200, response.text)
        before = copy.deepcopy(self.store.data[path])
        for event in ("job", "ack", "completion"):
            # APP04/05 consumers must call this actual domain fence before writes/I/O.
            def delayed(tx: Any) -> None:
                record = Reminder.model_validate(tx.get(path))
                if current_schedule(record, old):
                    tx.set(path, {**record.model_dump(mode="json"), "status": "acknowledged"})

            self.store.run(delayed)
            self.assertEqual(self.store.data[path], before, event)
        current = ScheduleReference(tenant_id=TENANT, reminder_id=identity, schedule_version=2)
        self.assertTrue(current_schedule(Reminder.model_validate(before), current))
        self.assertFalse(
            current_schedule(
                Reminder.model_validate(before), current.model_copy(update={"tenant_id": OTHER})
            )
        )
        self.update(identity, 2, "cancel-after-snooze", status="cancelled")
        self.assertFalse(current_schedule(Reminder.model_validate(self.store.data[path]), current))

    def test_dates_dst_past_validation_headers_and_pagination(self) -> None:
        identity = self.create().json()["id"]
        for value in ("2020-01-01T00:00:00Z", self.now.isoformat()):
            self.assertEqual(self.create("past-reminder-key-01", due_at=value).status_code, 400)
            self.assertEqual(self.snooze(identity, due_at=value).status_code, 400)
        for changes in (
            {},
            {"title": None},
            {"due_at": None},
            {"timezone": "Mars/Fake"},
            {"status": "scheduled"},
            {"snooze": True},
            {"due_at": "2026-10-28T09:00:00"},
            {"due_at": "2026-10-28T09:00:00-00:00"},
            {"delivery_target": REMINDER["delivery_target"]},
        ):
            self.assertEqual(self.update(identity, **changes).status_code, 422, changes)
        self.assertEqual(
            self.client.patch(self.url + "/" + identity, json={"title": "x"}).status_code, 428
        )
        self.assertEqual(
            self.client.post(self.url + "/" + identity + ":snooze", json=FUTURE).status_code, 422
        )
        self.assertEqual(
            self.create(
                "dst-reminder-key-01",
                due_at="2026-11-01T01:30:00-04:00",
                timezone="America/New_York",
            ).json()["due_at"],
            "2026-11-01T05:30:00Z",
        )
        gap = self.create(
            "dst-gap-reminder-01", due_at="2027-03-14T02:30:00-05:00", timezone="America/New_York"
        )
        self.assertEqual(gap.json()["due_at"], "2027-03-14T07:30:00Z")
        first = self.client.get(self.url, params={"limit": 2}).json()
        second = self.client.get(
            self.url, params={"limit": 2, "cursor": first["next_cursor"]}
        ).json()
        self.assertIsNone(second["next_cursor"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.update(identity, title="changed")
        self.assertNotEqual(
            self.client.get(
                self.url, params={"limit": 2, "cursor": first["next_cursor"]}
            ).status_code,
            200,
        )

    def test_target_scope_task_association_current_membership_and_control_ledger(self) -> None:
        shared = self.create().json()["id"]
        private = self.create(
            "private-reminder-01", visibility="private", display_policy="never"
        ).json()["id"]
        self.member()
        opaque = self.uid
        target = {"kind": "user", "id": opaque}
        self.assertEqual(self.create("opaque-reminder-01", delivery_target=target).status_code, 201)
        self.assertEqual(
            self.create(
                "private-other-user1", visibility="private", display_policy="never"
            ).status_code,
            404,
        )
        self.assertEqual(self.update(shared, title="forbidden").status_code, 403)
        self.assertEqual(self.update(private, title="forbidden").status_code, 404)
        self.store.data[f"tenants/{TENANT}/memberships/another"]["role"] = "admin"
        self.assertEqual(self.update(shared, title="allowed").status_code, 200)
        self.assertEqual(self.snooze(private).status_code, 404)
        self.uid = USER
        self.store.data[f"tenants/{TENANT}/memberships/another"]["status"] = "revoked"
        self.assertEqual(
            self.create("revoked-target-key1", delivery_target=target).status_code, 404
        )
        self.store.data[f"tenants/{TENANT}/memberships/another"].update(
            status="active", authorization_operation_id=str(uuid4())
        )
        self.assertEqual(
            self.create("unproved-target-key", delivery_target=target).status_code, 404
        )
        self.assertEqual(
            self.create(
                "prefix-target-key01", delivery_target={"kind": "user", "id": "another/user"}
            ).status_code,
            404,
        )
        task = TaskRequests(self.memories).create(
            self.identity(),
            TENANT,
            TaskCreate.model_validate(
                {"title": "Private task", "visibility": "private", "display_policy": "never"}
            ),
            "private-task-key-01",
        )
        self.assertEqual(self.create("private-task-shared", task_id=task.id).status_code, 404)
        related = self.create(
            "private-task-linked", task_id=task.id, visibility="private", display_policy="never"
        )
        self.assertEqual(related.status_code, 201, related.text)
        self.store.data[f"tenants/{TENANT}/tasks/{task.id}"]["deleted"] = True
        self.assertEqual(self.client.get(self.url + "/" + related.json()["id"]).status_code, 404)
        self.assertEqual(self.create("foreign-task-key01", task_id=str(uuid4())).status_code, 404)

    def test_device_target_and_unidentified_pi_create_snooze_scope(self) -> None:
        from brainoid_api.routes.agents import reader_identity
        from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions

        device = self.device()
        target = {"kind": "device", "id": device.device_id}
        self.assertEqual(self.create(delivery_target=target).status_code, 201)
        self.assertEqual(
            self.create(
                "private-device-never",
                delivery_target=target,
                visibility="private",
                display_policy="never",
            ).status_code,
            404,
        )
        self.assertEqual(
            self.create(
                "private-device-auth",
                delivery_target=target,
                visibility="private",
                display_policy="authenticated_session",
            ).status_code,
            201,
        )
        self.assertEqual(
            self.create(
                "invalid-device-path", delivery_target={"kind": "device", "id": "../bad"}
            ).status_code,
            404,
        )
        identity = self.create("pi-snooze-source-01").json()["id"]
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + device.access_token,
            "Idempotency-Key": "pi-reminder-create",
        }
        self.assertEqual(
            self.client.post(self.url, json=REMINDER, headers=headers).status_code, 403
        )
        self.assertNotEqual(
            self.client.post(
                self.url + "/" + identity + ":snooze",
                json={**FUTURE, "expected_version": 1},
                headers=headers,
            ).status_code,
            200,
        )
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        )
        for scope in ("notes", "reminders"):
            session = sessions.start(
                self.identity(),
                TENANT,
                device.device_id,
                PrivateSessionCreate(allowed_scopes=[scope], acknowledge_shared_space=True),
                "pi-" + scope + "-session",
                "pi-" + scope + "-request",
            )
            headers["X-Private-Session"] = session.id
            response = self.client.post(self.url, json=REMINDER, headers=headers)
            if scope == "notes":
                self.assertNotEqual(response.status_code, 201)
                self.now += timedelta(seconds=61)
            else:
                self.assertEqual(response.status_code, 201, response.text)
                headers["Idempotency-Key"] = "pi-reminder-snooze"
                self.assertEqual(
                    self.client.post(
                        self.url + "/" + identity + ":snooze",
                        json={**FUTURE, "expected_version": 1},
                        headers=headers,
                    ).status_code,
                    200,
                )

    def test_unknown_commits_recover_one_create_and_one_snooze(self) -> None:
        lost: list[bool] = []

        def fail() -> None:
            if any("/reminders/" in p for p in self.store.data) and not lost:
                lost.append(True)
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.store.after_commit = fail
        self.assertEqual(self.create().status_code, 503)
        self.store.after_commit = lambda: None
        identity = self.create().json()["id"]
        self.assertEqual(sum("/reminders/" in p for p in self.store.data), 1)
        lost.clear()
        self.store.after_commit = fail
        self.assertEqual(self.snooze(identity).status_code, 503)
        self.store.after_commit = lambda: None
        self.assertEqual(self.snooze(identity).json()["schedule_version"], 2)
        self.assertEqual(self.client.get(self.url + "/" + identity).json()["version"], 2)


class RemindersWorkerFixture(NotesWorkerFixture):
    def reminder(self, **changes: Any) -> Any:
        body = {
            **REMINDER,
            "delivery_target": {"kind": "user", "id": self.identity().user_id},
            **changes,
        }
        return ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate.model_validate(body),
            "reminder-key-" + str(uuid4()),
        )

    def prepare_update(self, record: Any, *, snooze: bool = False) -> ResourceId:
        changes = (
            ReminderSnooze.model_validate({**FUTURE, "expected_version": 1}).model_dump(
                mode="json", exclude={"expected_version"}
            )
            if snooze
            else {"status": "cancelled"}
        )
        arguments = (
            {"reminder_id": record.id, "expected_version": record.version, **changes}
            if snooze
            else {"reminder_id": record.id, "expected_version": record.version, "changes": changes}
        )
        name = "reminders.snooze" if snooze else "reminders.update"
        self.configure_calls(
            (
                ToolCall(call_key="list", name="reminders.list", arguments_json="{}"),
                ToolCall(call_key="update", name=name, arguments_json=json.dumps(arguments)),
            )
        )
        text = (
            ("Snooze" if snooze else "Update")
            + f" reminder {record.id}: "
            + json.dumps(changes, sort_keys=True)
        )
        return ResourceId(self.turn(self.conversation(), text=text).json()["run_id"])


class RemindersWorkerTests(RemindersWorkerFixture):
    def test_default_create_matches_api_and_direct_intent_is_required(self) -> None:
        record = self.reminder()
        body = {**REMINDER, "delivery_target": {"kind": "user", "id": self.identity().user_id}}
        run = self.call("reminders.create", body, command(body))
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        records = [d for p, d in self.store.data.items() if "/reminders/" in p]
        self.assertEqual(len(records), 2)
        tool = next(d for d in records if d["id"] != record.id)
        api = ReminderRequests(self.memories).get(
            self.identity(), self.tenant, ResourceId(tool["id"])
        )
        self.assertEqual(
            (api.due_at, api.timezone, api.delivery_target, api.schedule_version),
            (record.due_at, record.timezone, record.delivery_target, 1),
        )
        self.call("reminders.create", body, "Do not " + command(body))
        self.assertEqual(sum("/reminders/" in p for p in self.store.data), 2)

    def test_list_then_cancel_and_snooze_retire_old_input_and_use_fixed_completion(self) -> None:
        for snooze in (False, True):
            record = self.reminder(title="OLD_REMINDER")
            run = self.prepare_update(record, snooze=snooze)
            self.execute(run)
            self.assertEqual(
                self.get_run(run).json()["status"], "completed", self.get_run(run).text
            )
            self.assertEqual(len(self.model.requests), 2)
            saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
            self.assertEqual(saved["tool_bindings"], [])
            self.assertEqual(saved["continuation"], [])
            self.assertIn(
                {"reminder_id": record.id, "version": 2}, saved["purge_dependencies"]["reminders"]
            )
            self.assertNotIn("OLD_REMINDER", json.dumps(saved["purge_dependencies"]))
            final = self.store.data[
                f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
            ]
            self.assertEqual(final["text"], RemindersRuntime.UPDATE_ACK)
            current = self.store.data[reminder_path(self.tenant, record.id)]
            self.assertEqual((current["version"], current["schedule_version"]), (2, 2))
            self.assertEqual(current["status"], "scheduled" if snooze else "cancelled")

    def test_lost_tool_response_proof_reconciliation_and_no_reexecution(self) -> None:
        record = self.reminder()
        run = self.prepare_update(record, snooze=True)
        execute = RemindersRuntime.execute_reminders
        recovered = []

        async def lost(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "reminders.snooze":
                recovered.append(await execute(runtime, name, invocation, reconcile=True, **kwargs))
                self.assertEqual(recovered[-1], result)
                raise RuntimeError("synthetic response loss")
            return result

        with patch.object(RemindersRuntime, "execute_reminders", lost):
            self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(sum("/reminder_effects/" in p for p in self.store.data), 1)
        self.assertEqual(
            self.store.data[reminder_path(self.tenant, record.id)]["schedule_version"], 2
        )

    def test_list_rechecks_current_version_audience_and_delivery_authority(self) -> None:
        visible = self.reminder(title="VISIBLE_REMINDER")
        self.reminder(title="PRIVATE_REMINDER", visibility="private", display_policy="never")
        self.call("reminders.list", {"status": "scheduled"}, "List reminders")
        prompt = self.model.requests[-1].model_dump_json()
        self.assertIn("VISIBLE_REMINDER", prompt)
        self.assertNotIn("PRIVATE_REMINDER", prompt)
        execute = RemindersRuntime.execute_reminders

        async def stale(runtime: Any, *args: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, *args, **kwargs)
            self.store.data[reminder_path(self.tenant, visible.id)]["version"] += 1
            return result

        with patch.object(RemindersRuntime, "execute_reminders", stale):
            run = self.call("reminders.list", {}, "List reminders")
        self.assertEqual(len(self.model.requests), 1)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")


class ReminderExportTests(ExportFixture):
    def test_public_projection_manifest_and_current_change_revocation(self) -> None:
        reminders = ReminderRequests(self.memories)
        record = reminders.create(
            self.identity(), TENANT, ReminderCreate.model_validate(REMINDER), "export-reminder-key"
        )
        identity = self.ready()
        text = json.dumps(self.archive(identity))
        self.assertIn("Alex reminder", text)
        self.assertIn("2026-10-27T15:30:00Z", text)
        self.assertNotIn("updated_by", text)
        self.assertNotIn("suppress_reuse", text)
        reminders.update(
            self.identity(),
            TENANT,
            record.id,
            ReminderPatch(status="cancelled"),
            "export-reminder-edit",
            '"1"',
        )
        self.assertNotEqual(self.download(identity).status_code, 200)
