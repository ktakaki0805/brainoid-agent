"""APP-02 / FR-008: actual HTTP and default tools, state proof and erasure."""

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
from brainoid_api.repositories.tasks import TaskRequests
from brainoid_contracts import ResourceId
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import DeleteTask
from brainoid_memory.tasks import TaskCreate, TaskPatch, task_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_worker.tasks import TasksRuntime
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import OTHER, TENANT, USER
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_exports import ExportFixture
from test_notes import NotesFixture, NotesWorkerFixture
from test_purge_history import PurgeHistoryFixture

TASK = {
    "title": "Alex task",
    "due_at": "2026-10-28T09:30:00+09:00",
    "timezone": "Asia/Tokyo",
    "visibility": "shared",
    "display_policy": "shared_display",
}


def command(body: dict[str, Any]) -> str:
    normalized = TaskCreate.model_validate(body).model_dump(mode="json", exclude_unset=True)
    return "Create task " + json.dumps(normalized, ensure_ascii=False, sort_keys=True)


class TasksHTTPTests(NotesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = f"/v1/tenants/{TENANT}/tasks"
        self.tasks = TaskRequests(self.memories)

    def create(self, key: str = "task-create-key-001", **changes: Any) -> Any:
        return self.client.post(
            self.url, json={**TASK, **changes}, headers={"Idempotency-Key": key}
        )

    def update(
        self, identity: str, version: int = 1, key: str = "task-update-key-001", **changes: Any
    ) -> Any:
        return self.client.patch(
            self.url + "/" + identity,
            json=changes,
            headers={"Idempotency-Key": key, "If-Match": f'"{version}"'},
        )

    def test_http_normalized_state_actor_page_etag_and_transition_replay(self) -> None:
        response = self.create(assignee_user_id=USER)
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertEqual(task["due_at"], "2026-10-28T00:30:00Z")
        self.assertEqual(task["timezone"], "Asia/Tokyo")
        self.assertEqual(task["status"], "open")
        path = task_path(TENANT, ResourceId(task["id"]))
        self.assertEqual(self.store.data[path]["created_by"], USER)
        self.assertNotIn("source_refs", self.store.data[path])
        self.assertEqual(self.create(assignee_user_id=USER).json(), task)
        self.assertEqual(self.create(title="changed").status_code, 409)
        self.assertEqual(self.client.get(self.url + "/" + task["id"]).json(), task)
        page = self.client.get(self.url).json()
        self.assertIsNone(page["next_cursor"])
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        for name, value in (("Task", task), ("TaskPage", page)):
            Draft202012Validator(
                {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
            ).validate(value)
        for version, status in enumerate(("done", "cancelled", "open"), start=1):
            key = "task-transition-" + status
            changed = self.update(task["id"], version, key, status=status)
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["status"], status)
            self.assertEqual(changed.headers["etag"], f'"{version + 1}"')
            self.assertEqual(
                self.update(task["id"], version, key, status=status).json(), changed.json()
            )
            self.assertEqual(sum("/task_events/" in p for p in self.store.data), version)
        noop = self.update(task["id"], 4, "task-noop-open-001", status="open", due_at=None)
        self.assertEqual(noop.status_code, 200, noop.text)
        self.assertNotIn("due_at", noop.json())
        self.assertNotIn("due_at", self.client.get(self.url).json()["items"][0])
        self.assertEqual(sum("/task_events/" in p for p in self.store.data), 3)
        self.assertEqual(self.update(task["id"], status="done").status_code, 412)
        self.assertEqual(self.client.get(self.url, params={"status": "done"}).json()["items"], [])
        self.assertEqual(
            len(self.client.get(self.url, params={"status": "open"}).json()["items"]), 1
        )
        self.assertEqual(self.client.get(self.url, params={"status": "invalid"}).status_code, 422)

    def test_assignee_opaque_exact_current_tenant_membership_and_private_rules(self) -> None:
        shared = self.create().json()["id"]
        private = self.create(
            "task-private-key-001", visibility="private", display_policy="never"
        ).json()["id"]
        self.member()
        opaque = self.uid
        own = self.create("task-assignee-key-01", assignee_user_id=opaque)
        self.assertEqual(own.status_code, 201, own.text)
        self.assertEqual(own.json()["assignee_user_id"], opaque)
        self.assertEqual(self.update(shared, status="done").status_code, 403)
        self.assertEqual(self.update(private, status="done").status_code, 404)
        self.store.data[f"tenants/{TENANT}/memberships/another"]["role"] = "admin"
        self.assertEqual(self.update(shared, status="done").status_code, 200)
        self.assertEqual(self.update(private, status="done").status_code, 404)
        self.assertNotIn(private, self.client.get(self.url).text)
        self.assertEqual(self.client.get(f"/v1/tenants/{OTHER}/tasks/{shared}").status_code, 404)
        self.uid = USER
        self.store.data[f"tenants/{TENANT}/memberships/another"]["status"] = "revoked"
        self.assertEqual(
            self.create("revoked-assignee-001", assignee_user_id=opaque).status_code, 404
        )
        self.store.data[f"tenants/{TENANT}/memberships/another"].update(
            status="active", authorization_operation_id=str(uuid4())
        )
        # A restored active document without its matching committed ledger is not effective membership.
        self.assertEqual(
            self.create("unproved-assignee-01", assignee_user_id=opaque).status_code, 404
        )
        self.store.data[f"tenants/{OTHER}/memberships/other-assignee"] = {
            "tenant_id": OTHER,
            "user_id": "other-only",
            "role": "member",
            "status": "active",
            "authorization_version": 1,
        }
        self.assertEqual(
            self.create("foreign-assignee-001", assignee_user_id="other-only").status_code, 404
        )
        # Membership lookup cannot turn a Firebase UID into a path or accept a prefix.
        self.assertEqual(
            self.create("prefix-assignee-001", assignee_user_id="another/user").status_code, 404
        )

    def test_patch_contract_dates_and_page_cursor(self) -> None:
        task = self.create().json()["id"]
        for body in (
            {},
            {"title": None},
            {"status": None},
            {"status": "finished"},
            {"assignee_user_id": USER},
            {"timezone": "UTC"},
            {"visibility": "private"},
            {"due_at": "2026-10-28T09:30:00"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.update(task, **body).status_code, 422)
        for field, value in (
            ("due_at", None),
            ("timezone", None),
            ("assignee_user_id", None),
            ("timezone", "Mars/Fake"),
        ):
            self.assertEqual(self.create("invalid-create-001", **{field: value}).status_code, 422)
        self.create("task-next-page-001")
        first = self.client.get(self.url, params={"limit": 1}).json()
        second = self.client.get(
            self.url, params={"limit": 1, "cursor": first["next_cursor"]}
        ).json()
        self.assertIsNone(second["next_cursor"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertNotEqual(
            self.client.get(
                self.url, params={"limit": 1, "status": "done", "cursor": first["next_cursor"]}
            ).status_code,
            200,
        )
        self.update(task, status="done")
        self.assertNotEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": first["next_cursor"]}
            ).status_code,
            200,
        )

    def test_unknown_create_commit_recovers_same_key_and_failed_transition_never_succeeds(
        self,
    ) -> None:
        lost: list[bool] = []

        def fail() -> None:
            if any("/tasks/" in p for p in self.store.data) and not lost:
                lost.append(True)
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.store.after_commit = fail
        result = self.create()
        self.assertEqual(result.status_code, 503, result.text)
        self.store.after_commit = lambda: None
        task = self.create().json()["id"]
        self.assertEqual(sum("/tasks/" in p for p in self.store.data), 1)
        with patch.object(self.tasks.service.store, "run", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.tasks.update(
                    self.identity(),
                    TENANT,
                    ResourceId(task),
                    TaskPatch(status="done"),
                    "task-failed-write-01",
                    '"1"',
                )
        self.assertEqual(self.client.get(self.url + "/" + task).json()["status"], "open")
        self.assertFalse(any("/task_events/" in p for p in self.store.data))

    def test_delete_ledger_failure_restore_replay_and_source_kind_separation(self) -> None:
        from brainoid_memory.deletion_ledger import DeletionIntent
        from brainoid_policy.control_ledger import LedgerError
        from pydantic import ValidationError

        task = self.create().json()["id"]
        before = copy.deepcopy(self.store.data)
        body = {
            "kind": "delete_resource",
            "target_type": "task",
            "target_id": task,
            "expected_version": 1,
        }
        headers = {"Idempotency-Key": "task-delete-restore"}
        url = f"/v1/tenants/{TENANT}/operations"
        with patch.object(self.objects, "create", side_effect=LedgerError()):
            self.assertEqual(self.client.post(url, json=body, headers=headers).status_code, 503)
        self.assertEqual(self.update(task, status="done").status_code, 412)
        accepted = self.client.post(url, json=body, headers=headers)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(self.client.post(url, json=body, headers=headers).json(), accepted.json())
        immutable = copy.deepcopy(self.objects.data)
        self.store.data = before
        self.now += timedelta(seconds=1)
        replay = self.client.post(url, json=body, headers=headers)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], accepted.json()["operation_id"])
        self.assertEqual(immutable, self.objects.data)
        self.assertEqual(self.client.get(self.url + "/" + task).status_code, 404)
        assert self.memories.deletion_ledger is not None
        intent = self.memories.deletion_ledger.get(ResourceId(accepted.json()["operation_id"]))
        assert intent is not None
        invalid = {
            **intent.model_dump(mode="json"),
            "kind": "forget_memory",
            "targets": [
                {
                    **intent.targets[0].model_dump(mode="json"),
                    "kind": "fact",
                    "resource_id": str(uuid4()),
                },
                intent.targets[0].model_dump(mode="json"),
            ],
        }
        with self.assertRaises(ValidationError):
            DeletionIntent.model_validate(invalid)

    def test_pi_requires_tasks_session_and_persists_verified_actor(self) -> None:
        from brainoid_api.routes.agents import reader_identity
        from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions

        device = self.device()
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + device.access_token,
            "Idempotency-Key": "pi-task-create-0001",
        }
        self.assertEqual(self.client.post(self.url, json=TASK, headers=headers).status_code, 403)
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        )
        note_session = sessions.start(
            self.identity(),
            TENANT,
            device.device_id,
            PrivateSessionCreate(allowed_scopes=["notes"], acknowledge_shared_space=True),
            "pi-notes-session-01",
            "pi-notes-request-01",
        )
        headers["X-Private-Session"] = note_session.id
        self.assertNotEqual(self.client.post(self.url, json=TASK, headers=headers).status_code, 201)
        self.now += timedelta(seconds=61)
        session = sessions.start(
            self.identity(),
            TENANT,
            device.device_id,
            PrivateSessionCreate(allowed_scopes=["tasks"], acknowledge_shared_space=True),
            "pi-tasks-session-01",
            "pi-tasks-request-01",
        )
        headers["X-Private-Session"] = session.id
        response = self.client.post(self.url, json=TASK, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        record = self.store.data[task_path(TENANT, ResourceId(response.json()["id"]))]
        self.assertEqual(
            (record["created_by"], record["created_via_device_id"]), (USER, device.device_id)
        )


class TasksWorkerFixture(NotesWorkerFixture):
    def task(self, **changes: Any) -> Any:
        return TaskRequests(self.memories).create(
            self.identity(),
            self.tenant,
            TaskCreate.model_validate({**TASK, **changes}),
            "task-key-" + str(uuid4()),
        )

    def prepare_update(self, task: Any, *, status: str = "done") -> ResourceId:
        changes = {"status": status}
        self.configure_calls(
            (
                ToolCall(call_key="list", name="tasks.list", arguments_json="{}"),
                ToolCall(
                    call_key="update",
                    name="tasks.update",
                    arguments_json=json.dumps(
                        {"task_id": task.id, "expected_version": 1, "changes": changes}
                    ),
                ),
            )
        )
        return ResourceId(
            self.turn(
                self.conversation(),
                text=f"Update task {task.id}: " + json.dumps(changes, sort_keys=True),
            ).json()["run_id"]
        )


class TasksWorkerTests(TasksWorkerFixture):
    def test_actual_create_matches_api_and_model_success_alone_changes_nothing(self) -> None:
        api = self.task()
        run = self.call("tasks.create", TASK, command(TASK))
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        records = [d for p, d in self.store.data.items() if "/tasks/" in p]
        self.assertEqual(len(records), 2)
        tool = next(d for d in records if d["id"] != api.id)
        actual = self.store.data[task_path(self.tenant, api.id)]
        for k in (
            "title",
            "due_at",
            "timezone",
            "status",
            "created_by",
            "updated_by",
            "owner_user_id",
        ):
            self.assertEqual(tool[k], actual[k])
        from brainoid_worker.entry import Worker
        from brainoid_worker.local import LocalCaller
        from brainoid_worker.run_turn import RunTurn
        from fakes.turn_model import Model

        self.configure_calls(())
        self.model = Model(text="タスクを完了しました。")
        self.handler = RunTurn(self.jobs, self.model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        plain = ResourceId(
            self.turn(self.conversation(), text="Say the task is done").json()["run_id"]
        )
        self.execute(plain)
        self.assertEqual(self.get_run(plain).json()["status"], "completed")
        saved = self.store.data[f"tenants/{self.tenant}/runs/{plain}"]
        final = self.store.data[
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
        ]
        self.assertEqual(final["text"], "タスクを完了しました。")
        self.assertEqual(self.store.data[task_path(self.tenant, api.id)]["status"], "open")

    def test_direct_instruction_mismatch_negation_and_kind_confusion_do_not_write(self) -> None:
        for text in (
            "Do not " + command(TASK),
            "Create task {}",
            "Save shared note Alex task: done",
        ):
            self.call("tasks.create", TASK, text)
            self.assertFalse(any("/tasks/" in p for p in self.store.data))
        task = self.task()
        run = self.call(
            "notes.update",
            {"note_id": task.id, "expected_version": 1, "changes": {"title": "bad"}},
            f'Update note {task.id}: {{"title": "bad"}}',
        )
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["version"], 1)

    def test_list_done_and_cancel_retire_inputs_atomically_and_do_not_reinvoke_model(self) -> None:
        for status in ("done", "cancelled"):
            task = self.task(title="OLD_TASK_" + status)
            run = self.prepare_update(task, status=status)
            self.execute(run)
            self.assertEqual(self.get_run(run).json()["status"], "completed")
            self.assertEqual(len(self.model.requests), 2)
            record = self.store.data[task_path(self.tenant, task.id)]
            self.assertEqual((record["status"], record["version"]), (status, 2))
            saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
            self.assertEqual(saved["tool_bindings"], [])
            self.assertEqual(saved["continuation"], [])
            self.assertIn({"task_id": task.id, "version": 2}, saved["purge_dependencies"]["tasks"])
            self.assertNotIn("OLD_TASK", json.dumps(saved["purge_dependencies"]))
            final = self.store.data[
                f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
            ]
            self.assertEqual(final["text"], TasksRuntime.UPDATE_ACK)

    def test_lost_response_reconcile_reads_one_completion_proof(self) -> None:
        task = self.task()
        run = self.prepare_update(task)
        execute = TasksRuntime.execute_tasks
        recovered: list[Any] = []

        async def lost(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "tasks.update":
                recovered.append(await execute(runtime, name, invocation, reconcile=True, **kwargs))
                self.assertEqual(recovered[-1], result)
                raise RuntimeError("synthetic response loss")
            return result

        with patch.object(TasksRuntime, "execute_tasks", lost):
            self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(recovered), 1)
        self.assertEqual(sum("/task_effects/" in p for p in self.store.data), 1)
        self.assertEqual(sum("/task_events/" in p for p in self.store.data), 1)

    def test_list_filters_status_current_result_and_private_audience(self) -> None:
        visible = self.task(title="VISIBLE_TASK")
        self.task(title="PRIVATE_TASK", visibility="private", display_policy="never")
        self.call("tasks.list", {"status": "open"}, "List tasks")
        prompt = self.model.requests[-1].model_dump_json()
        self.assertIn("VISIBLE_TASK", prompt)
        self.assertNotIn("PRIVATE_TASK", prompt)
        execute = TasksRuntime.execute_tasks

        async def stale(runtime: Any, *args: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, *args, **kwargs)
            self.store.data[task_path(self.tenant, visible.id)]["version"] += 1
            return result

        with patch.object(TasksRuntime, "execute_tasks", stale):
            run = self.call("tasks.list", {}, "List tasks")
        self.assertEqual(len(self.model.requests), 1)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")


class TaskHistoryTests(PurgeHistoryFixture):
    def test_actual_task_result_and_effect_history_delete_with_unrelated_preserved(self) -> None:
        tasks = TaskRequests(self.memories)
        task = tasks.create(
            self.identity(), self.tenant, TaskCreate.model_validate(TASK), "history-task-create"
        )
        other = tasks.create(
            self.identity(),
            self.tenant,
            TaskCreate.model_validate({**TASK, "title": "UNRELATED_TASK"}),
            "other-task-create-01",
        )
        self.configure_calls(
            (
                ToolCall(call_key="list", name="tasks.list", arguments_json="{}"),
                ToolCall(
                    call_key="update",
                    name="tasks.update",
                    arguments_json=json.dumps(
                        {"task_id": task.id, "expected_version": 1, "changes": {"status": "done"}}
                    ),
                ),
            )
        )
        conversation = self.conversation()
        first = ResourceId(
            self.turn(conversation, text=f'Update task {task.id}: {{"status": "done"}}').json()[
                "run_id"
            ]
        )
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        saved = copy.deepcopy(self.store.data[f"tenants/{self.tenant}/runs/{first}"])
        ledger = DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            DeleteTask(
                kind="delete_resource", target_type="task", target_id=task.id, expected_version=2
            ),
            "history-task-delete",
        )
        self.bind_purge(ledger, operation.operation_id)
        self.finish_purge()
        self.assertNotIn(task_path(self.tenant, task.id), self.store.data)
        self.assertIn(task_path(self.tenant, other.id), self.store.data)
        self.assertFalse(
            any("/task_effects/" in p or "/task_events/" in p for p in self.store.data)
        )
        self.assertNotIn(
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}",
            self.store.data,
        )


class TaskExportTests(ExportFixture):
    def test_public_task_projection_current_authority_and_delete_revocation(self) -> None:
        task = TaskRequests(self.memories).create(
            self.identity(), TENANT, TaskCreate.model_validate(TASK), "export-task-create"
        )
        identity = self.ready()
        archive = self.archive(identity)
        text = json.dumps(archive)
        self.assertIn("Alex task", text)
        self.assertIn("2026-10-28T00:30:00Z", text)
        self.assertNotIn("updated_by", text)
        operation = self.memories.forget(
            self.identity(),
            TENANT,
            DeleteTask(
                kind="delete_resource", target_type="task", target_id=task.id, expected_version=1
            ),
            "export-task-delete",
        )
        self.assertNotEqual(self.download(identity).status_code, 200)
        assert self.memories.deletion_ledger is not None
        intent = self.memories.deletion_ledger.get(operation.operation_id)
        assert intent is not None
        self.exports.invalidate(intent, key=f"{operation.operation_id}:export:v1")
        self.erased(identity)
