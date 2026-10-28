"""Reminders and notification UI controllers against real HTTP and canonical Firestore."""

import os
import socket
import subprocess
import threading
import time
from datetime import timedelta

import uvicorn
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.routes.agents import reader_identity
from brainoid_jobs import Jobs
from brainoid_memory.reminders import ReminderCreate
from brainoid_worker.notifications import DueReminders
from test_memories_integration import MemoryIntegrationFixture

from scripts.demo_emulator import ROOT


class UiRemindersIntegrationTests(MemoryIntegrationFixture):
    def test_current_notification_ack_snooze_and_registration(self) -> None:
        self.now -= timedelta(minutes=10)
        original = ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate.model_validate(
                {
                    "title": "UI notification seed",
                    "due_at": self.now + timedelta(seconds=1),
                    "timezone": "Asia/Tokyo",
                    "delivery_target": {"kind": "user", "id": self.identity().user_id},
                    "visibility": "private",
                    "display_policy": "never",
                }
            ),
            "ui-reminder-seed",
            None,
        )
        self.now += timedelta(minutes=10, seconds=2)
        jobs = Jobs(self.store, self.ledger, now=lambda: self.now, allow_untracked_bootstrap=True)
        scanned = DueReminders(jobs, self.store).scan(self.tenant)
        self.assertEqual(scanned.created, 1)
        app = create_app()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, memories=self.memories
        )
        app.dependency_overrides[reader_identity] = self.identity
        app.dependency_overrides[authenticated_identity] = self.identity
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            server = uvicorn.Server(
                uvicorn.Config(app, access_log=False, log_level="error", ws="none")
            )
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                for _ in range(200):
                    if server.started:
                        break
                    self.assertTrue(thread.is_alive())
                    time.sleep(0.05)
                self.assertTrue(server.started)
                result = subprocess.run(
                    ["node", "tests/ui/reminders_emulator.mjs"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "UI_API_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
                        "UI_TENANT": str(self.tenant),
                    },
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "notification old generation and global installation passed", result.stdout
                )
                reminder = (
                    self.db.document(f"tenants/{self.tenant}/reminders/{original.id}")
                    .get()
                    .to_dict()
                    or {}
                )
                self.assertEqual(
                    (reminder["status"], reminder["schedule_version"]), ("scheduled", 2)
                )
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned UI reminders API listener did not stop")
