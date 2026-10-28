"""Production mobile + Auth/Firestore + actual device service over loopback HTTP.

Explicit Chromium fixture only. Never installs host services or uses physical Pi hardware.
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
import httpx
import uvicorn
from brainoid_api.app import create_app
from brainoid_api.settings import load_settings
from brainoid_device.app import DeviceApplication
from brainoid_device.config import CloudSettings, Settings
from firebase_admin import auth
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, ROOT, DemoEmulator


@unittest.skipUnless(
    os.environ.get("UI_CHROMIUM") and os.environ.get("UI_ARTIFACTS"),
    "Explicit local Chromium and evidence directory required",
)
class UiDevicesIntegrationTests(unittest.TestCase):
    def test_production_mobile_pairs_equivalent_device_service_and_revokes(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT}, name="ui-devices-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)
        auth.update_user(
            "demo-a-owner",
            email="ui-owner@example.test",
            password="synthetic-password-only",
            app=firebase,
        )
        db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(db.close)
        with tempfile.TemporaryDirectory(prefix="ba-ui-09-writer-equivalent-") as directory:
            root = Path(directory)
            for name in ("credentials", "state", "assets", "ledger"):
                (root / name).mkdir(mode=0o700)
            (root / "assets/index.html").write_text(
                "<!doctype html><title>Equivalent device service</title>"
            )
            api_env = {
                **os.environ,
                "CLIENT_ORIGINS": "http://127.0.0.1:5195",
                "AUTHZ_LEDGER_DIRECTORY": str(root / "ledger"),
            }
            api = create_app(settings_loader=lambda: load_settings(api_env))
            with socket.socket() as api_socket, socket.socket() as local_socket:
                api_socket.bind(("127.0.0.1", 8000))
                api_socket.listen()
                local_socket.bind(("127.0.0.1", 0))
                local_socket.listen()
                settings = Settings(
                    "127.0.0.1",
                    local_socket.getsockname()[1],
                    root / "assets",
                    root / "credentials",
                    root / "state",
                    CloudSettings("http://127.0.0.1:8000", local_test=True),
                )
                application = DeviceApplication(settings)
                servers = [
                    uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
                    for app in (api, application.app)
                ]
                threads = [
                    threading.Thread(target=server.run, kwargs={"sockets": [listener]})
                    for server, listener in zip(servers, (api_socket, local_socket), strict=True)
                ]
                stop_observer = threading.Event()
                marker = root / "registered.public"
                observations: list[str] = []

                def observe() -> None:
                    while not stop_observer.wait(0.05):
                        cloud = application.cloud
                        if cloud and cloud.journal.credentials and not marker.exists():
                            # Publish only completion, never a credential or protected journal.
                            observations.append("device_received_credentials")
                            marker.write_text("registered")

                observer = threading.Thread(target=observe)
                for thread in threads:
                    thread.start()
                observer.start()
                try:
                    for server, thread in zip(servers, threads, strict=True):
                        for _ in range(200):
                            if server.started:
                                break
                            self.assertTrue(thread.is_alive(), "Owned HTTP service failed")
                            time.sleep(0.05)
                        self.assertTrue(server.started)
                    with httpx.Client(base_url=settings.origin, trust_env=False) as local:
                        boot = local.post(
                            "/local/session",
                            headers={"Origin": settings.origin, "X-Brainoid-Local": "1"},
                        )
                        self.assertEqual(boot.status_code, 200)
                        headers = {
                            "Origin": settings.origin,
                            "X-CSRF-Token": boot.json()["csrf_token"],
                        }
                        started = local.post(
                            "/local/cloud/pairing",
                            headers=headers,
                            json={"device_name": "Equivalent Pi"},
                        )
                        self.assertEqual(started.status_code, 200)
                        public = started.json()
                        self.assertEqual(set(public), {"user_code", "expires_at"})
                        result = subprocess.run(
                            ["node", "tests/ui/devices_emulator.mjs"],
                            cwd=ROOT,
                            env={
                                **os.environ,
                                "UI_PAIRING_PUBLIC": json.dumps(public),
                                "UI_REGISTRATION_MARKER": str(marker),
                            },
                            capture_output=True,
                            text=True,
                            timeout=150,
                        )
                        # No SDK/HTTP bodies or credentials are copied to test diagnostics.
                        self.assertEqual(
                            result.returncode,
                            0,
                            "Device browser failed; safe stage: "
                            + str(
                                [
                                    line
                                    for line in result.stdout.splitlines()
                                    if line.startswith("Device UI stage:")
                                ]
                            ),
                        )
                        self.assertIn(
                            "Production devices browser and equivalent service passed",
                            result.stdout,
                        )
                        self.assertEqual(observations, ["device_received_credentials"])
                        rows = [row.to_dict() for row in db.collection_group("devices").stream()]
                        paired = [
                            row
                            for row in rows
                            if row and row.get("name") == "Equivalent service from mobile"
                        ]
                        self.assertEqual(len(paired), 1)
                        self.assertEqual(paired[0]["status"], "revoked")
                        # Force an authenticated operation after remote revoke to observe refusal.
                        denied = local.post(
                            "/local/cloud/operations",
                            headers=headers,
                            json={"operation": "list_conversations"},
                        )
                        self.assertIn(denied.status_code, (401, 403, 404, 409))
                        self.assertIsNotNone(application.cloud)
                        assert application.cloud is not None
                        self.assertIsNone(application.cloud.journal.credentials)
                        self.assertEqual((root / "credentials").stat().st_mode & 0o777, 0o700)
                        for path in (root / "credentials").iterdir():
                            if path.is_file():
                                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        print(
                            "Equivalent device observed: actual DeviceApplication/Runtime/CloudClient/CredentialStore/SQLite; loopback HTTP; public-only pairing; device exchange; Firestore revoked; old device access refused. Physical Pi not tested."
                        )
                finally:
                    stop_observer.set()
                    observer.join(timeout=3)
                    self.assertFalse(observer.is_alive())
                    for server in reversed(servers):
                        server.should_exit = True
                    for thread in reversed(threads):
                        thread.join(timeout=15)
                        self.assertFalse(thread.is_alive(), "Owned HTTP listener did not stop")
                    self.assertFalse(application.shutdown_failed)
