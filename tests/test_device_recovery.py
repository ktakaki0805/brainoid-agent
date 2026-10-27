"""Local reset authority, live resource fencing, durable restart and diagnostics."""

import asyncio
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from brainoid_device.app import DeviceApplication
from brainoid_device.ledger import CommandDelivery, StorageUnavailable
from brainoid_device.local_api import COOKIE
from brainoid_device.storage import CredentialsUnavailable
from starlette.testclient import TestClient
from test_device_service import DeviceFixture


class RecoveryTests(DeviceFixture):
    def application(self) -> DeviceApplication:
        app = DeviceApplication(self.settings())
        self.addCleanup(app.close)
        return app

    def test_confirmation_authority_and_old_browser_session(self) -> None:
        app = self.application()
        app.credentials.write("cloud", b"synthetic-refresh")
        with TestClient(
            app.app, base_url=self.settings().origin, client=("127.0.0.1", 42)
        ) as client:
            csrf = self.bootstrap(client)
            headers = {"origin": self.settings().origin, "x-csrf-token": csrf}
            for extra, expected in [
                ({"origin": "http://evil.invalid"}, 403),
                ({"host": "evil.invalid"}, 403),
                ({"x-csrf-token": "wrong"}, 403),
                ({"cookie": "brainoid_local=wrong"}, 401),
            ]:
                result = client.post("/local/reset/prepare", headers={**headers, **extra})
                self.assertEqual(result.status_code, expected)
                self.assertEqual(app.credentials.read("cloud"), b"synthetic-refresh")
            token = client.post("/local/reset/prepare", headers=headers).json()["reset_token"]
            self.assertEqual(client.post("/local/reset", headers=headers).status_code, 403)
            confirmed = {
                **headers,
                "x-brainoid-confirm": "local-administrator",
                "x-brainoid-reset-token": token,
            }
            # Security-header ambiguity and payloads stay rejected by DEV-01.
            self.assertEqual(
                client.post(
                    "/local/reset",
                    headers=list(confirmed.items())
                    + [("x-brainoid-confirm", "local-administrator")],
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post("/local/reset", headers=confirmed, content="{}").status_code, 413
            )
            old_cookie = client.cookies.get(COOKIE)
            response = client.post("/local/reset", headers=confirmed)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json(), {"local_reset": "complete", "remote_revocation": "unconfirmed"}
            )
            self.assertEqual(client.get("/local/state").status_code, 401)
            self.assertEqual(client.post("/local/reset", headers=confirmed).status_code, 401)
            self.assertEqual(list(self.private.iterdir()), [])
            self.assertEqual(app.storage.snapshot().status, "unregistered")
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.bootstrap(client)
            self.assertNotEqual(client.cookies.get(COOKIE), old_cookie)
            diagnostic = client.get("/local/diagnostics").json()
            self.assertEqual(diagnostic["cloud"], "unavailable")
            self.assertEqual(diagnostic["audio"], "unavailable")
            self.assertEqual(diagnostic["remote_revocation"], "unconfirmed")
            self.assertNotIn("synthetic", str(diagnostic))

    def test_token_is_bound_to_session_one_use_and_expires(self) -> None:
        app = self.application()
        with TestClient(
            app.app, base_url=self.settings().origin, client=("127.0.0.1", 42)
        ) as client:
            headers = {"origin": self.settings().origin, "x-csrf-token": self.bootstrap(client)}
            token = client.post("/local/reset/prepare", headers=headers).json()["reset_token"]
            client.cookies.clear()
            headers["x-csrf-token"] = self.bootstrap(client)
            headers.update(
                {"x-brainoid-confirm": "local-administrator", "x-brainoid-reset-token": token}
            )
            self.assertEqual(client.post("/local/reset", headers=headers).status_code, 403)
            with patch("brainoid_device.reset_command.time.monotonic", return_value=1):
                token = client.post("/local/reset/prepare", headers=headers).json()["reset_token"]
            headers["x-brainoid-reset-token"] = token
            self.assertEqual(client.post("/local/reset", headers=headers).status_code, 403)
            self.assertEqual(app.reset.status, "idle")

    def test_actual_process_interruption_reopens_unpaired_without_startup_work(self) -> None:
        code = """
import asyncio, os
from brainoid_device.app import DeviceApplication
from brainoid_device.config import load_settings
async def main():
    app = DeviceApplication(load_settings())
    app.runtime.running = True
    app.credentials.write('cloud', b'synthetic-secret')
    app.storage.register('00000000-0000-4000-8000-000000000001', '00000000-0000-4000-8000-000000000002')
    async def interrupted_drain():
        assert app.credentials.reset_pending
        assert not app.storage.snapshot().permits_operations
        os._exit(73)
    app.runtime.close = interrupted_drain
    await app.reset.execute()
asyncio.run(main())
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            env={**os.environ, **self.env},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertTrue((self.private / ".reset-pending").exists())

        async def forbidden_startup(runtime: object) -> None:
            self.fail("reset restart must not invoke previous connection startup")

        app = DeviceApplication(self.settings(), startup=forbidden_startup)
        self.addCleanup(app.close)
        with TestClient(
            app.app, base_url=self.settings().origin, client=("127.0.0.1", 42)
        ) as client:
            self.bootstrap(client)
            self.assertEqual(client.get("/local/state").json()["storage_status"], "unregistered")
            self.assertFalse(app.runtime.permits_work(app.runtime.generation))
        self.assertEqual(list(self.private.iterdir()), [])

    def test_diagnostics_failure_is_explicit_no_remote_probe(self) -> None:
        from brainoid_device.diagnostics import local_diagnostics

        with (
            patch("brainoid_device.diagnostics.subprocess.run", side_effect=OSError),
            patch("brainoid_device.diagnostics.shutil.disk_usage", side_effect=OSError),
        ):
            status = local_diagnostics(self.state)
        self.assertEqual(status["clock"], "unconfirmed")
        self.assertIsNone(status["free_bytes"])
        self.assertEqual(status["remote_revocation"], "unconfirmed")


class ResetRuntimeTests(DeviceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_active_private_audio_command_and_late_callbacks_are_fenced(self) -> None:
        app = DeviceApplication(self.settings())
        self.addCleanup(app.close)
        runtime = app.runtime
        runtime.running = True
        generation = runtime.generation
        private = bytearray(b"synthetic-private-audio")
        display = ["synthetic-private-display"]
        cached_token = [b"synthetic-refresh"]
        runtime.register_private_cleanup(private.clear)
        runtime.register_private_cleanup(display.clear)
        runtime.register_private_cleanup(cached_token.clear)
        app.credentials.write("cloud", b"synthetic-refresh")
        app.credentials.write("ha", b"synthetic-ha")
        started = asyncio.Event()
        cancellation = asyncio.Event()
        stale_allowed: list[bool] = []

        async def connection() -> None:
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stale_allowed.append(runtime.permits_work(generation))
                with self.assertRaises(CredentialsUnavailable):
                    app.credentials.read("cloud")
                # A cancellation finally block is allowed to touch buffers, but
                # the final private cleanup must also remove this late write.
                private.extend(b"late-private")
                cancellation.set()

        runtime.start_task(connection())
        child = await runtime.start_child([sys.executable, "-c", "import time; time.sleep(120)"])
        await started.wait()
        registration = app.storage.register(
            "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
        )
        app.storage.set_generation(registration.epoch, 1)
        delivery = CommandDelivery(
            registration.tenant_id,
            registration.device_id,
            registration.epoch,
            1,
            "00000000-0000-4000-8000-000000000003",
            1,
            "a" * 64,
            10000,
        )
        app.storage.receive_command(delivery, expected_cursor=0, now_ms=1, clock_trusted=True)
        self.assertTrue(await app.reset.execute())
        self.assertTrue(cancellation.is_set())
        self.assertEqual(stale_allowed, [False])
        self.assertIsNotNone(child.returncode)
        self.assertEqual((private, display, cached_token), (bytearray(), [], []))
        with self.assertRaises(RuntimeError):
            await runtime.start_child([sys.executable, "-c", "raise SystemExit(0)"])
        self.assertEqual(app.storage.snapshot().status, "unregistered")
        with self.assertRaises((StorageUnavailable, ValueError)):
            app.storage.receive_command(delivery, expected_cursor=0, now_ms=2, clock_trusted=True)
        app.close()
        reopened = DeviceApplication(self.settings())
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.storage.snapshot().status, "unregistered")
        self.assertEqual(list(self.private.iterdir()), [])

    async def test_request_cancellation_does_not_interrupt_owned_reset(self) -> None:
        app = DeviceApplication(self.settings())
        self.addCleanup(app.close)
        app.runtime.running = True
        started = asyncio.Event()
        finish = asyncio.Event()
        original = app.runtime.close

        async def slow_close() -> None:
            started.set()
            await finish.wait()
            await original()

        with patch.object(app.runtime, "close", slow_close):
            request = asyncio.create_task(app.reset.execute())
            await started.wait()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertTrue(app.credentials.reset_pending)
            self.assertFalse(app.storage.snapshot().permits_operations)
            self.assertFalse(await app.reset.execute())
            finish.set()
            await app.reset.finish()
        self.assertEqual(app.reset.status, "complete")

    async def test_failed_drain_keeps_durable_fence_and_reopening_resumes(self) -> None:
        app = DeviceApplication(self.settings())
        app.credentials.write("cloud", b"synthetic-refresh")

        async def failed() -> None:
            raise RuntimeError("synthetic drain failure")

        with patch.object(app.runtime, "close", failed):
            self.assertFalse(await app.reset.execute())
        self.assertTrue(app.credentials.reset_pending)
        self.assertFalse(app.storage.snapshot().permits_operations)
        with self.assertRaises(CredentialsUnavailable):
            app.credentials.read("cloud")
        app.close()
        resumed = DeviceApplication(self.settings())
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.storage.snapshot().status, "unregistered")
        self.assertEqual(resumed.reset.status, "complete")
        resumed.runtime.running = True
        self.assertFalse(resumed.runtime.permits_work(resumed.runtime.generation))
        self.assertEqual(list(self.private.iterdir()), [])
