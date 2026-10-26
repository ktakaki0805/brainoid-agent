"""Task update proofs remain bound to current run, version and authorization."""

import copy
import json
from typing import Any
from unittest.mock import patch

from brainoid_api.repositories.tasks import TaskRequests
from brainoid_contracts import ResourceId
from brainoid_memory.tasks import TaskPatch, task_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_worker.tasks import TasksRuntime
from test_tasks import TasksWorkerFixture


class TaskUpdateRegressionTests(TasksWorkerFixture):
    def assert_updated(self, run: ResourceId, task: Any) -> dict[str, Any]:
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["status"], "done")
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["version"], 2)
        return self.store.data[f"tenants/{self.tenant}/runs/{run}"]

    def test_lost_atomic_commit_response_retries_ack_without_model_or_update(self) -> None:
        task = self.task()
        run = self.prepare_update(task)
        thrown: list[bool] = []

        original = self.store.run

        def lost(work: Any) -> Any:
            result = original(work)
            if self.store.data[task_path(self.tenant, task.id)]["version"] == 2 and not thrown:
                thrown.append(True)
                raise PersistenceUnavailable()
            return result

        with patch.object(self.store, "run", lost), self.assertRaises(PersistenceUnavailable):
            self.execute(run)
        self.assertTrue(thrown)
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["version"], 2)
        calls = len(self.model.requests)
        self.clock.advance(120)
        self.execute(run)
        self.assert_updated(run, task)
        self.assertEqual(len(self.model.requests), calls)
        self.assertEqual(sum("/task_effects/" in p for p in self.store.data), 1)

    def test_external_change_to_other_list_item_blocks_update_before_commit(self) -> None:
        task, other = self.task(), self.task(title="OTHER_TASK")
        run = self.prepare_update(task)
        mutate = self.handler.memory.tasks.service.mutate

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            TaskRequests(self.memories).update(
                self.identity(),
                self.tenant,
                other.id,
                TaskPatch(title="Externally edited"),
                "external-other-task",
                '"1"',
            )
            return mutate(*args, **kwargs)

        with patch.object(self.handler.memory.tasks.service, "mutate", concurrent):
            self.execute(run)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["version"], 1)
        self.assertEqual(self.store.data[task_path(self.tenant, other.id)]["version"], 2)

    def post_commit_denied(self, change: str) -> None:
        task = self.task()
        run = self.prepare_update(task)
        execute = TasksRuntime.execute_tasks

        async def changed(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "tasks.update":
                if change == "edit":
                    TaskRequests(self.memories).update(
                        self.identity(),
                        self.tenant,
                        task.id,
                        TaskPatch(title="External v3"),
                        "external-target-task",
                        '"2"',
                    )
                elif change == "expire":
                    self.clock.advance(121)
                else:
                    for p, d in self.store.data.items():
                        if "/memberships/" in p and d.get("user_id") == self.identity().user_id:
                            d["status"] = "removed"
            return result

        with patch.object(TasksRuntime, "execute_tasks", changed):
            self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertNotEqual(saved["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)

    def test_post_commit_external_edit_denies_ack(self) -> None:
        self.post_commit_denied("edit")

    def test_post_commit_revocation_denies_ack(self) -> None:
        self.post_commit_denied("revoke")

    def test_post_commit_expiry_denies_ack(self) -> None:
        self.post_commit_denied("expire")

    def test_terminal_proof_cannot_be_reused_by_another_run(self) -> None:
        task = self.task()
        run = self.prepare_update(task)
        self.execute(run)
        saved = self.assert_updated(run, task)
        second = ResourceId(
            self.turn(self.conversation(), key="other-terminal-run", text="List tasks").json()[
                "run_id"
            ]
        )
        other = self.store.data[f"tenants/{self.tenant}/runs/{second}"]
        other["task_update"] = copy.deepcopy(saved["task_update"])
        other["tool_call_ids"] = list(saved["tool_call_ids"])
        before = len(self.model.requests)
        self.execute(second)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), before)

    def test_task_list_history_rechecks_current_version_in_later_run(self) -> None:
        from brainoid_agent_core.types import ToolCall

        task = self.task()
        self.configure_calls((ToolCall(call_key="list", name="tasks.list", arguments_json="{}"),))
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="List tasks").json()["run_id"])
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        TaskRequests(self.memories).update(
            self.identity(),
            self.tenant,
            task.id,
            TaskPatch(title="Externally changed"),
            "task-history-change",
            '"1"',
        )
        second = ResourceId(
            self.turn(conversation, text="Continue", key="task-history-second").json()["run_id"]
        )
        before = len(self.model.requests)
        self.execute(second)
        self.assertEqual(len(self.model.requests), before)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")

    def test_derived_user_message_cannot_authorize_completion(self) -> None:
        task = self.task()
        run = self.prepare_update(task)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        path = f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['input_message_id']}"
        self.store.data[path]["source_refs"] = [
            {"kind": "note", "resource_id": str(task.id), "version": 1}
        ]
        self.execute(run)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[task_path(self.tenant, task.id)]["status"], "open")

    def test_actual_list_then_delete_task_uses_exact_durable_operation(self) -> None:
        from brainoid_agent_core.types import ToolCall
        from brainoid_memory.deletion_ledger import DeletionLedger
        from fakes.authz_ledger import MemoryObjects

        task = self.task()
        self.configure_calls(
            (
                ToolCall(call_key="list", name="tasks.list", arguments_json="{}"),
                ToolCall(
                    call_key="delete",
                    name="tasks.delete",
                    arguments_json=json.dumps({"task_id": task.id, "expected_version": 1}),
                ),
            )
        )
        self.handler.memory.deletion_ledger = DeletionLedger(MemoryObjects())
        run = ResourceId(
            self.turn(self.conversation(), text=f"Delete task {task.id}").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)
        self.assertTrue(self.store.data[task_path(self.tenant, task.id)]["deleted"])
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        intent = self.handler.memory.deletion_ledger.get(ResourceId(saved["forget_operation_id"]))
        assert intent is not None
        self.assertEqual([(t.kind, t.resource_id) for t in intent.targets], [("task", task.id)])
        self.assertEqual(saved["tool_bindings"], [])

    def test_notes_list_then_delete_also_retires_consumed_text_for_fixed_ack(self) -> None:
        from brainoid_agent_core.types import ToolCall
        from brainoid_memory.deletion_ledger import DeletionLedger
        from brainoid_memory.notes import note_path
        from fakes.authz_ledger import MemoryObjects

        note = self.note()
        self.configure_calls(
            (
                ToolCall(call_key="list", name="notes.list", arguments_json="{}"),
                ToolCall(
                    call_key="delete",
                    name="notes.delete",
                    arguments_json=json.dumps({"note_id": note.id, "expected_version": 1}),
                ),
            )
        )
        self.handler.memory.deletion_ledger = DeletionLedger(MemoryObjects())
        run = ResourceId(
            self.turn(self.conversation(), text=f"Delete note {note.id}").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)
        self.assertTrue(self.store.data[note_path(self.tenant, note.id)]["deleted"])
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertIn({"note_id": note.id, "version": 1}, saved["purge_dependencies"]["notes"])
        final = self.store.data[
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
        ]
        self.assertEqual(final["text"], "ノートの削除を受け付けました。消去処理を待っています。")
