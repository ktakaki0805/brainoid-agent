"""Reminder update proofs remain bound to current run, version and authorization."""

import copy
from typing import Any
from unittest.mock import patch

from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_contracts import ResourceId
from brainoid_memory.reminders import ReminderPatch, reminder_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_worker.reminders import RemindersRuntime
from test_reminders import RemindersWorkerFixture


class ReminderUpdateRegressionTests(RemindersWorkerFixture):
    def assert_updated(self, run: ResourceId, reminder: Any) -> dict[str, Any]:
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(
            self.store.data[reminder_path(self.tenant, reminder.id)]["status"], "cancelled"
        )
        self.assertEqual(self.store.data[reminder_path(self.tenant, reminder.id)]["version"], 2)
        return self.store.data[f"tenants/{self.tenant}/runs/{run}"]

    def test_lost_atomic_commit_response_retries_ack_without_model_or_update(self) -> None:
        reminder = self.reminder()
        run = self.prepare_update(reminder)
        thrown: list[bool] = []

        original = self.store.run

        def lost(work: Any) -> Any:
            result = original(work)
            if (
                self.store.data[reminder_path(self.tenant, reminder.id)]["version"] == 2
                and not thrown
            ):
                thrown.append(True)
                raise PersistenceUnavailable()
            return result

        with patch.object(self.store, "run", lost), self.assertRaises(PersistenceUnavailable):
            self.execute(run)
        self.assertTrue(thrown)
        self.assertEqual(self.store.data[reminder_path(self.tenant, reminder.id)]["version"], 2)
        calls = len(self.model.requests)
        self.clock.advance(120)
        self.execute(run)
        self.assert_updated(run, reminder)
        self.assertEqual(len(self.model.requests), calls)
        self.assertEqual(sum("/reminder_effects/" in p for p in self.store.data), 1)

    def test_external_change_to_other_list_item_blocks_update_before_commit(self) -> None:
        reminder, other = self.reminder(), self.reminder(title="OTHER_REMINDER")
        run = self.prepare_update(reminder)
        mutate = self.handler.memory.reminders.service.mutate

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            ReminderRequests(self.memories).update(
                self.identity(),
                self.tenant,
                other.id,
                ReminderPatch(title="Externally edited"),
                "external-other-reminder",
                '"1"',
            )
            return mutate(*args, **kwargs)

        with patch.object(self.handler.memory.reminders.service, "mutate", concurrent):
            self.execute(run)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[reminder_path(self.tenant, reminder.id)]["version"], 1)
        self.assertEqual(self.store.data[reminder_path(self.tenant, other.id)]["version"], 2)

    def post_commit_denied(self, change: str) -> None:
        reminder = self.reminder()
        run = self.prepare_update(reminder)
        execute = RemindersRuntime.execute_reminders

        async def changed(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "reminders.update":
                if change == "edit":
                    ReminderRequests(self.memories).update(
                        self.identity(),
                        self.tenant,
                        reminder.id,
                        ReminderPatch(title="External v3"),
                        "external-target-reminder",
                        '"2"',
                    )
                elif change == "expire":
                    self.clock.advance(121)
                else:
                    for p, d in self.store.data.items():
                        if "/memberships/" in p and d.get("user_id") == self.identity().user_id:
                            d["status"] = "removed"
            return result

        with patch.object(RemindersRuntime, "execute_reminders", changed):
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
        reminder = self.reminder()
        run = self.prepare_update(reminder)
        self.execute(run)
        saved = self.assert_updated(run, reminder)
        second = ResourceId(
            self.turn(self.conversation(), key="other-terminal-run", text="List reminders").json()[
                "run_id"
            ]
        )
        other = self.store.data[f"tenants/{self.tenant}/runs/{second}"]
        other["reminder_update"] = copy.deepcopy(saved["reminder_update"])
        other["tool_call_ids"] = list(saved["tool_call_ids"])
        before = len(self.model.requests)
        self.execute(second)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), before)

    def test_reminder_list_history_rechecks_current_version_in_later_run(self) -> None:
        from brainoid_agent_core.types import ToolCall

        reminder = self.reminder()
        self.configure_calls(
            (ToolCall(call_key="list", name="reminders.list", arguments_json="{}"),)
        )
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="List reminders").json()["run_id"])
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        ReminderRequests(self.memories).update(
            self.identity(),
            self.tenant,
            reminder.id,
            ReminderPatch(title="Externally changed"),
            "reminder-history-change",
            '"1"',
        )
        second = ResourceId(
            self.turn(conversation, text="Continue", key="reminder-history-second").json()["run_id"]
        )
        before = len(self.model.requests)
        self.execute(second)
        self.assertEqual(len(self.model.requests), before)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")

    def test_derived_user_message_cannot_authorize_completion(self) -> None:
        reminder = self.reminder()
        run = self.prepare_update(reminder)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        path = f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['input_message_id']}"
        self.store.data[path]["source_refs"] = [
            {"kind": "note", "resource_id": str(reminder.id), "version": 1}
        ]
        self.execute(run)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(
            self.store.data[reminder_path(self.tenant, reminder.id)]["status"], "scheduled"
        )

    def test_started_unknown_tool_replays_result_without_reexecuting_mutation(self) -> None:
        from brainoid_agent_core.types import ToolResult
        from brainoid_tools import Executor

        record = self.reminder()
        run = self.prepare_update(record, snooze=True)
        dispatches: list[str] = []
        original_backend = RemindersRuntime.execute_reminders
        original_execute = Executor.execute

        async def unavailable(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            if name == "reminders.snooze":
                dispatches.append(name)
                raise RuntimeError("synthetic unavailable before mutation")
            return await original_backend(runtime, name, invocation, **kwargs)

        async def retry(executor: Any, request: Any, **kwargs: Any) -> Any:
            result = await original_execute(executor, request, **kwargs)
            if request.call.name == "reminders.snooze":
                self.assertIsInstance(result, ToolResult)
                assert isinstance(result, ToolResult)
                self.assertEqual(result.status, "outcome_unknown")
                self.assertEqual(await original_execute(executor, request, **kwargs), result)
            return result

        with (
            patch.object(RemindersRuntime, "execute_reminders", unavailable),
            patch.object(Executor, "execute", retry),
        ):
            self.execute(run)
        self.assertEqual(dispatches, ["reminders.snooze"])
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[reminder_path(self.tenant, record.id)]["version"], 1)
        self.assertFalse(any("/reminder_effects/" in p for p in self.store.data))

    def test_assistant_success_text_alone_never_changes_canonical_reminder(self) -> None:
        from brainoid_worker.entry import Worker
        from brainoid_worker.local import LocalCaller
        from brainoid_worker.run_turn import RunTurn
        from fakes.turn_model import Model

        record = self.reminder()
        self.configure_calls(())
        self.model = Model(text="リマインダーを取り消しました。")
        self.handler = RunTurn(self.jobs, self.model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        run = ResourceId(
            self.turn(self.conversation(), text="Say the reminder was cancelled").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        current = self.store.data[reminder_path(self.tenant, record.id)]
        self.assertEqual(
            (current["status"], current["version"], current["schedule_version"]),
            ("scheduled", 1, 1),
        )
