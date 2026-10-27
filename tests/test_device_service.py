"""BA-DEV-01: local browser boundary, credential isolation and real Linux lifetime."""

import asyncio
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brainoid_device.app import DeviceApplication
from brainoid_device.assets import MAX_FILE, load_assets
from brainoid_device.config import ConfigurationError, Settings, load_settings
from brainoid_device.local_api import COOKIE, MAX_SESSIONS, SESSION_SECONDS
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from starlette.testclient import TestClient

SECRET = "fixture-secret-not-for-logs"
ROOT = Path(__file__).resolve().parents[1]


class DeviceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="brainoid-device-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.static = self.root / "display"
        self.private = self.root / "private"
        self.state = self.root / "state"
        self.static.mkdir(mode=0o755)
        self.private.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        (self.static / "index.html").write_text("<!doctype html><title>Device fixture</title>")
        (self.static / "app.js").write_text('document.title = "synthetic UI";')
        self.env = {
            "DEVICE_HOST": "127.0.0.1",
            "DEVICE_PORT": "8765",
            "DEVICE_STATIC_DIR": str(self.static),
            "DEVICE_CREDENTIALS_DIR": str(self.private),
            "DEVICE_STATE_DIR": str(self.state),
        }

    def settings(self, **overrides: str) -> Settings:
        return load_settings({**self.env, **overrides})

    def client(self, **overrides: str) -> TestClient:
        settings = self.settings(**overrides)
        application = DeviceApplication(settings)
        self.addCleanup(application.close)
        return TestClient(application.app, base_url=settings.origin, client=(settings.host, 12345))

    def bootstrap(self, client: TestClient) -> str:
        response = client.post(
            "/local/session",
            headers={
                "origin": str(client.base_url).rstrip("/"),
                "x-brainoid-local": "1",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertNotIn("Domain=", response.headers["set-cookie"])
        return str(response.json()["csrf_token"])


class DeviceSettingsTests(DeviceFixture):
    def test_required_settings_and_root_refusal(self) -> None:
        for name in self.env:
            with self.subTest(name=name), self.assertRaisesRegex(ConfigurationError, name):
                load_settings({key: value for key, value in self.env.items() if key != name})
        with (
            patch("os.geteuid", return_value=0),
            self.assertRaisesRegex(ConfigurationError, "non-root"),
        ):
            load_settings(self.env)

    def test_only_explicit_numeric_loopback_unprivileged_port(self) -> None:
        for host in (
            "0.0.0.0",
            "::",
            "localhost",
            "127.0.0.2",
            "[::1]",
            "::ffff:127.0.0.1",
            "192.0.2.1",
            SECRET,
        ):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ConfigurationError, "DEVICE_HOST") as caught,
            ):
                self.settings(DEVICE_HOST=host)
            self.assertNotIn(SECRET, str(caught.exception))
        for port in ("0", "80", "65536", "-1", "8765x", " 8765", "１２３４", SECRET):
            with self.subTest(port=port), self.assertRaisesRegex(ConfigurationError, "DEVICE_PORT"):
                self.settings(DEVICE_PORT=port)
        self.assertEqual(self.settings(DEVICE_HOST="::1").origin, "http://[::1]:8765")

    def test_storage_roots_cannot_overlap_or_resolve_through_symlinks(self) -> None:
        for override in (
            {"DEVICE_STATIC_DIR": str(self.root)},
            {"DEVICE_STATIC_DIR": str(self.private)},
            {"DEVICE_CREDENTIALS_DIR": str(self.root)},
            {"DEVICE_STATE_DIR": str(self.private)},
            {"DEVICE_STATE_DIR": str(self.static)},
            {"DEVICE_STATE_DIR": str(self.root)},
            {"DEVICE_STATIC_DIR": "relative"},
            {"DEVICE_STATIC_DIR": str(self.root / "missing")},
        ):
            with self.subTest(override=override), self.assertRaises(ConfigurationError):
                self.settings(**override)
        alias = self.root / "alias"
        alias.symlink_to(self.static, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            self.settings(DEVICE_STATIC_DIR=str(alias))

    def test_private_owner_permissions_type_and_hardlinks(self) -> None:
        for mode in (0o755, 0o750, 0o770):
            self.private.chmod(mode)
            with self.assertRaises(ConfigurationError):
                CredentialStore(self.private)
        self.private.chmod(0o700)
        path = self.private / "device"
        path.write_text(SECRET)
        for mode in (0o644, 0o640, 0o700):
            path.chmod(mode)
            with self.assertRaises(ConfigurationError):
                CredentialStore(self.private)
        path.chmod(0o600)
        with (
            patch("os.geteuid", return_value=os.geteuid() + 1),
            self.assertRaises(ConfigurationError),
        ):
            CredentialStore(self.private)
        link = self.private / "link"
        link.hardlink_to(path)
        with self.assertRaises(ConfigurationError):
            CredentialStore(self.private)
        link.unlink()
        link.symlink_to(path)
        with self.assertRaises(ConfigurationError):
            CredentialStore(self.private)
        link.unlink()
        link.mkdir()
        with self.assertRaises(ConfigurationError):
            CredentialStore(self.private)

    def test_private_store_atomic_write_and_revalidation(self) -> None:
        store = CredentialStore(self.private)
        self.addCleanup(store.close)
        store.write("device_refresh", SECRET.encode())
        path = self.private / "device_refresh"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(store.read("device_refresh"), SECRET.encode())
        store.write("device_refresh", b"rotated synthetic secret")
        self.assertEqual(store.read("device_refresh"), b"rotated synthetic secret")
        self.assertEqual([p.name for p in self.private.iterdir()], ["device_refresh"])
        path.chmod(0o644)
        with self.assertRaises(ConfigurationError):
            store.read("device_refresh")
        for name in ("../outside", "/outside", "file.json", "", ".hidden"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                store.write(name, b"synthetic")
        with self.assertRaises(ValueError):
            store.write("large", b"x" * 65537)
        path.unlink()
        path.symlink_to(self.static / "index.html")
        with self.assertRaises(OSError):
            store.read("device_refresh")

    def test_assets_reject_hidden_unsupported_link_and_oversize(self) -> None:
        for name in (".env", "credentials.json", "debug.map"):
            path = self.static / name
            path.write_text(SECRET)
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                load_assets(self.static)
            path.unlink()
        path = self.static / "escape.html"
        path.symlink_to(self.private, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            load_assets(self.static)
        path.unlink()
        path.hardlink_to(self.static / "index.html")
        with self.assertRaises(ConfigurationError):
            load_assets(self.static)
        path.unlink()
        with path.open("wb") as target:
            target.truncate(MAX_FILE + 1)
        with self.assertRaises(ConfigurationError):
            load_assets(self.static)
        path.unlink()
        (self.static / "index.html").unlink()
        with self.assertRaises(ConfigurationError):
            load_assets(self.static)

    def test_ancestor_swap_and_untrusted_writable_ancestors_are_refused(self) -> None:
        parent = self.root / "tree"
        parent.mkdir()
        assets = parent / "display"
        assets.mkdir()
        (assets / "index.html").write_text("synthetic original")
        settings = self.settings(DEVICE_STATIC_DIR=str(assets))
        replacement = self.root / "replacement"
        replacement.mkdir()
        (replacement / "display").mkdir()
        (replacement / "display" / "index.html").write_text(SECRET)
        parent.rename(self.root / "saved")
        parent.symlink_to(replacement, target_is_directory=True)
        with self.assertRaises(ConfigurationError):
            DeviceApplication(settings)
        parent.unlink()
        (self.root / "saved").rename(parent)
        parent.chmod(0o777)
        with self.assertRaises(ConfigurationError):
            DeviceApplication(settings)
        parent.chmod(0o755)
        store = CredentialStore(self.private)
        self.addCleanup(store.close)
        self.private.rename(self.root / "original-private")
        self.private.symlink_to(replacement, target_is_directory=True)
        store.write("refresh", SECRET.encode())
        self.assertEqual((self.root / "original-private" / "refresh").read_text(), SECRET)
        self.assertFalse((replacement / "refresh").exists())


class DeviceBoundaryTests(DeviceFixture):
    def test_valid_ui_bootstrap_state_logout_and_no_credentials(self) -> None:
        store = CredentialStore(self.private)
        store.write("refresh_token", SECRET.encode())
        store.close()
        with self.client() as client:
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(client.get("/local/state").status_code, 401)
            ui = client.get("/")
            self.assertEqual(ui.status_code, 200)
            self.assertIn("no-store", ui.headers["cache-control"])
            self.assertIn("frame-ancestors 'none'", ui.headers["content-security-policy"])
            csrf = self.bootstrap(client)
            before = client.get("/local/state").json()
            for _ in range(3):
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/app.js").status_code, 200)
                self.assertEqual(client.get("/local/state").json(), before)
            self.assertEqual(
                set(before),
                {"status", "instance_id", "started_at", "software_version", "storage_status"},
            )
            self.assertNotIn(SECRET, json.dumps(before))
            self.assertNotIn(str(self.private), json.dumps(before))
            self.assertEqual(self.bootstrap(client), csrf)
            response = client.delete(
                "/local/session", headers={"origin": self.settings().origin, "x-csrf-token": csrf}
            )
            self.assertEqual(response.status_code, 204)
            self.assertEqual(client.get("/local/state").status_code, 401)
            self.assertEqual(client.get("/healthz").status_code, 200)

    def test_exact_host_and_origin_including_duplicates_port_and_missing(self) -> None:
        with self.client() as client:
            for host in (
                "evil.test:8765",
                "127.0.0.1",
                "127.0.0.1:8766",
                "localhost:8765",
                "127.0.0.1:8765.evil",
                "127.0.0.1:8765,evil",
                "",
                "127.0.0.1:08765",
            ):
                with self.subTest(host=host):
                    self.assertEqual(
                        client.get("/healthz", headers={"host": host}).status_code, 403
                    )
            self.assertEqual(
                client.get(
                    "/healthz", headers=[("host", "127.0.0.1:8765"), ("host", "evil")]
                ).status_code,
                400,
            )
            for origin in (
                "null",
                "",
                "http://evil.test",
                "http://127.0.0.1",
                "http://127.0.0.1:8766",
                "https://127.0.0.1:8765",
                "http://127.0.0.1:8765/",
                "http://127.0.0.1:8765.evil",
                "http://user@127.0.0.1:8765",
            ):
                with self.subTest(origin=origin):
                    self.assertEqual(
                        client.post(
                            "/local/session", headers={"origin": origin, "x-brainoid-local": "1"}
                        ).status_code,
                        403,
                    )
            self.assertEqual(
                client.post("/local/session", headers={"x-brainoid-local": "1"}).status_code, 403
            )
            self.assertEqual(
                client.post(
                    "/local/session", headers={"origin": self.settings().origin}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/local/session",
                    headers=[
                        ("origin", self.settings().origin),
                        ("origin", self.settings().origin),
                        ("x-brainoid-local", "1"),
                    ],
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/local/session",
                    headers={
                        "origin": self.settings().origin,
                        "x-brainoid-local": "1",
                        "sec-fetch-site": "cross-site",
                    },
                ).status_code,
                403,
            )

    def test_remote_peer_and_proxy_headers_do_not_bypass(self) -> None:
        app = DeviceApplication(self.settings())
        self.addCleanup(app.close)
        with TestClient(
            app.app, base_url=self.settings().origin, client=("192.0.2.20", 12345)
        ) as client:
            self.assertEqual(
                client.get("/healthz", headers={"x-forwarded-for": "127.0.0.1"}).status_code, 403
            )
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/healthz",
                    headers={"host": "evil", "x-forwarded-host": self.settings().authority},
                ).status_code,
                403,
            )

    def test_ipv6_authority_and_origin_are_bracketed(self) -> None:
        with self.client(DEVICE_HOST="::1") as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.bootstrap(client)
            self.assertEqual(client.get("/local/state").status_code, 200)
            self.assertEqual(client.get("/healthz", headers={"host": "::1:8765"}).status_code, 403)
            self.assertEqual(
                client.post(
                    "/local/session", headers={"origin": "http://::1:8765", "x-brainoid-local": "1"}
                ).status_code,
                403,
            )

    def test_mutation_needs_current_session_csrf_and_origin(self) -> None:
        with self.client() as client:
            csrf = self.bootstrap(client)
            cookie = client.cookies.get(COOKIE)
            self.assertIsNotNone(cookie)
            for headers, expected in (
                ({"x-csrf-token": csrf}, 403),
                ({"origin": self.settings().origin}, 403),
                ({"origin": self.settings().origin, "x-csrf-token": "wrong"}, 403),
                ({"origin": "null", "x-csrf-token": csrf}, 403),
                (
                    {
                        "origin": self.settings().origin,
                        "x-csrf-token": csrf,
                        "cookie": f"{COOKIE}=forged",
                    },
                    401,
                ),
                (
                    {
                        "origin": self.settings().origin,
                        "x-csrf-token": csrf,
                        "cookie": f"{COOKIE}={cookie}; {COOKIE}={cookie}",
                    },
                    401,
                ),
            ):
                with self.subTest(expected=expected):
                    self.assertEqual(
                        client.delete("/local/session", headers=headers).status_code, expected
                    )
                    self.assertEqual(client.get("/local/state").status_code, 200)
            self.assertEqual(
                client.delete(
                    "/local/session",
                    headers=[
                        ("origin", self.settings().origin),
                        ("x-csrf-token", csrf),
                        ("x-csrf-token", csrf),
                    ],
                ).status_code,
                400,
            )
            client.cookies.clear()
            second = self.bootstrap(client)
            self.assertNotEqual(second, csrf)
            self.assertEqual(
                client.delete(
                    "/local/session",
                    headers={"origin": self.settings().origin, "x-csrf-token": csrf},
                ).status_code,
                403,
            )

    def test_session_expiry_limit_and_restart_invalidation(self) -> None:
        with self.client() as client:
            self.bootstrap(client)
            cookie = client.cookies.get(COOKIE)
            now = time.monotonic()
            with patch(
                "brainoid_device.local_api.time.monotonic", return_value=now + SESSION_SECONDS + 1
            ):
                self.assertEqual(client.get("/local/state").status_code, 401)
            for _ in range(MAX_SESSIONS):
                client.cookies.clear()
                self.bootstrap(client)
            client.cookies.clear()
            self.assertEqual(
                client.post(
                    "/local/session",
                    headers={"origin": self.settings().origin, "x-brainoid-local": "1"},
                ).status_code,
                429,
            )
        with self.client() as restarted:
            self.assertEqual(
                restarted.get("/local/state", headers={"cookie": f"{COOKIE}={cookie}"}).status_code,
                401,
            )

    def test_static_snapshot_traversal_and_credential_paths(self) -> None:
        with self.client() as client:
            original = client.get("/").text
            (self.static / "index.html").unlink()
            secret = self.private / "secret"
            secret.write_text(SECRET)
            secret.chmod(0o600)
            (self.static / "index.html").symlink_to(secret)
            for path in (
                "/%2e%2e/private/secret",
                "/%252e%252e/private/secret",
                "/foo%2f..%2fsecret",
                "/%00",
                "/private/secret",
                "/.env",
                "/etc/passwd",
                "/config",
                "/credentials",
                "/unknown",
                "/local/commands",
                "/local/audio",
            ):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(SECRET, response.text)
            self.assertEqual(client.get("/").text, original)
            self.assertEqual(client.head("/").content, b"")

    def test_no_cors_methods_bodies_and_bounded_headers(self) -> None:
        with self.client() as client:
            headers = {"origin": self.settings().origin, "x-brainoid-local": "1"}
            self.assertEqual(
                client.post("/local/session", headers=headers, content=SECRET).status_code, 413
            )
            response = client.options(
                "/local/session",
                headers={"origin": "http://evil.test", "access-control-request-method": "POST"},
            )
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("access-control-allow-origin", response.headers)
            self.assertEqual(
                client.get("/healthz", headers={"x-large": "x" * 8192}).status_code, 431
            )
            self.assertEqual(client.get("/" + "x" * 2049).status_code, 414)
            self.assertEqual(client.get("/local/session").status_code, 404)
            for method, path, allowed in (
                ("PUT", "/local/state", {"GET", "HEAD"}),
                ("POST", "/unknown", {"GET", "HEAD"}),
                ("PUT", "/local/session", {"POST", "DELETE"}),
            ):
                with self.subTest(method=method, path=path):
                    response = client.request(method, path, headers=headers)
                    self.assertEqual(response.status_code, 405)
                    self.assertEqual(response.headers["content-type"], "application/json")
                    self.assertEqual(response.json(), {"error": "method_not_allowed"})
                    self.assertEqual(
                        set(response.headers["allow"].replace(" ", "").split(",")), allowed
                    )
                    self.assertIn("no-store", response.headers["cache-control"])
                    self.assertEqual(response.headers["x-content-type-options"], "nosniff")


class DeviceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_tasks_close_and_late_registration_rejected(self) -> None:
        runtime = Runtime(shutdown_seconds=0.1)
        runtime.running = True
        finished = asyncio.Event()

        async def worker() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        runtime.start_task(worker())
        await asyncio.sleep(0)
        await runtime.close()
        self.assertTrue(finished.is_set())
        with self.assertRaises(RuntimeError):
            runtime.start_task(worker())
        with self.assertRaises(RuntimeError):
            await runtime.start_child([sys.executable, "-c", "pass"])

    async def test_cancel_during_real_subprocess_creation_keeps_ownership(self) -> None:
        runtime = Runtime(shutdown_seconds=0.1)
        runtime.running = True
        created = asyncio.Event()
        release = asyncio.Event()
        original_spawn = asyncio.create_subprocess_exec
        children: list[asyncio.subprocess.Process] = []

        async def delayed_spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
            child = await original_spawn(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            children.append(child)
            created.set()
            await release.wait()
            return child

        with patch("brainoid_device.runtime.asyncio.create_subprocess_exec", delayed_spawn):
            caller = asyncio.create_task(
                runtime.start_child([sys.executable, "-c", "import time; time.sleep(300)"])
            )
            await asyncio.wait_for(created.wait(), 3)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            closing = asyncio.create_task(runtime.close())
            await asyncio.sleep(0)
            release.set()
            try:
                await asyncio.wait_for(closing, 3)
                self.assertIsNotNone(children[0].returncode)
            finally:
                for child in children:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()

    async def test_completed_tasks_are_released_and_failures_consumed(self) -> None:
        runtime = Runtime()
        runtime.running = True

        async def failed() -> None:
            raise RuntimeError(SECRET)

        task = runtime.start_task(failed())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(task.done())
        self.assertTrue(runtime.resource_failed)
        self.assertEqual(runtime._tasks, set())
        await runtime.close()


@unittest.skipUnless(sys.platform == "linux", "Pi service process acceptance requires Linux")
class DeviceProcessTests(DeviceFixture):
    def child_environment(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items() if not key.startswith("DEVICE_")}

    def start_daemon(
        self, *, host: str = "127.0.0.1", fixture: bool = False, **extra: str
    ) -> tuple[subprocess.Popen[bytes], int, Path]:
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        with socket.socket(family) as reserved:
            reserved.bind((host, 0))
            port = int(reserved.getsockname()[1])
        output = self.root / f"process-{port}.log"
        env = {
            **self.child_environment(),
            **self.env,
            "DEVICE_HOST": host,
            "DEVICE_PORT": str(port),
            **extra,
        }
        argv = [sys.executable, "-B", "-m", "brainoid_device", "serve"]
        if fixture:
            argv = [sys.executable, "-B", str(ROOT / "tests/fixtures/device_service/run.py")]
            env["DEVICE_TEST_MARKER"] = str(self.root / "worker.ready")
        with output.open("wb") as log:
            process = subprocess.Popen(
                argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        birth = Path(f"/proc/{process.pid}/stat").read_text().split()[21]
        print(f"owned daemon pid={process.pid} birth={birth} host={host} port={port}")
        self.addCleanup(self.stop_daemon, process, output)
        return process, port, output

    def stop_daemon(self, process: subprocess.Popen[bytes], output: Path) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=18)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # Cleanup is limited to a child whose PID AND Linux birth time match its
        # recorded fixture output. This also runs if an assertion/startup fails.
        for line in output.read_text().splitlines():
            if not line.startswith('{"child_pid":'):
                continue
            owned = json.loads(line)
            proc_stat = Path(f"/proc/{owned['child_pid']}/stat")
            try:
                if proc_stat.read_text().split()[21] == owned["birth"]:
                    os.kill(owned["child_pid"], signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass

    def request(
        self,
        port: int,
        path: str,
        method: str = "GET",
        *,
        host: str = "127.0.0.1",
        headers: list[tuple[str, str]] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            authority = f"[{host}]:{port}" if host == "::1" else f"{host}:{port}"
            for key, value in headers if headers is not None else [("Host", authority)]:
                connection.putheader(key, value)
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def wait_ready(
        self, process: subprocess.Popen[bytes], port: int, output: Path, host: str = "127.0.0.1"
    ) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"daemon exited {process.returncode}: {output.read_text()}")
            try:
                if self.request(port, "/healthz", host=host)[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.02)
        self.fail("daemon health deadline exceeded")

    def assert_closed(
        self, process: subprocess.Popen[bytes], port: int, host: str = "127.0.0.1"
    ) -> None:
        self.assertIsNotNone(process.poll())
        with self.assertRaises(OSError):
            socket.create_connection((host, port), timeout=0.3)

    def test_real_ipv4_http_reload_origin_parser_and_lan_boundary(self) -> None:
        process, port, output = self.start_daemon()
        self.wait_ready(process, port, output)
        authority = f"127.0.0.1:{port}"
        origin = f"http://{authority}"
        bootstrap = [("Host", authority), ("Origin", origin), ("X-Brainoid-Local", "1")]
        status, headers, body = self.request(port, "/local/session", "POST", headers=bootstrap)
        self.assertEqual(status, 200)
        cookie = headers["set-cookie"].split(";", 1)[0]
        csrf = str(json.loads(body)["csrf_token"])
        session_headers = [("Host", authority), ("Cookie", cookie)]
        before = self.request(port, "/local/state", headers=session_headers)[2]
        for _ in range(3):
            self.assertEqual(self.request(port, "/")[0], 200)
            self.assertEqual(self.request(port, "/app.js")[0], 200)
            self.assertEqual(self.request(port, "/local/state", headers=session_headers)[2], before)
            self.assertIsNone(process.poll())
        for invalid in (
            [("Host", "evil.test")],
            [("Host", "127.0.0.1")],
            [("Host", f"127.0.0.1:{port + 1}")],
        ):
            self.assertEqual(self.request(port, "/healthz", headers=invalid)[0], 403)
        # h11 rejects absent/duplicate Host before the ASGI application.
        self.assertEqual(self.request(port, "/healthz", headers=[])[0], 400)
        self.assertEqual(
            self.request(port, "/healthz", headers=[("Host", authority), ("Host", authority)])[0],
            400,
        )
        self.assertEqual(
            self.request(port, "/local/session", "POST", headers=bootstrap + [("Origin", origin)])[
                0
            ],
            400,
        )
        self.assertEqual(
            self.request(
                port,
                "/local/session",
                "DELETE",
                headers=session_headers + [("Origin", "http://evil.test"), ("X-CSRF-Token", csrf)],
            )[0],
            403,
        )
        self.assertEqual(self.request(port, "/local/state", headers=session_headers)[2], before)
        self.assertEqual(self.request(port, "/%2e%2e/private/secret")[0], 404)
        for method, path, allowed in (
            ("PUT", "/local/state", {"GET", "HEAD"}),
            ("POST", "/unknown", {"GET", "HEAD"}),
            ("PUT", "/local/session", {"POST", "DELETE"}),
        ):
            status, response_headers, payload = self.request(
                port, path, method, headers=[("Host", authority), ("Origin", origin)]
            )
            self.assertEqual(status, 405)
            self.assertEqual(response_headers["content-type"], "application/json")
            self.assertEqual(json.loads(payload), {"error": "method_not_allowed"})
            self.assertEqual(set(response_headers["allow"].replace(" ", "").split(",")), allowed)
        # Query the local interface table only; no DNS, proxy or external network.
        import fcntl
        import struct

        addresses: list[str] = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, name in socket.if_nameindex():
                try:
                    info = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()))
                    address = socket.inet_ntoa(info[20:24])
                except OSError:
                    continue
                if not address.startswith("127."):
                    addresses.append(address)
        self.assertTrue(addresses, "Linux acceptance requires a non-loopback interface")
        for address in addresses:
            with self.assertRaises(OSError):
                socket.create_connection((address, port), timeout=0.3)
        process.terminate()
        self.assertEqual(process.wait(timeout=8), 0)
        self.assert_closed(process, port)
        self.assertIn("device_stopped", output.read_text())
        self.assertNotIn(SECRET, output.read_text())
        print(
            "verified real IPv4 UI reload x3; same daemon state; LAN refused; SIGTERM exit=0; listener closed"
        )

    def test_real_ipv6_start_and_stop(self) -> None:
        process, port, output = self.start_daemon(host="::1")
        self.wait_ready(process, port, output, "::1")
        self.assertEqual(self.request(port, "/", host="::1")[0], 200)
        self.assertEqual(
            self.request(
                port,
                "/local/session",
                "POST",
                host="::1",
                headers=[
                    ("Host", f"[::1]:{port}"),
                    ("Origin", f"http://[::1]:{port}"),
                    ("X-Brainoid-Local", "1"),
                ],
            )[0],
            200,
        )
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.3)
        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=8), 0)
        self.assert_closed(process, port, "::1")

    def test_real_children_and_tasks_cleaned_after_ui_reload(self) -> None:
        process, port, output = self.start_daemon(fixture=True)
        self.wait_ready(process, port, output)
        owned = json.loads(output.read_text().splitlines()[0])
        for _ in range(3):
            self.assertEqual(self.request(port, "/")[0], 200)
        self.assertTrue(Path(f"/proc/{owned['child_pid']}").exists())
        started = time.monotonic()
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 0, output.read_text())
        self.assertLess(time.monotonic() - started, 9)
        self.assert_closed(process, port)
        self.assertFalse(Path(f"/proc/{owned['child_pid']}").exists())
        self.assertTrue((self.root / "worker.closed").exists())
        print(
            "verified stubborn child SIGKILL/reap, task finally and listener close after daemon SIGTERM"
        )

    def test_broken_task_cannot_hold_process_open_indefinitely(self) -> None:
        process, port, output = self.start_daemon(fixture=True, DEVICE_TEST_STUBBORN_TASK="1")
        self.wait_ready(process, port, output)
        owned = json.loads(output.read_text().splitlines()[0])
        started = time.monotonic()
        process.terminate()
        self.assertEqual(process.wait(timeout=12), 1, output.read_text())
        self.assertLess(time.monotonic() - started, 11)
        self.assert_closed(process, port)
        self.assertFalse(Path(f"/proc/{owned['child_pid']}").exists())
        self.assertNotIn(SECRET, output.read_text())

    def test_partial_startup_failure_reaps_children_without_secret_log(self) -> None:
        process, port, output = self.start_daemon(fixture=True, DEVICE_TEST_STARTUP_FAIL="1")
        self.assertEqual(process.wait(timeout=10), 1)
        self.assert_closed(process, port)
        owned = json.loads(output.read_text().splitlines()[0])
        self.assertFalse(Path(f"/proc/{owned['child_pid']}").exists())
        self.assertNotIn(SECRET, output.read_text())

    def wait_starting(
        self, process: subprocess.Popen[bytes], port: int, output: Path
    ) -> dict[str, object]:
        marker = self.root / "worker.starting"
        deadline = time.monotonic() + 4
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"startup fixture exited {process.returncode}: {output.read_text()}")
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "startup hook did not reach waiting state")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.3)
        owned: dict[str, object] = json.loads(output.read_text().splitlines()[0])
        print("owned startup child " + json.dumps(owned, sort_keys=True))
        return owned

    def check_pending_startup_stop(self, sig: signal.Signals, stubborn: bool) -> None:
        process, port, output = self.start_daemon(
            fixture=True,
            DEVICE_TEST_STARTUP_WAIT="1",
            DEVICE_TEST_STUBBORN_STARTUP="1" if stubborn else "0",
        )
        owned = self.wait_starting(process, port, output)
        started = time.monotonic()
        process.send_signal(sig)
        # A repeated signal must still follow resource cleanup rather than skip lifespan.
        time.sleep(0.02)
        process.send_signal(sig)
        self.assertEqual(process.wait(timeout=12), 1 if stubborn else 0, output.read_text())
        self.assertLess(time.monotonic() - started, 11)
        self.assert_closed(process, port)
        self.assertFalse(Path(f"/proc/{owned['child_pid']}").exists())
        self.assertTrue((self.root / "worker.cancelled").exists())
        self.assertTrue((self.root / "worker.closed").exists())
        if not stubborn:
            self.assertTrue((self.root / "worker.startup_closed").exists())
        self.assertNotIn(SECRET, output.read_text())
        self.assertNotIn("Traceback", output.read_text())
        print(
            f"startup stop signal={sig.name} cancellation_resistant={stubborn} exit={process.returncode}; child reaped; listener closed"
        )

    def test_sigterm_during_waiting_startup_closes_owned_resources(self) -> None:
        self.check_pending_startup_stop(signal.SIGTERM, False)

    def test_sigint_during_waiting_startup_closes_owned_resources(self) -> None:
        self.check_pending_startup_stop(signal.SIGINT, False)

    def test_sigterm_bounds_cancellation_resistant_startup(self) -> None:
        self.check_pending_startup_stop(signal.SIGTERM, True)

    def test_sigint_bounds_cancellation_resistant_startup(self) -> None:
        self.check_pending_startup_stop(signal.SIGINT, True)

    def test_startup_deadline_fails_and_reaps_owned_resources_without_signal(self) -> None:
        process, port, output = self.start_daemon(fixture=True, DEVICE_TEST_STARTUP_WAIT="1")
        owned = self.wait_starting(process, port, output)
        self.assertEqual(process.wait(timeout=12), 1, output.read_text())
        self.assert_closed(process, port)
        self.assertFalse(Path(f"/proc/{owned['child_pid']}").exists())
        self.assertTrue((self.root / "worker.startup_closed").exists())
        self.assertTrue((self.root / "worker.closed").exists())
        self.assertNotIn(SECRET, output.read_text())
        self.assertNotIn("Traceback", output.read_text())
        print("startup deadline exit=1; child reaped; task finally completed; listener closed")

    def test_stop_signals_wake_idle_startup_without_periodic_work(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                process, port, output = self.start_daemon(
                    fixture=True, DEVICE_TEST_STARTUP_EVENT_ONLY="1"
                )
                deadline = time.monotonic() + 3
                while "startup_waiting" not in output.read_text():
                    self.assertIsNone(process.poll(), output.read_text())
                    self.assertLess(time.monotonic(), deadline, "idle startup did not begin")
                    time.sleep(0.01)
                # No child, worker or repeating timer can wake this fixture. The
                # service's 5-second startup deadline is its only scheduled timer.
                sent = time.monotonic()
                process.send_signal(sig)
                self.assertEqual(process.wait(timeout=4), 0, output.read_text())
                events = [json.loads(line) for line in output.read_text().splitlines()]
                finished = next(e["monotonic"] for e in events if e["event"] == "startup_finally")
                self.assertLess(finished - sent, 2, "cleanup waited for startup's deadline")
                self.assertIn({"event": "signals_restored", "restored": True}, events)
                self.assert_closed(process, port)
                print(
                    json.dumps(
                        {
                            "idle_startup_signal": sig.name,
                            "signal_sent": sent,
                            "startup_finally": finished,
                            "signal_to_finally_seconds": finished - sent,
                            "exit": process.returncode,
                            "signals_restored": True,
                            "listener_closed": True,
                        }
                    )
                )

    def test_cli_check_and_sanitized_startup_failures(self) -> None:
        for overrides, expected in (
            ({}, "DEVICE_HOST: required"),
            ({**self.env, "DEVICE_HOST": SECRET}, "DEVICE_HOST:"),
            ({**self.env, "DEVICE_STATIC_DIR": str(self.root / SECRET)}, "DEVICE_STATIC_DIR:"),
        ):
            with self.subTest(expected=expected):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "brainoid_device", "serve"],
                    cwd=ROOT,
                    env={**self.child_environment(), **overrides},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
                self.assertNotIn(SECRET, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "brainoid_device", "check"],
            cwd=ROOT,
            env={**self.child_environment(), **self.env},
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "device_configuration_ok")
        with socket.socket(socket.AF_INET) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = subprocess.run(
                [sys.executable, "-B", "-m", "brainoid_device", "serve"],
                cwd=ROOT,
                env={
                    **self.child_environment(),
                    **self.env,
                    "DEVICE_PORT": str(occupied.getsockname()[1]),
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
