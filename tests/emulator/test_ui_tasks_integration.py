"""Tasks UI controller against real HTTP and canonical Firestore."""

import os
import socket
import subprocess
import threading
import time

import uvicorn
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.routes.agents import reader_identity
from test_memories_integration import MemoryIntegrationFixture

from scripts.demo_emulator import ROOT


class UiTasksIntegrationTests(MemoryIntegrationFixture):
    def test_ui_controller_persistence_conflict_and_status(self) -> None:
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
                    ["node", "tests/ui/tasks_emulator.mjs"],
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
                self.assertIn("readonly display passed", result.stdout)
                tasks = list(self.db.collection(f"tenants/{self.tenant}/tasks").stream())
                self.assertEqual(len(tasks), 1)
                task = tasks[0].to_dict() or {}
                self.assertEqual((task["status"], task["version"]), ("cancelled", 5))
                self.assertIsNone(task.get("due_at"))
                events = list(self.db.collection(f"tenants/{self.tenant}/task_events").stream())
                self.assertEqual(len(events), 4)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned UI tasks API listener did not stop")
