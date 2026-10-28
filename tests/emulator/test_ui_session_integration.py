"""Real browser Firebase adapter + generated client through the HTTP API and emulators."""

import os
import socket
import subprocess
import threading
import time
import unittest
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
import uvicorn
from brainoid_api.app import create_app
from firebase_admin import auth

from scripts.demo_emulator import PROJECT, ROOT, DemoEmulator


class UiSessionIntegrationTests(unittest.TestCase):
    def test_real_client_authentication_and_tenant_flow(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT}, name="ui-test-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        auth.update_user(
            "demo-a-owner",
            email="ui-owner@example.test",
            password="synthetic-password-only",
            app=firebase,
        )
        auth.create_user(
            uid="ui-no-profile",
            email="ui-new@example.test",
            password="synthetic-password-only",
            app=firebase,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 8000 if os.environ.get("UI_CHROMIUM") else 0))
            listener.listen()
            port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(create_app(), access_log=False, log_level="error", ws="none")
            )
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
            thread.start()
            try:
                for _ in range(200):
                    if server.started:
                        break
                    self.assertTrue(thread.is_alive(), "API startup failed")
                    time.sleep(0.05)
                self.assertTrue(server.started)
                result = subprocess.run(
                    ["node", "tests/ui/session_emulator.mjs"],
                    cwd=ROOT,
                    env={**os.environ, "UI_API_URL": f"http://127.0.0.1:{port}"},
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                # Never echo SDK exceptions or response bodies, which could contain credentials.
                self.assertEqual(
                    result.returncode, 0, "UI emulator client failed; inspect synthetic local run"
                )
                self.assertIn("no-profile onboarding, signout passed", result.stdout)
                if os.environ.get("UI_CHROMIUM"):
                    browser = subprocess.run(
                        ["node", "tests/ui/session_browser_emulator.mjs"],
                        cwd=ROOT,
                        env={**os.environ, "UI_API_URL": f"http://127.0.0.1:{port}"},
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    self.assertEqual(
                        browser.returncode,
                        0,
                        "Browser emulator client failed: "
                        + str(
                            [
                                line
                                for line in browser.stdout.splitlines()
                                if line.startswith(
                                    (
                                        "UI browser failed at:",
                                        "UI network failed:",
                                        "UI response:",
                                    )
                                )
                            ]
                        ),
                    )
                    self.assertIn("API/CORS UI flow passed", browser.stdout)

            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned API listener did not stop")
