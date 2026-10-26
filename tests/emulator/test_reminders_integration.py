"""APP-03 Firestore races, ambiguous commits, generation fences and default snooze."""

import json
from functools import partial
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.types import ToolCall
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_contracts import FirebaseUid
from brainoid_memory.reminders import (
    Reminder,
    ReminderCreate,
    ReminderPatch,
    ReminderSnooze,
    ReminderView,
    ScheduleReference,
    current_schedule,
    reminder_path,
)
from brainoid_runs.models import TurnCreate
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller
from brainoid_worker.reminders import RemindersRuntime
from brainoid_worker.run_turn import RunTurn
from fakes.run_confirmation import MultiStageModel
from google.api_core.exceptions import ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture
from test_turn_run_integration import TurnRunIntegrationFixture

BODY = {
    "title": "Emulator reminder",
    "visibility": "shared",
    "display_policy": "shared_display",
    "due_at": "2026-10-28T00:30:00+09:00",
    "timezone": "Asia/Tokyo",
}
SNOOZE = {"due_at": "2026-11-01T06:30:00Z", "timezone": "America/New_York", "expected_version": 1}


class RemindersIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.reminders = ReminderRequests(self.memories)

    def create_reminder(self) -> ReminderView:
        return self.reminders.create(
            self.identity(),
            self.tenant,
            ReminderCreate.model_validate(
                {**BODY, "delivery_target": {"kind": "user", "id": self.identity().user_id}}
            ),
            "emulator-reminder-key",
        )

    def test_same_key_create_and_two_current_editors_one_stale(self) -> None:
        first, second = self.concurrent(self.create_reminder, self.create_reminder)
        self.assertEqual(first, second)
        assert isinstance(first, ReminderView)
        admin = self.identity().model_copy(update={"user_id": FirebaseUid("reminder/admin/opaque")})
        self.db.document(f"tenants/{self.tenant}/memberships/reminder-admin").set(
            {
                "tenant_id": self.tenant,
                "user_id": admin.user_id,
                "role": "admin",
                "status": "active",
                "authorization_version": 1,
            }
        )

        def update(identity: AuthenticatedIdentity, key: str) -> ReminderView:
            return self.reminders.snooze(
                identity, self.tenant, first.id, ReminderSnooze.model_validate(SNOOZE), key
            )

        results = self.concurrent(
            partial(update, self.identity(), "reminder-owner-race"),
            partial(update, admin, "reminder-admin-race"),
        )
        self.assertEqual(sum(isinstance(r, ReminderView) for r in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        current = self.reminders.get(self.identity(), self.tenant, first.id)
        self.assertEqual((current.version, current.schedule_version), (2, 2))
        winner_key = (
            "reminder-owner-race" if isinstance(results[0], ReminderView) else "reminder-admin-race"
        )
        winner = self.identity() if isinstance(results[0], ReminderView) else admin
        self.assertEqual(update(winner, winner_key), current)

    def test_commit_response_loss_recovers_one_create_and_snooze(self) -> None:
        commit = self.db._firestore_api.commit

        def lost(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(self.create_reminder, ErrorCode.TEMPORARILY_UNAVAILABLE)
        record = self.create_reminder()
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/reminders").stream())), 1
        )

        def snooze() -> ReminderView:
            return self.reminders.snooze(
                self.identity(),
                self.tenant,
                record.id,
                ReminderSnooze.model_validate(SNOOZE),
                "emulator-snooze-loss",
            )

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(snooze, ErrorCode.TEMPORARILY_UNAVAILABLE)
        current = snooze()
        self.assertEqual((current.version, current.schedule_version), (2, 2))

    def test_t038_delayed_old_job_and_ack_never_modify_new_schedule(self) -> None:
        record = self.create_reminder()
        path = reminder_path(self.tenant, record.id)
        self.db.document(path).update({"status": "dispatched"})
        old = ScheduleReference(tenant_id=self.tenant, reminder_id=record.id, schedule_version=1)
        updated = self.reminders.snooze(
            self.identity(),
            self.tenant,
            record.id,
            ReminderSnooze.model_validate(SNOOZE),
            "emulator-t038-snooze",
        )
        for event in ("delivery", "ack", "completion"):

            def delayed(tx: Any) -> bool:
                canonical = Reminder.model_validate(tx.get(path))
                if not current_schedule(canonical, old):
                    return False
                tx.set(path, {**canonical.model_dump(mode="json"), "status": "acknowledged"})
                return True

            self.assertFalse(self.store.run(delayed), event)
        self.assertEqual(self.reminders.get(self.identity(), self.tenant, record.id), updated)
        cancelled = self.reminders.update(
            self.identity(),
            self.tenant,
            record.id,
            ReminderPatch(status="cancelled"),
            "emulator-t038-cancel",
            '"2"',
        )
        self.assertEqual(cancelled.schedule_version, 3)


class RemindersDefaultToolIntegrationTests(TurnRunIntegrationFixture):
    def test_list_then_snooze_on_actual_default_tool_and_http_repository(self) -> None:
        memories = Memories(
            self.store,
            self.tenants,
            Cursors(b"r" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        reminders = ReminderRequests(memories)
        record = reminders.create(
            self.identity(),
            self.tenant,
            ReminderCreate.model_validate(
                {**BODY, "delivery_target": {"kind": "user", "id": self.identity().user_id}}
            ),
            "emulator-run-reminder",
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["reminders.list", "reminders.snooze"]}
        )
        model = MultiStageModel(
            (
                ToolCall(call_key="list", name="reminders.list", arguments_json="{}"),
                ToolCall(
                    call_key="snooze",
                    name="reminders.snooze",
                    arguments_json=json.dumps({"reminder_id": record.id, **SNOOZE}),
                ),
            )
        )
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conversation = self.conversation()
        changes = {k: v for k, v in SNOOZE.items() if k != "expected_version"}
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(
                text=f"Snooze reminder {record.id}: " + json.dumps(changes, sort_keys=True),
                output_mode="text",
            ),
            "emulator-reminder-run",
        ).run_id
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(reminders.get(self.identity(), self.tenant, record.id).schedule_version, 2)
        saved = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None
        self.assertEqual(saved["tool_bindings"], [])
        self.assertEqual(saved["continuation"], [])
        self.assertIn(
            {"reminder_id": record.id, "version": 2}, saved["purge_dependencies"]["reminders"]
        )
        final = (
            self.db.document(
                f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
            )
            .get()
            .to_dict()
        )
        assert final is not None
        self.assertEqual(final["text"], RemindersRuntime.UPDATE_ACK)
