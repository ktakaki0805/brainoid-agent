"""Notes UI controller against real HTTP, canonical Firestore and independent deletion ledger."""

import os
import socket
import subprocess
import threading
import time

import uvicorn
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_memory.deletion_ledger import DeletionLedger
from test_memories_integration import MemoryIntegrationFixture

from scripts.demo_emulator import ROOT


class UiNotesIntegrationTests(MemoryIntegrationFixture):
    def test_ui_controller_persistence_conflict_and_delete(self) -> None:
        self.memories.deletion_ledger = DeletionLedger(LocalAuthzObjects(self.directory))
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
                    ["node", "tests/ui/notes_emulator.mjs"],
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
                self.assertIn("both derived policies passed", result.stdout)
                notes = list(self.db.collection(f"tenants/{self.tenant}/notes").stream())
                self.assertEqual(len(notes), 4)  # Two fixture notes plus two UI-created notes.
                created = [
                    item.to_dict() or {}
                    for item in notes
                    if (item.to_dict() or {}).get("title", "").startswith("UI ")
                ]
                self.assertEqual(len(created), 2)
                self.assertTrue(all(item["deleted"] and item["suppress_reuse"] for item in created))
                operations = list(self.db.collection(f"tenants/{self.tenant}/operations").stream())
                self.assertEqual(len(operations), 2)
                self.assertTrue(
                    all(
                        (item.to_dict() or {})["operation"]["status"] == "pending"
                        for item in operations
                    )
                )
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned UI notes API listener did not stop")
