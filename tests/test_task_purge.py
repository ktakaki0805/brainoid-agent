"""Accepted task deletion binds original ownership, independently of its requester."""

import copy
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, cast

from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.export_manifest import KIND as EXPORT_KIND
from brainoid_memory.exports import Exports
from brainoid_memory.forget import operation_path
from brainoid_memory.notes import NoteCreate, Notes, note_path
from brainoid_memory.tasks import TaskView, task_path
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import GraphStore
from fakes.purge import FakePurgePages
from test_memory_purge import PurgeFixture
from test_purge_history import PurgeHistoryFixture
from test_tasks import TASK, TasksWorkerFixture


class TaskAdminPurgeTests(TasksWorkerFixture, PurgeHistoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.creator = self.uid
        self.admin_path = f"tenants/{self.tenant}/memberships/task-admin"
        self.memories.deletion_ledger = DeletionLedger(MemoryObjects())
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        self.exports = Exports(
            self.jobs,
            LocalExportObjects(directory, b"t" * 32, now=self.clock.now),
            FakePurgePages(cast(GraphStore, self.store)),
            CanonicalSources(SavedMessages()),
            now=self.clock.now,
        )
        self.export_worker = Worker(
            self.jobs, LocalCaller(), {EXPORT_KIND: ExportData(self.exports)}
        )
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            conversations=self.conversations,
            memories=self.memories,
            exports=ExportRequests(self.exports, self.tenants),
        )
        self.operations_url = f"/v1/tenants/{self.tenant}/operations"

    def http_task(self, **changes: Any) -> TaskView:
        response = self.client.post(
            f"/v1/tenants/{self.tenant}/tasks",
            json={**TASK, **changes},
            headers={"Idempotency-Key": "create-purge-task-" + str(len(self.store.data))},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return TaskView.model_validate(response.json())

    def switch_member(self, role: str) -> None:
        self.uid = "task/admin/opaque"
        self.store.data[self.admin_path] = {
            "tenant_id": self.tenant,
            "user_id": self.uid,
            "role": role,
            "status": "active",
            "authorization_version": 1,
        }

    def delete(self, task: TaskView) -> Any:
        return self.client.post(
            self.operations_url,
            json={
                "kind": "delete_resource",
                "target_type": "task",
                "target_id": task.id,
                "expected_version": task.version,
            },
            headers={"Idempotency-Key": "delete-task-" + task.id},
        )

    def accept(self, task: TaskView) -> ResourceId:
        response = self.delete(task)
        self.assertEqual(response.status_code, 202, response.text)
        identity = ResourceId(response.json()["operation_id"])
        assert self.memories.deletion_ledger is not None
        self.bind_purge(self.memories.deletion_ledger, identity)
        return identity

    def generate(self, identity: ResourceId) -> None:
        delivery = self.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, EXPORT_KIND, identity)
        )
        assert delivery is not None
        self.export_worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def test_admin_purge_after_revocation_erases_real_copies_and_unblocks_tenant_export(
        self,
    ) -> None:
        task = self.http_task(title="ERASE_ADMIN_TASK_SENTINEL")
        other = self.http_task(title="PRESERVE_OTHER_TASK_SENTINEL")
        notes = Notes(self.store, now=self.clock.now)
        notes.mutate(
            lambda tx: self.memories._open(tx, self.identity(), self.tenant, None),
            NoteCreate(
                title="Preserved",
                body="SAME_ID_NOTE_SENTINEL",
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
            ),
            "same-id-note-create",
            effect=task.id,
        )
        same_note = copy.deepcopy(self.store.data[note_path(self.tenant, task.id)])
        run = self.prepare_update(task)
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        saved = copy.deepcopy(self.store.data[f"tenants/{self.tenant}/runs/{run}"])
        result_paths = [
            f"tenants/{self.tenant}/tool_executions/{i}" for i in saved["tool_call_ids"]
        ]
        self.assertTrue(result_paths)
        self.assertTrue(all(self.store.data[p].get("result") is not None for p in result_paths))
        self.assertTrue(any("/task_effects/" in p for p in self.store.data))
        self.assertTrue(any("/task_events/" in p for p in self.store.data))
        task = task.model_copy(update={"version": 2})
        self.switch_member("admin")
        self.accept(task)
        # Admission established dedicated exact-target authority. Its completion
        # must not depend on the administrator retaining ordinary membership.
        self.store.data[self.admin_path]["status"] = "removed"
        self.uid = self.creator
        response = self.client.post(
            self.operations_url,
            json={"kind": "export", "scope": "visible_tenant_data"},
            headers={"Idempotency-Key": "export-during-admin-purge"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        export_id = ResourceId(response.json()["operation_id"])
        self.generate(export_id)
        export_path = operation_path(self.tenant, export_id)
        self.assertNotEqual(self.store.data[export_path]["operation"]["status"], "succeeded")
        self.finish_purge()
        self.assertNotIn(task_path(self.tenant, task.id), self.store.data)
        self.assertEqual(self.store.data[note_path(self.tenant, task.id)], same_note)
        self.assertIn(task_path(self.tenant, other.id), self.store.data)
        self.assertNotIn("ERASE_ADMIN_TASK_SENTINEL", repr(self.store.data))
        self.assertFalse(
            any("/task_events/" in p or "/task_effects/" in p for p in self.store.data)
        )
        self.assertTrue(all(p not in self.store.data for p in result_paths))
        self.assertNotIn(
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}",
            self.store.data,
        )
        self.assertEqual(self.store.data[f"tenants/{self.tenant}/runs/{run}"]["status"], "failed")
        self.clock.advance(120)
        self.generate(export_id)
        self.assertEqual(self.store.data[export_path]["operation"]["status"], "succeeded")
        downloaded = self.client.get(self.operations_url + f"/{export_id}/download")
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            data = json.dumps(json.loads(archive.read("data.json")))
        self.assertNotIn("ERASE_ADMIN_TASK_SENTINEL", data)
        self.assertIn("PRESERVE_OTHER_TASK_SENTINEL", data)
        self.assertIn("SAME_ID_NOTE_SENTINEL", data)

    def test_owner_can_purge_shared_task_but_member_and_other_private_are_denied(self) -> None:
        shared = self.http_task()
        private = self.http_task(visibility="private", display_policy="never")
        self.switch_member("member")
        self.assertEqual(self.delete(shared).status_code, 403)
        self.assertEqual(self.delete(private).status_code, 404)
        self.store.data[self.admin_path]["role"] = "admin"
        self.assertEqual(self.delete(private).status_code, 404)
        self.store.data[self.admin_path]["role"] = "owner"
        self.assertEqual(self.delete(private).status_code, 404)
        self.accept(shared)
        self.finish_purge()
        self.assertNotIn(task_path(self.tenant, shared.id), self.store.data)
        self.assertIn(task_path(self.tenant, private.id), self.store.data)

    def test_accepted_task_with_changed_creator_or_owner_is_not_erased(self) -> None:
        for field in ("created_by", "owner_user_id"):
            with self.subTest(field=field):
                self.setUp()
                task = self.http_task()
                other = self.http_task(title="UNRELATED_TASK")
                self.switch_member("admin")
                self.accept(task)
                path = task_path(self.tenant, task.id)
                self.store.data[path][field] = "different/owner/opaque"
                with self.assertRaisesRegex(ValueError, "Original resource ownership changed"):
                    self.purge.run(self.purge_execution)
                self.assertIn(path, self.store.data)
                self.assertIn(task_path(self.tenant, other.id), self.store.data)
                self.assertNotEqual(
                    self.store.data[self.op_path]["operation"]["status"], "succeeded"
                )


class ForgetSourceOwnershipTests(PurgeFixture):
    def test_accepted_fact_forget_still_rejects_changed_note_and_message_authors(self) -> None:
        from brainoid_memory.forget import target_path

        for message in (False, True):
            with self.subTest(message=message):
                self.setUp()
                self.accept(message=message, delete=True)
                intent = self.ledger.get(self.operation.operation_id)
                assert intent is not None
                source = intent.targets[1]
                path = target_path(intent.tenant_id, source)
                self.store.data[path]["created_by"] = "different/source/author"
                with self.assertRaisesRegex(ValueError, "Source author changed"):
                    self.handler.run(self.acquire())
                self.assertIn(path, self.store.data)
                self.assertNotEqual(
                    self.store.data[self.op_path]["operation"]["status"], "succeeded"
                )
