"""APP-02 Firestore: actual canonical races, recovery, default tool and purge."""

import json
from functools import partial
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.types import ToolCall
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.tasks import TaskRequests
from brainoid_contracts import FirebaseUid
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import DeleteTask, operation_path
from brainoid_memory.tasks import TaskCreate, TaskPatch, TaskView, task_path
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_runs.models import TurnCreate
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeDispatcher, PurgeResource
from brainoid_worker.run_turn import RunTurn
from brainoid_worker.tasks import TasksRuntime
from fakes.run_confirmation import MultiStageModel
from google.api_core.exceptions import ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture
from test_turn_run_integration import TurnRunIntegrationFixture

BODY = {
    "title": "Emulator task",
    "visibility": "shared",
    "display_policy": "shared_display",
    "due_at": "2026-10-28T09:30:00+09:00",
    "timezone": "Asia/Tokyo",
}


class TasksIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.tasks = TaskRequests(self.memories)
        self.memories.deletion_ledger = DeletionLedger(LocalAuthzObjects(self.directory))

    def create_task(self) -> TaskView:
        return self.tasks.create(
            self.identity(), self.tenant, TaskCreate.model_validate(BODY), "emulator-task-create"
        )

    def test_same_key_creation_two_authorized_editors_and_exact_transition_replay(self) -> None:
        a, b = self.concurrent(self.create_task, self.create_task)
        self.assertEqual(a, b)
        assert isinstance(a, TaskView)
        admin = self.identity().model_copy(update={"user_id": FirebaseUid("task/admin/opaque")})
        self.db.document(f"tenants/{self.tenant}/memberships/task-admin").set(
            {
                "tenant_id": self.tenant,
                "user_id": admin.user_id,
                "role": "admin",
                "status": "active",
                "authorization_version": 1,
            }
        )

        def update(identity: AuthenticatedIdentity, key: str) -> TaskView:
            return self.tasks.update(
                identity, self.tenant, a.id, TaskPatch(status="done"), key, '"1"'
            )

        results = self.concurrent(
            partial(update, self.identity(), "task-owner-update-01"),
            partial(update, admin, "task-admin-update-01"),
        )
        self.assertEqual(sum(isinstance(r, TaskView) for r in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        record = self.tasks.get(self.identity(), self.tenant, a.id)
        self.assertEqual((record.version, record.status), (2, "done"))
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/task_events").stream())), 1
        )
        updated = self.tasks.update(
            self.identity(),
            self.tenant,
            a.id,
            TaskPatch(status="cancelled", due_at=None),
            "task-cancel-update-01",
            '"2"',
        )
        self.assertEqual(
            self.tasks.update(
                self.identity(),
                self.tenant,
                a.id,
                TaskPatch(status="cancelled", due_at=None),
                "task-cancel-update-01",
                '"2"',
            ),
            updated,
        )
        self.assertIsNone(updated.due_at)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/task_events").stream())), 2
        )

    def test_commit_response_loss_recovers_one_task(self) -> None:
        commit = self.db._firestore_api.commit

        def lost(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(self.create_task, ErrorCode.TEMPORARILY_UNAVAILABLE)
        task = self.create_task()
        docs = list(self.db.collection(f"tenants/{self.tenant}/tasks").stream())
        self.assertEqual([d.id for d in docs], [task.id])

    def test_exact_task_delete_erases_events_and_preserves_notes(self) -> None:
        self.delete_task_as(admin=False)

    def test_admin_task_purge_survives_membership_revocation_after_acceptance(self) -> None:
        self.delete_task_as(admin=True)

    def delete_task_as(self, *, admin: bool) -> None:
        task = self.create_task()
        self.tasks.update(
            self.identity(),
            self.tenant,
            task.id,
            TaskPatch(status="done"),
            "delete-task-done-01",
            '"1"',
        )
        unrelated = self.db.document(f"tenants/{self.tenant}/notes/{task.id}")
        unrelated.set({"id": task.id, "tenant_id": self.tenant, "body": "UNRELATED_NOTE"})
        requester = self.identity()
        if admin:
            requester = requester.model_copy(update={"user_id": FirebaseUid("task/admin/opaque")})
            self.db.document(f"tenants/{self.tenant}/memberships/task-admin").set(
                {
                    "tenant_id": self.tenant,
                    "user_id": requester.user_id,
                    "role": "admin",
                    "status": "active",
                    "authorization_version": 1,
                }
            )
        operation = self.memories.forget(
            requester,
            self.tenant,
            DeleteTask(
                kind="delete_resource", target_type="task", target_id=task.id, expected_version=2
            ),
            "emulator-task-delete",
        )
        if admin:
            self.db.document(f"tenants/{self.tenant}/memberships/task-admin").update(
                {"status": "removed"}
            )
        self.fails(
            lambda: self.tasks.get(self.identity(), self.tenant, task.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        assert self.memories.deletion_ledger is not None
        jobs = PurgeJobs(self.memories.jobs, self.memories.deletion_ledger)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=2,
        )
        queue = LocalQueue()
        dispatcher = PurgeDispatcher(handler, self.store, queue)
        worker = Worker(jobs, LocalCaller(), {KIND: handler})
        dispatcher.recover(self.tenant)
        queue.drain(worker)
        self.assertFalse(self.db.document(task_path(self.tenant, task.id)).get().exists)
        self.assertTrue(unrelated.get().exists)
        self.assertEqual(
            list(self.db.collection(f"tenants/{self.tenant}/task_events").stream()), []
        )
        saved = (
            self.db.document(operation_path(self.tenant, operation.operation_id)).get().to_dict()
        )
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded")
        job = jobs.get(self.tenant, job_id(self.tenant, KIND, operation.operation_id))
        assert job is not None
        self.assertEqual(job.status, "succeeded")


class TasksRunIntegrationTests(TurnRunIntegrationFixture):
    def test_default_list_complete_runtime_and_saved_task_agree(self) -> None:
        memories = Memories(
            self.store,
            self.tenants,
            Cursors(b"t" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        tasks = TaskRequests(memories)
        task = tasks.create(
            self.identity(), self.tenant, TaskCreate.model_validate(BODY), "run-task-create-001"
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["tasks.list", "tasks.update"]}
        )
        model = MultiStageModel(
            (
                ToolCall(call_key="list", name="tasks.list", arguments_json="{}"),
                ToolCall(
                    call_key="done",
                    name="tasks.update",
                    arguments_json=json.dumps(
                        {"task_id": task.id, "expected_version": 1, "changes": {"status": "done"}}
                    ),
                ),
            )
        )
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conversation = self.conversation()
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(text=f'Update task {task.id}: {{"status": "done"}}', output_mode="text"),
            "emulator-task-run-01",
        ).run_id
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(tasks.get(self.identity(), self.tenant, task.id).status, "done")
        saved = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None
        self.assertEqual(saved["tool_bindings"], [])
        self.assertEqual(saved["continuation"], [])
        self.assertIn({"task_id": task.id, "version": 2}, saved["purge_dependencies"]["tasks"])
        final = (
            self.db.document(
                f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
            )
            .get()
            .to_dict()
        )
        assert final is not None
        self.assertEqual(final["text"], TasksRuntime.UPDATE_ACK)
