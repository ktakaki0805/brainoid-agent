"""DEV10: real API contracts, serialized durable credentials and local transport fences."""

import asyncio
import ipaddress
import json
import logging
import os
import socket
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from brainoid_api.auth.runtime import AuthServices, authenticated_identity
from brainoid_api.device_gateway import DeviceGateway
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.routes.agents import reader_identity
from brainoid_api.websocket_transport import DeviceWebSocketProtocol
from brainoid_contracts import ResourceId
from brainoid_device.app import DeviceApplication
from brainoid_device.cloud_client import JOURNAL_NAME, CloudClient
from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import EVENTS, PROTOCOL, SSEDecoder, decode
from brainoid_device.config import CloudSettings, ConfigurationError, Settings
from brainoid_device.device_transport import DeviceTransport, NoRedirect, backoff, planned_reconnect
from brainoid_device.ledger import DeviceStorage
from brainoid_device.local_api import Sessions
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from brainoid_device.ui_bridge import CreateTurn, GetConversation, GetRun, UIBridge
from brainoid_runs.models import TurnCreate
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_turn_run import TurnRunFixture


def local_tls(root: Path) -> tuple[Path, Path, ssl.SSLContext]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DEV10 isolated fixture")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = root / "tls-cert.pem", root / "tls-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyfile.chmod(0o600)
    return certfile, keyfile, ssl.create_default_context(cafile=str(certfile))


class Proxy(httpx.AsyncBaseTransport):
    def __init__(self, app: Any) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.requests: list[tuple[str, bytes, str | None]] = []
        self.lose = ""
        self.losses = 0
        self.reject_token: str | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            (request.url.path, request.content, request.headers.get("idempotency-key"))
        )
        if (
            self.reject_token
            and request.headers.get("authorization") == "Bearer " + self.reject_token
        ):
            return httpx.Response(401)
        response = await self.inner.handle_async_request(request)
        if self.release and request.url.path.endswith(self.lose):
            self.entered.set()
            await self.release.wait()
        if self.losses and request.url.path.endswith(self.lose):
            self.losses -= 1
            await response.aclose()
            raise httpx.ReadError("synthetic credential must never escape")
        return response


class CloudFixture(TurnRunFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conversation_id = self.conversation()
        self.app.dependency_overrides.pop(reader_identity)
        self.app.dependency_overrides.pop(authenticated_identity)
        self.root = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="ba-dev-10-writer-cloud-"))
        )
        for name in ("credentials", "state", "assets"):
            (self.root / name).mkdir(mode=0o700)
        self.private_store = CredentialStore(self.root / "credentials")
        self.local_runtime = Runtime()
        self.local_storage = DeviceStorage(
            self.root / "state",
            self.private_store,
            cleanup_private=self.local_runtime.clear_private,
        )
        self.local_runtime.running = True
        self.proxy = Proxy(self.app)
        self.cloud_settings = CloudSettings("http://127.0.0.1:18998", local_test=True)
        self.sleeps: list[float] = []
        self.cloud = self.new_cloud()
        self.bridge = UIBridge(self.cloud, Sessions())
        self.addAsyncCleanup(self.cleanup_cloud)

    def new_cloud(self, *, transport: httpx.AsyncBaseTransport | None = None) -> CloudClient:
        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.clock.advance(seconds)
            await asyncio.sleep(0)

        return CloudClient(
            self.cloud_settings,
            self.local_runtime,
            self.local_storage,
            self.private_store,
            now=lambda: self.clock.now().timestamp(),
            sleep=sleep,
            transport=transport or self.proxy,
        )

    async def cleanup_cloud(self) -> None:
        await self.cloud.close()
        await self.local_runtime.close()
        self.local_storage.close()
        self.private_store.close()

    async def paired(self) -> None:
        public = await self.cloud.begin_pairing("Synthetic Pi")
        self.assertEqual(set(public), {"user_code", "expires_at"})
        pairing = self.cloud.journal.pairing
        assert pairing is not None
        self.confirm(pairing)  # type: ignore[arg-type]
        self.assertTrue(await self.cloud.exchange())
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
            "capabilities"
        ] = ["display"]


class CloudTests(CloudFixture):
    async def test_actual_pairing_exchange_and_shared_http_no_user_credential(self) -> None:
        await self.paired()
        result = await self.bridge.operation(
            GetConversation(operation="get_conversation", conversation_id=self.conversation_id)
        )
        self.assertEqual(result["id"], self.conversation_id)
        self.assertEqual(self.local_storage.snapshot().status, "ready")
        self.assertEqual(os.stat(self.root / "credentials" / JOURNAL_NAME).st_mode & 0o777, 0o600)
        self.assertNotIn("access_token", json.dumps(result))
        self.assertEqual(repr(self.cloud.journal.credentials).count("access_token"), 0)
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.assertEqual(
            self.local_storage.credential_reference(registration.epoch, "cloud_refresh"),
            JOURNAL_NAME,
        )

    async def test_concurrent_401_has_one_serialized_rotation(self) -> None:
        await self.paired()
        self.proxy.reject_token = await self.cloud.token()
        self.clock.advance(1)
        responses = await asyncio.gather(
            *[
                self.bridge.operation(
                    GetConversation(
                        operation="get_conversation", conversation_id=self.conversation_id
                    )
                )
                for _ in range(8)
            ],
            return_exceptions=True,
        )
        self.assertTrue(
            all(
                isinstance(value, dict) and value["id"] == self.conversation_id
                for value in responses
            )
        )
        refreshes = [r for r in self.proxy.requests if r[0].endswith("device-tokens:refresh")]
        self.assertEqual(len(refreshes), 1)
        self.assertNotEqual(await self.cloud.token(), self.proxy.reject_token)

    async def test_refresh_response_loss_reuses_exact_body_and_key(self) -> None:
        await self.paired()
        self.proxy.lose, self.proxy.losses = "device-tokens:refresh", 1
        original = await self.cloud.token()
        self.clock.advance(1)
        refreshed = await self.cloud.token(rejected=original)
        self.assertTrue(original != refreshed)
        refreshes = [r for r in self.proxy.requests if r[0].endswith(self.proxy.lose)]
        self.assertEqual(len(refreshes), 2)
        self.assertEqual(refreshes[0], refreshes[1])
        self.assertEqual(self.sleeps, [1])
        self.repo.authenticate(refreshed)

    async def test_restart_after_remote_refresh_success_reuses_pending_identity(self) -> None:
        await self.paired()
        original = await self.cloud.token()
        self.clock.advance(1)
        self.proxy.lose, self.proxy.release = "device-tokens:refresh", asyncio.Event()
        pending = asyncio.create_task(self.cloud.token(rejected=original))
        await self.proxy.entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        saved = decode(self.private_store.read(JOURNAL_NAME))["pending_auth"]
        self.assertEqual(saved["attempts"], 1)
        await self.cloud.close()
        self.proxy.release = None
        self.cloud = self.new_cloud()
        refreshed = await self.cloud.token()
        self.assertTrue(original != refreshed)
        refreshes = [r for r in self.proxy.requests if r[0].endswith(self.proxy.lose)]
        self.assertEqual(refreshes[0], refreshes[1])
        self.assertIsNone(self.cloud.journal.pending_auth)

    async def test_expired_pending_refresh_does_not_reuse_old_token(self) -> None:
        await self.paired()
        self.proxy.lose, self.proxy.release = "device-tokens:refresh", asyncio.Event()
        pending = asyncio.create_task(self.cloud.token(rejected=await self.cloud.token()))
        await self.proxy.entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await self.cloud.close()
        self.clock.advance(60)
        self.cloud = self.new_cloud()
        count = len(self.proxy.requests)
        with self.assertRaises(CloudError):
            await self.cloud.token()
        self.assertEqual(len(self.proxy.requests), count)
        self.assertEqual(self.cloud.status, "stopped")
        self.assertIsNone(self.cloud.journal.credentials)
        self.assertTrue(decode(self.private_store.read(JOURNAL_NAME))["stopped"])

    async def test_refresh_refusal_clears_private_and_stops_future_operations(self) -> None:
        await self.paired()
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        identity = self.repo.authenticate(credentials.access_token)
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        cleared: list[bool] = []
        self.local_runtime.register_private_cleanup(lambda: cleared.append(True))
        with self.assertRaises(CloudError):
            await self.bridge.operation(
                GetConversation(operation="get_conversation", conversation_id=self.conversation_id)
            )
        self.assertEqual(cleared, [True])
        self.assertFalse(self.cloud.active)
        before = len(self.proxy.requests)
        with self.assertRaises(CloudError):
            await self.cloud.token()
        self.assertEqual(before, len(self.proxy.requests))

    async def test_family_absolute_expiry_and_clock_rollback_fail_closed(self) -> None:
        await self.paired()
        self.clock.advance(30 * 86400)
        with self.assertRaises(CloudError):
            await self.cloud.token()
        self.assertFalse(self.cloud.active)

    async def test_mutation_response_loss_only_one_turn_and_same_key(self) -> None:
        await self.paired()
        self.proxy.lose, self.proxy.losses = "/turns", 1
        operation = CreateTurn(
            operation="create_turn",
            operation_id=ResourceId(str(uuid4())),
            conversation_id=self.conversation_id,
            turn=TurnCreate(text="Synthetic request", output_mode="text"),
        )
        result = await self.bridge.operation(operation)
        self.assertNotIn(b"Synthetic request", self.private_store.read(JOURNAL_NAME))
        self.assertEqual(self.cloud.journal.pending_mutation.body, {})  # type: ignore[union-attr]
        self.assertEqual(set(result), {"run_id", "status"})
        requests = [r for r in self.proxy.requests if r[0].endswith("/turns")]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        messages = [
            p
            for p in self.store.data
            if p.startswith(f"tenants/{self.tenant}/conversations/{self.conversation_id}/messages/")
        ]
        self.assertEqual(len(messages), 1)

    async def test_mutation_ambiguous_budget_survives_restart_and_blocks_new_key(self) -> None:
        await self.paired()
        self.proxy.lose, self.proxy.losses = "/turns", 3
        operation = CreateTurn(
            operation="create_turn",
            operation_id=ResourceId(str(uuid4())),
            conversation_id=self.conversation_id,
            turn=TurnCreate(text="Synthetic request", output_mode="text"),
        )
        with self.assertRaises(CloudError):
            await self.bridge.operation(operation)
        self.assertNotIn(b"Synthetic request", self.private_store.read(JOURNAL_NAME))
        self.assertEqual(len([r for r in self.proxy.requests if r[0].endswith("/turns")]), 3)
        await self.cloud.close()
        self.cloud = self.new_cloud()
        self.bridge = UIBridge(self.cloud, Sessions())
        before = len(self.proxy.requests)
        for value in (
            operation,
            operation.model_copy(update={"operation_id": ResourceId(str(uuid4()))}),
        ):
            with self.assertRaises(CloudError):
                await self.bridge.operation(value)
        self.assertEqual(before, len(self.proxy.requests))

    async def test_reset_racing_late_refresh_does_not_resurrect_credentials(self) -> None:
        await self.paired()
        self.proxy.lose, self.proxy.release = "device-tokens:refresh", asyncio.Event()
        task = asyncio.create_task(self.cloud.token(rejected=await self.cloud.token()))
        await self.proxy.entered.wait()
        self.local_runtime.fence_operations()
        self.private_store.begin_reset()
        self.local_runtime.clear_private()
        self.assertTrue(self.local_storage.local_reset())
        self.proxy.release.set()
        with self.assertRaises(CloudError):
            await task
        self.assertEqual(list((self.root / "credentials").iterdir()), [])
        self.assertEqual(self.local_storage.snapshot().status, "unregistered")

    async def test_real_local_http_and_wss_gateway_generations_and_shutdown(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        leases = DeviceLeases(self.store, self.repo, now=self.clock.now)
        self.app.state.auth_services = AuthServices(
            devices=self.repo, device_gateway=DeviceGateway(leases)
        )
        certfile, keyfile, tls = local_tls(self.root)
        server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                lifespan="off",
                log_config=None,
                access_log=False,
                ws=DeviceWebSocketProtocol,
                ws_max_size=65536,
                ws_per_message_deflate=False,
                ssl_certfile=str(certfile),
                ssl_keyfile=str(keyfile),
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started)
            await self.cloud.close()
            self.cloud_settings = CloudSettings(f"https://127.0.0.1:{port}", local_test=True)
            self.cloud = CloudClient(
                self.cloud_settings,
                self.local_runtime,
                self.local_storage,
                self.private_store,
                now=lambda: self.clock.now().timestamp(),
            )
            await self.cloud.http.aclose()
            self.cloud.http = httpx.AsyncClient(verify=tls, trust_env=False)
            await self.paired()
            device = DeviceTransport(self.cloud)
            token = await self.cloud.token()
            with patch("brainoid_device.device_transport.NoRedirect", partial(NoRedirect, ssl=tls)):
                for generation in (1, 2):
                    task = self.cloud.start(device.connection(token))
                    for _ in range(100):
                        if device.generation == generation:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(device.generation, generation)
                    registration = self.local_storage.snapshot().registration
                    assert registration is not None
                    self.assertEqual(registration.connection_generation, generation)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self.assertIsNone(device.socket)
            identity = self.repo.authenticate(token)
            self.assertEqual(identity.connection_generation, 1)
            self.assertEqual(len(self.cloud._tasks), 0)
        finally:
            await self.cloud.close()
            server.should_exit = True
            async with asyncio.timeout(10):
                await serving
            listener.close()

    async def test_schema_parity_partial_utf8_sse_heartbeat_and_frame_bounds(self) -> None:
        self.assertEqual(
            PROTOCOL, json.loads(Path("specs/device-protocol.schema.json").read_text())
        )
        self.assertEqual(EVENTS, json.loads(Path("specs/run-events.schema.json").read_text()))
        event = {
            "run_id": str(uuid4()),
            "seq": 2,
            "type": "text.delta",
            "occurred_at": self.clock.now().isoformat(),
            "payload": {"text": "日🙂"},
        }
        raw = (
            ": heartbeat\r\n\r\nid: 2\r\nevent: text.delta\r\ndata: "
            + json.dumps(event, ensure_ascii=False)
            + "\r\n\r\n"
        ).encode()
        decoder, values = SSEDecoder(), []
        for byte in raw:
            values.extend(decoder.feed(bytes([byte])))
        self.assertEqual(values, [event])
        for bad in (
            b"x" * 65537,
            b"\xff",
            b'id: 2\nevent: text.delta\ndata: {"seq":2,"seq":2}\n\n',
        ):
            with self.assertRaises(CloudError):
                SSEDecoder().feed(bad)

    async def test_real_api_sse_durable_cursor_reopen_and_410_history(self) -> None:
        await self.paired()
        accepted = await self.bridge.operation(
            CreateTurn(
                operation="create_turn",
                operation_id=ResourceId(str(uuid4())),
                conversation_id=self.conversation_id,
                turn=TurnCreate(text="Synthetic request", output_mode="text"),
            )
        )
        run = ResourceId(accepted["run_id"])
        await asyncio.to_thread(self.execute, run)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        await self.bridge.events(run, None, queue)
        saved = self.cloud.journal.cursors[0].seq
        self.assertGreater(saved, 1)
        first = b"".join(queue.get_nowait() or b"" for _ in range(queue.qsize()))
        self.assertIn(b"run.completed", first)
        await self.cloud.close()
        self.cloud = self.new_cloud()
        self.bridge = UIBridge(self.cloud, Sessions())
        self.cloud.journal.cursors[0].seq = 1
        self.cloud.save()
        self.clock.advance(86400)
        await self.bridge.events(run, None, queue)
        after = b"".join(queue.get_nowait() or b"" for _ in range(queue.qsize()))
        self.assertIn(b"event: history", after)
        self.assertNotIn(b"text.delta", after)
        self.assertIn(b'"play_speech":false', after)
        self.assertEqual(self.cloud.journal.cursors[0].seq, saved)

    async def test_untrusted_error_bodies_never_reach_ui_or_logs(self) -> None:
        await self.paired()
        secret = await self.cloud.token()

        async def bad(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": secret, "refresh_token": "synthetic-secret"})

        await self.cloud.http.aclose()
        self.cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(bad))
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("brainoid.test").info("content-free fixture observation")
            with self.assertRaises(CloudError) as raised:
                await self.bridge.operation(
                    GetRun(operation="get_run", run_id=ResourceId(str(uuid4())))
                )
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertFalse(secret in " ".join(captured.output))
        self.assertFalse("synthetic-secret" in " ".join(captured.output))


class CloudConfigurationTests(unittest.TestCase):
    def test_trusted_origin_and_explicit_loopback_gate(self) -> None:
        for value in (
            "http://example.com",
            "https://u:p@example.com",
            "https://example.com/v1",
            "https://example.com?token=x",
            "https://example.com#x",
            "https://example.com\\x",
        ):
            with self.assertRaises(ConfigurationError):
                CloudSettings(value)
        with self.assertRaises(ConfigurationError):
            CloudSettings("http://127.0.0.1:8766")
        CloudSettings("http://127.0.0.1:8766", local_test=True)
        CloudSettings("https://api.example.test")
        with self.assertRaises(ConfigurationError):
            CloudSettings("https://api.example.test", local_test=True)

    def test_backoff_and_planned_reconnect_boundaries(self) -> None:
        for attempt in range(100):
            for jitter in (0.0, 0.5, 1.0):

                def fixed(value: float = jitter) -> float:
                    return value

                self.assertTrue(1 <= backoff(attempt, fixed) <= 30)
        self.assertEqual(backoff(0, lambda: 1), 1)
        self.assertEqual(backoff(1, lambda: 1), 2)
        self.assertEqual(backoff(2, lambda: 1), 4)
        self.assertEqual(planned_reconnect(lambda: 0.5), 3300)


class LocalBridgeTests(CloudFixture):
    async def test_production_lifespan_typed_boundary_pairing_and_reset(self) -> None:
        # Composition uses real DeviceApplication and its Runtime/ResetCommand;
        # only remote I/O and wall clock use the existing API fixture.
        directory = self.root / "application"
        directory.mkdir(mode=0o700)
        for name in ("credentials", "state", "assets"):
            (directory / name).mkdir(mode=0o700)
        (directory / "assets" / "index.html").write_text("<!doctype html><title>Fixture</title>")
        settings = Settings(
            "127.0.0.1",
            18765,
            directory / "assets",
            directory / "credentials",
            directory / "state",
            self.cloud_settings,
        )

        def factory(
            config: CloudSettings,
            runtime: Runtime,
            storage: DeviceStorage,
            credentials: CredentialStore,
        ) -> CloudClient:
            return CloudClient(
                config,
                runtime,
                storage,
                credentials,
                now=lambda: self.clock.now().timestamp(),
                transport=self.proxy,
            )

        with patch("brainoid_device.app.CloudClient", side_effect=factory):
            application = DeviceApplication(settings)
        assert application.cloud is not None
        cloud = application.cloud
        async with application.app.app.router.lifespan_context(application.app.app):  # type: ignore[attr-defined]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application.app, client=("127.0.0.1", 5500)),
                base_url=settings.origin,
            ) as browser:
                boot = await browser.post(
                    "/local/session", headers={"Origin": settings.origin, "X-Brainoid-Local": "1"}
                )
                self.assertEqual(boot.status_code, 200)
                headers = {"Origin": settings.origin, "X-CSRF-Token": boot.json()["csrf_token"]}
                response = await browser.post(
                    "/local/cloud/pairing",
                    headers=headers,
                    json={"device_name": "Synthetic local Pi"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(set(response.json()), {"user_code", "expires_at"})
                assert cloud.journal.pairing is not None
                self.confirm(cloud.journal.pairing)  # type: ignore[arg-type]
                await cloud.exchange()
                assert cloud.journal.credentials is not None
                secret = cloud.journal.credentials.access_token
                self.store.data[
                    f"tenants/{self.tenant}/devices/{cloud.journal.credentials.device_id}"
                ]["capabilities"] = ["display"]
                op = {"operation": "get_conversation", "conversation_id": self.conversation_id}
                response = await browser.post("/local/cloud/operations", headers=headers, json=op)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["id"], self.conversation_id)
                self.assertNotIn(secret, response.text)
                cases = [
                    ({**headers, "Origin": "http://evil.test"}, op, 403),
                    ({**headers, "Host": "evil.test"}, op, 403),
                    ({"Origin": settings.origin}, op, 403),
                    ({**headers, "Authorization": "Bearer " + secret}, op, 400),
                    (headers, {**op, "url": "https://evil.test/"}, 400),
                    (headers, {**op, "headers": {"Authorization": secret}}, 400),
                    (
                        headers,
                        {"operation": "proxy", "method": "GET", "url": "https://evil.test"},
                        400,
                    ),
                    (headers, {**op, "conversation_id": "../tokens"}, 400),
                ]
                before = len(self.proxy.requests)
                for supplied, body, expected in cases:
                    rejected = await browser.post(
                        "/local/cloud/operations", headers=supplied, json=body
                    )
                    self.assertEqual(rejected.status_code, expected, rejected.text)
                    self.assertNotIn(secret, rejected.text)
                duplicate = await browser.post(
                    "/local/cloud/operations",
                    headers=[*headers.items(), ("Origin", settings.origin)],
                    json=op,
                )
                self.assertEqual(duplicate.status_code, 400)
                oversized = await browser.post(
                    "/local/cloud/operations", headers=headers, json={"x": "x" * 33000}
                )
                self.assertEqual(oversized.status_code, 413)
                wrong_type = await browser.post(
                    "/local/cloud/operations", headers=headers, content="{}"
                )
                self.assertEqual(wrong_type.status_code, 413)
                query = await browser.post(
                    "/local/cloud/operations?url=https://evil.test", headers=headers, json=op
                )
                self.assertEqual(query.status_code, 400)
                old_route = await browser.post("/local/reset/prepare", headers=headers, json={})
                self.assertEqual(old_route.status_code, 413)
                self.assertEqual(len(self.proxy.requests), before)
                prepared = await browser.post("/local/reset/prepare", headers=headers)
                reset_headers = {
                    **headers,
                    "X-Brainoid-Confirm": "local-administrator",
                    "X-Brainoid-Reset-Token": prepared.json()["reset_token"],
                }
                response = await browser.post("/local/reset", headers=reset_headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(cloud.active)
                self.assertIsNone(cloud.journal.credentials)
                self.assertEqual(list((directory / "credentials").iterdir()), [])
                self.assertEqual((await browser.get("/local/cloud/status")).status_code, 401)
                boot2 = await browser.post(
                    "/local/session", headers={"Origin": settings.origin, "X-Brainoid-Local": "1"}
                )
                self.assertEqual(boot2.status_code, 200)
                self.assertEqual(
                    (await browser.get("/local/cloud/status")).json(), {"status": "stopped"}
                )
                stopped = await browser.post(
                    "/local/cloud/pairing",
                    headers={"Origin": settings.origin, "X-CSRF-Token": boot2.json()["csrf_token"]},
                    json={"device_name": "Should not restart"},
                )
                self.assertEqual(stopped.status_code, 409)
        self.assertFalse(application.runtime.running)
        self.assertTrue(cloud.http.is_closed)
        self.assertEqual(len(cloud._tasks), 0)

    async def test_no_private_json_released_after_reset_generation_changes(self) -> None:
        from brainoid_device.ui_bridge import GuardedResponse
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.types import Message

        await self.paired()
        sessions = Sessions()
        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/local/cloud/operations",
            "headers": [],
            "scheme": "http",
            "server": ("127.0.0.1", 8765),
            "query_string": b"",
        }
        request = Request(scope)
        response = GuardedResponse(
            JSONResponse({"text": "synthetic private buffer"}), self.cloud, sessions, request
        )
        self.local_runtime.fence_operations()
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b""}

        async def send(message: Message) -> None:
            sent.append(message)

        with self.assertRaises(asyncio.CancelledError):
            await response(scope, receive, send)
        self.assertNotIn("synthetic private buffer", repr(sent))

    async def test_fake_clock_three_missed_heartbeats_closes_connection(self) -> None:
        await self.paired()
        ticks = [0.0]
        sent: list[dict[str, Any]] = []
        fixture = self

        class Socket:
            subprotocol = "brainoid-agent.device.v1"
            first = True

            async def send(self, raw: str) -> None:
                sent.append(json.loads(raw))

            async def recv(self) -> str:
                if self.first:
                    self.first = False
                    return json.dumps(
                        {
                            "protocol_version": 1,
                            "message_id": str(uuid4()),
                            "type": "hello_ack",
                            "sent_at": fixture.clock.now().isoformat(),
                            "connection_generation": 1,
                            "payload": {
                                "heartbeat_interval_seconds": 15,
                                "lease_expires_at": fixture.clock.now().isoformat(),
                                "server_time": fixture.clock.now().isoformat(),
                                "snapshot_required": True,
                            },
                        }
                    )
                ticks[0] += 15
                raise TimeoutError

        class Connection:
            async def __aenter__(self) -> Socket:
                return Socket()

            async def __aexit__(self, *args: Any) -> None:
                pass

        device = DeviceTransport(self.cloud, monotonic=lambda: ticks[0])
        with patch("brainoid_device.device_transport.NoRedirect", return_value=Connection()):
            with self.assertRaises(CloudError):
                await device.connection(await self.cloud.token())
        self.assertEqual(ticks[0], 45)
        self.assertEqual([value["type"] for value in sent], ["hello", "heartbeat", "heartbeat"])
        self.assertIsNone(device.socket)


class CloudRecoveryTests(CloudFixture):
    async def test_same_second_identical_access_bytes_still_rotate_once(self) -> None:
        await self.paired()
        token = await self.cloud.token()
        version = self.cloud.journal.credential_version
        results = await asyncio.gather(
            *[self.cloud.token(rejected=token, rejected_version=version) for _ in range(8)]
        )
        self.assertTrue(all(value == token for value in results))
        self.assertEqual(self.cloud.journal.credential_version, version + 1)
        self.assertEqual(
            len([r for r in self.proxy.requests if r[0].endswith("device-tokens:refresh")]), 1
        )

    async def test_resource_denial_retains_pairing_but_masked_device_revocation_stops(self) -> None:
        await self.paired()
        with self.assertRaises(CloudError) as missing:
            await self.bridge.operation(
                GetRun(operation="get_run", run_id=ResourceId(str(uuid4())))
            )
        self.assertEqual(missing.exception.status, 404)
        self.assertTrue(self.cloud.active)
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"]["status"] = (
            "revoked"
        )
        with self.assertRaises(CloudError) as revoked:
            await self.bridge.operation(
                GetConversation(operation="get_conversation", conversation_id=self.conversation_id)
            )
        self.assertEqual(revoked.exception.code, "credential_refused")
        self.assertFalse(self.cloud.active)

    async def test_exchange_success_then_crash_reopens_exact_receipt(self) -> None:
        await self.cloud.begin_pairing("Synthetic exchange")
        pairing = self.cloud.journal.pairing
        assert pairing is not None
        self.confirm(pairing)  # type: ignore[arg-type]
        self.proxy.lose, self.proxy.release = ":exchange", asyncio.Event()
        task = asyncio.create_task(self.cloud.exchange())
        await self.proxy.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        first = self.proxy.requests[-1]
        await self.cloud.close()
        self.proxy.release = None
        self.cloud = self.new_cloud()
        self.assertTrue(await self.cloud.exchange())
        self.assertEqual(first, self.proxy.requests[-1])
        self.assertEqual(self.local_storage.snapshot().status, "ready")

    async def test_partial_sse_disconnect_resumes_on_new_api_instance(self) -> None:
        from brainoid_api.app import create_app
        from brainoid_api.auth.runtime import get_auth_services
        from brainoid_api.settings import load_settings
        from test_api_foundation import LOCAL_ENV

        await self.paired()
        accepted = await self.bridge.operation(
            CreateTurn(
                operation="create_turn",
                operation_id=ResourceId(str(uuid4())),
                conversation_id=self.conversation_id,
                turn=TurnCreate(text="Synthetic resume", output_mode="text"),
            )
        )
        run = ResourceId(accepted["run_id"])
        await asyncio.to_thread(self.execute, run)
        fresh = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        fresh.dependency_overrides[get_auth_services] = lambda: AuthServices(
            devices=self.repo, tenants=self.tenants, conversations=self.conversations
        )
        new_instance = httpx.ASGITransport(app=fresh)
        old_instance = self.proxy.inner
        cursors: list[str] = []

        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self) -> Any:
                for byte in first:
                    yield bytes([byte])
                raise httpx.ReadError("fixture instance stopped")

        first = b""

        async def route(request: httpx.Request) -> httpx.Response:
            nonlocal first
            if request.url.path.endswith("/events"):
                cursors.append(request.headers["last-event-id"])
                if len(cursors) == 1:
                    response = await old_instance.handle_async_request(request)
                    raw = await response.aread()
                    await response.aclose()
                    first = raw.split(b"\n\n", 1)[0] + b"\n\n"
                    return httpx.Response(
                        200, headers={"Content-Type": "text/event-stream"}, stream=Partial()
                    )
            return await new_instance.handle_async_request(request)

        await self.cloud.http.aclose()
        self.cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(route))
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        await self.bridge.events(run, None, queue)
        self.assertEqual(cursors, ["0", "1"])
        frames = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual(sum(b'"seq":1,' in (value or b"") for value in frames), 1)
        self.assertTrue(any(b"run.completed" in (value or b"") for value in frames))
        self.assertTrue(self.cloud.journal.cursors[0].terminal)
        await new_instance.aclose()
