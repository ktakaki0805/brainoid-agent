"""Public browser entry uses official memory-only Firebase Auth and the real deletion API."""

import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
import uvicorn
from brainoid_api.app import create_app
from brainoid_api.settings import load_settings
from firebase_admin import auth

from scripts.demo_emulator import PROJECT, ROOT, DemoEmulator


@unittest.skipUnless(
    os.environ.get("UI_CHROMIUM") and os.environ.get("UI_ARTIFACTS"),
    "Explicit local Chromium and artifact directory required",
)
class WebAccountDeleteIntegrationTests(unittest.TestCase):
    def test_public_browser_authentication_deletion_and_recovery(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT}, name="web-deletion-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        for uid, email in (
            ("demo-a-member", "ui-delete@example.test"),
            ("demo-b-member", "ui-other@example.test"),
            ("demo-a-owner", "ui-owner@example.test"),
        ):
            auth.update_user(uid, email=email, password="synthetic-password-only", app=firebase)
        with tempfile.TemporaryDirectory(prefix="ba-ui-13-writer-ledger-") as directory:
            environment = {
                **os.environ,
                "AUTHZ_LEDGER_DIRECTORY": directory,
                "CLIENT_ORIGINS": "http://127.0.0.1:5200",
            }
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 8000))
                listener.listen()
                server = uvicorn.Server(
                    uvicorn.Config(
                        create_app(settings_loader=lambda: load_settings(environment)),
                        access_log=False,
                        log_level="error",
                        ws="none",
                    )
                )
                thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
                thread.start()
                print("Web account deletion owned API listener 127.0.0.1:8000", flush=True)
                try:
                    for _ in range(200):
                        if server.started:
                            break
                        self.assertTrue(thread.is_alive())
                        time.sleep(0.05)
                    self.assertTrue(server.started)
                    result = subprocess.run(
                        ["node", "tests/ui/web_account_delete_browser.mjs"],
                        cwd=ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=240,
                    )
                    # Preserve safe stage diagnostics, never SDK payloads or credential errors.
                    for line in result.stdout.splitlines():
                        if line.startswith("Web account deletion"):
                            print(line, flush=True)
                    self.assertEqual(result.returncode, 0, "Public browser failed; stages above")
                    self.assertIn("real SDK/API browser flows passed", result.stdout)
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive(), "Owned API listener remained live")
            with socket.socket() as check:
                self.assertNotEqual(check.connect_ex(("127.0.0.1", 8000)), 0)
            print("Web account deletion owned API listener closed", flush=True)
