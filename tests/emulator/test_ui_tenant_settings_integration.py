"""UI-11 controllers and mobile browser use actual Auth, Firestore and API routes."""

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
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, ROOT, DemoEmulator, load_fixture


class UiTenantSettingsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT}, name="ui-settings-" + str(uuid4())
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
        db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(db.close)
        source = next(
            row
            for row in load_fixture()["documents"]
            if "/memberships/" in row["path"] and row["fields"]["user_id"] == "demo-a-member"
        )
        for uid in (".", "..", "opaque / uid", "%2E", "u?/#+"):
            db.document(source["path"].rsplit("/", 1)[0] + "/" + str(uuid4())).set(
                {**source["fields"], "user_id": uid}
            )
        self.directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="ba-ui-11-writer-api-")
        )

    def run_client(self, script: str, timeout: int = 120) -> None:
        environment = {
            **os.environ,
            "AUTHZ_LEDGER_DIRECTORY": self.directory,
            "CLIENT_ORIGINS": "http://127.0.0.1:5197",
        }
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 8000))
            listener.listen()
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
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
            print(f"UI11 owned API listener {origin}", flush=True)
            try:
                for _ in range(200):
                    if server.started:
                        break
                    self.assertTrue(thread.is_alive())
                    time.sleep(0.05)
                self.assertTrue(server.started)
                result = subprocess.run(
                    ["node", script],
                    cwd=ROOT,
                    env={**environment, "UI_API_URL": origin},
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                # Never print transport bodies, Firebase tokens, invitation codes or SDK errors.
                for line in result.stdout.splitlines():
                    if line.startswith("Tenant settings"):
                        print(line, flush=True)
                if result.returncode:
                    for line in result.stderr.splitlines():
                        if "tests/ui/tenant_settings" in line or line.startswith("Tenant settings"):
                            print(line, flush=True)
                self.assertEqual(result.returncode, 0, "UI11 client failed; stage output above")
                self.assertIn("passed", result.stdout)
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "Owned API listener remained live")
                print(f"UI11 owned API listener closed {origin}", flush=True)

    def test_controller_contract_and_exact_receipts(self) -> None:
        self.run_client("tests/ui/tenant_settings_emulator.mjs")

    @unittest.skipUnless(os.environ.get("UI_CHROMIUM"), "Explicit local Chromium required")
    def test_mobile_browser_invitations_and_usage(self) -> None:
        self.run_client("tests/ui/tenant_settings_browser.mjs", timeout=180)
