"""UI SDK against real SSE/HTTP and official Firestore, with a synthetic reader identity."""

import os
import socket
import subprocess
import threading
import time

import uvicorn
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.agents import Agents
from brainoid_api.routes.agents import reader_identity
from brainoid_runs.state import run_path
from test_turn_run_integration import TurnRunIntegrationFixture

from scripts.demo_emulator import ROOT


class UiChatIntegrationTests(TurnRunIntegrationFixture):
    def test_sdk_http_replay_final_message_and_cancel(self) -> None:
        conversation = self.conversation()
        run = self.accept(conversation).run_id
        self.execute(run)
        expired_conversation = self.conversation()
        expired = self.accept(expired_conversation).run_id
        self.execute(expired)
        self.db.document(run_path(self.tenant, expired)).update({"events_version": 0})
        app = create_app()
        agents = Agents(
            self.store,
            self.tenants,
            Cursors(b"a" * 32, now=lambda: self.now),
            ledger=self.ledger,
            credentials=self.repo,
            now=lambda: self.now,
        )
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, conversations=self.conversations, agents=agents
        )
        app.dependency_overrides[reader_identity] = self.identity
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
                    ["node", "tests/ui/chat_emulator.mjs"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "UI_API_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
                        "UI_TENANT": str(self.tenant),
                        "UI_CONVERSATION": str(conversation),
                        "UI_RUN": str(run),
                        "UI_EXPIRED_RUN": str(expired),
                    },
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, "Synthetic UI chat HTTP client failed")
                self.assertIn("controller canonical DTO passed", result.stdout)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned UI chat API listener did not stop")
