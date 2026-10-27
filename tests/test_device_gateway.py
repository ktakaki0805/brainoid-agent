"""T-004/023/024: dedicated WSS admission, durable generations and owned cleanup."""

import asyncio
import copy
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import anyio
import uvicorn
from brainoid_api.auth.runtime import AuthServices
from brainoid_api.device_gateway import Delivery, DeviceConnection, DeviceGateway
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_protocol import MAX_FRAME_BYTES, SCHEMA, SUBPROTOCOL, ProtocolError, decode
from brainoid_api.errors import APIError
from brainoid_api.websocket_transport import DeviceWebSocketProtocol
from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_policy.control_ledger import Change, DeviceTarget, LedgerError
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect
from test_common_mutations import MemoryTransaction
from test_device_pairing import DevicePairingFixture
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.typing import Subprotocol


class GatewayFixture(DevicePairingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.creds = self.credentials()
        self.device_identity = self.repo.authenticate(self.creds.access_token)
        self.leases = DeviceLeases(self.store, self.repo, now=self.clock.now)
        self.gateway = DeviceGateway(self.leases)
        self.app = cast(FastAPI, self.client.app)
        self.app.state.auth_services = AuthServices(devices=self.repo, device_gateway=self.gateway)
        self.url = f"/v1/devices/{self.creds.device_id}/connect"
        self.headers = {"authorization": "Bearer " + self.creds.access_token}

    def frame(self, kind: str = "hello", generation: int = 1) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "protocol_version": 1,
            "message_id": str(uuid4()),
            "type": kind,
            "sent_at": self.clock.now().isoformat(),
            "payload": {"last_received_seq": 0},
        }
        if kind == "hello":
            frame["payload"].update(
                device_id=self.creds.device_id,
                software_version="test",
                supported_protocol_versions=[1],
                capabilities=[],
            )
        else:
            frame["connection_generation"] = generation
        return frame

    def connect(self, **kwargs: Any) -> WebSocketTestSession:
        return self.client.websocket_connect(
            self.url, headers=dict(self.headers), subprotocols=[Subprotocol(SUBPROTOCOL)], **kwargs
        )

    def hello(self, ws: WebSocketTestSession) -> dict[str, Any]:
        ws.send_json(self.frame())
        ack = ws.receive_json()
        self.assertEqual(ack["type"], "hello_ack")
        self.assertEqual(ack["payload"]["heartbeat_interval_seconds"], 15)
        self.assertIs(ack["payload"]["snapshot_required"], True)
        decode(json.dumps(ack))
        return cast(dict[str, Any], ack)


class DeviceLeaseTests(GatewayFixture):
    def test_reconnect_new_repository_keeps_credentials_and_refresh_authority(self) -> None:
        old = self.leases.acquire(self.device_identity, "first")
        fresh_repo = DeviceLeases(self.store, self.repo, now=self.clock.now)
        new = fresh_repo.acquire(self.device_identity, "second")
        self.assertEqual((old.generation, new.generation), (1, 2))
        self.assertEqual(new.identity.connection_generation, 1)
        with self.assertRaises(APIError):
            self.leases.run(old, lambda tx: tx.set("test/effect", {"started": True}))
        self.assertNotIn("test/effect", self.store.data)
        self.leases.release(self.device_identity, "first")
        fresh_repo.check(new)
        self.assertEqual(self.repo.authenticate(self.creds.access_token), self.device_identity)
        refreshed = self.refresh(self.refresh_body(self.creds))
        self.assertEqual(self.repo.authenticate(refreshed.access_token).connection_generation, 1)
        fresh_repo.release(self.device_identity, "second")
        third = fresh_repo.acquire(self.device_identity, "third")
        self.assertEqual(third.generation, 3)

    def test_heartbeat_lease_boundary_no_resurrection_and_no_other_frame_renewal(self) -> None:
        binding = self.leases.acquire(self.device_identity, "one")
        self.clock.advance(44)
        expires = self.leases.heartbeat(binding)
        self.assertEqual(expires, self.clock.now() + timedelta(seconds=45))
        self.clock.advance(45)
        for work in (self.leases.check, self.leases.heartbeat):
            with self.assertRaises(APIError):
                work(binding)
        self.assertEqual(self.leases.acquire(self.device_identity, "two").generation, 2)

    def test_expiry_during_transaction_effect_or_response_is_denied(self) -> None:
        binding = self.leases.acquire(self.device_identity, "one")

        def delayed(tx: Transaction) -> None:
            self.clock.advance(45)
            tx.set("test/effect", {"started": True})

        with self.assertRaises(APIError):
            self.leases.run(binding, delayed)
        self.assertNotIn("test/effect", self.store.data)

    def test_lease_cannot_extend_original_access_token(self) -> None:
        binding = self.leases.acquire(self.device_identity, "one")
        for _ in range(59):
            self.clock.advance(15)
            self.leases.heartbeat(binding)
        self.clock.advance(15)
        with self.assertRaises(APIError):
            self.leases.heartbeat(binding)
        with self.assertRaises(APIError):
            self.leases.acquire(self.device_identity, "two")

    def test_pending_ledger_device_deny_is_effective_before_firestore_mutation(self) -> None:
        binding = self.leases.acquire(self.device_identity, "one")
        change = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            target=DeviceTarget(device_id=self.creds.device_id, status="revoked"),
            expected_version=1,
            authorization_version=2,
            effect="deny",
        )
        self.ledger.prepare(change)
        with self.assertRaises(APIError):
            self.leases.check(binding)


class DeviceGatewayTests(GatewayFixture):
    def test_handshake_heartbeat_and_disconnect_cas(self) -> None:
        with self.connect() as ws:
            ack = self.hello(ws)
            self.assertEqual(ws.accepted_subprotocol, SUBPROTOCOL)
            ws.send_json(self.frame("heartbeat", ack["connection_generation"]))
            response = ws.receive_json()
            self.assertEqual(response["type"], "heartbeat_ack")
            decode(json.dumps(response))
        self.assertIs(self.store.data[self.leases.path(self.device_identity)]["online"], False)

    def test_latest_connection_only_and_old_disconnect_preserves_new(self) -> None:
        with self.connect() as new:
            with self.connect() as old:
                self.hello(old)
                ack = self.hello(new)
                self.assertEqual(ack["connection_generation"], 2)
                old.send_json(self.frame("heartbeat", 1))
                with self.assertRaises(WebSocketDisconnect):
                    old.receive_json()
            new.send_json(self.frame("heartbeat", 2))
            self.assertEqual(new.receive_json()["connection_generation"], 2)

    def test_handshake_rejects_path_query_cookie_protocol_and_credentials(self) -> None:
        cases: list[dict[str, Any]] = [
            {"url": self.url + "?token=synthetic-secret"},
            {"url": self.url.replace(str(self.creds.device_id), str(uuid4()))},
            {"headers": {}},
            {"headers": {"authorization": "Bearer user-token-synthetic"}},
            {"headers": {**self.headers, "cookie": "token=synthetic-secret"}},
            {"subprotocols": []},
            {"subprotocols": [SUBPROTOCOL, "secret"]},
        ]
        for overrides in cases:
            with self.subTest(overrides=tuple(overrides)):
                args: dict[str, Any] = {
                    "url": self.url,
                    "headers": dict(self.headers),
                    "subprotocols": [SUBPROTOCOL],
                }
                args.update(overrides)
                with self.assertRaises(WebSocketDisconnect):
                    with self.client.websocket_connect(**args):
                        pass
        self.assertNotIn(self.leases.path(self.device_identity), self.store.data)

    def test_bad_hello_and_inbound_direction_fail_without_lease(self) -> None:
        variants = [self.frame("heartbeat"), {**self.frame(), "connection_generation": 1}]
        wrong = self.frame()
        wrong["payload"]["device_id"] = str(uuid4())
        variants.append(wrong)
        for frame in variants:
            with self.subTest(frame_type=frame["type"]), self.connect() as ws:
                ws.send_json(frame)
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
        self.assertNotIn(self.leases.path(self.device_identity), self.store.data)
        for frame in (self.frame(), {**self.frame("heartbeat"), "type": "unknown"}):
            with self.connect() as ws:
                self.hello(ws)
                ws.send_json(frame)
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()

    def test_hello_timeout(self) -> None:
        with patch("brainoid_api.device_gateway.HELLO_TIMEOUT", 0.01), self.connect() as ws:
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()

    def test_binary_and_oversize_frames(self) -> None:
        for content in (b"{}", "x" * (MAX_FRAME_BYTES + 1)):
            with self.connect() as ws:
                if isinstance(content, bytes):
                    ws.send_bytes(content)
                else:
                    ws.send_text(content)
                with self.assertRaises(WebSocketDisconnect) as error:
                    ws.receive_json()
                self.assertEqual(error.exception.code, 1003 if isinstance(content, bytes) else 1009)

    def test_packaged_schema_and_hostile_json(self) -> None:
        self.assertEqual(SCHEMA, json.loads(Path("specs/device-protocol.schema.json").read_text()))
        valid = json.dumps(self.frame())
        bad = [
            "[]",
            "null",
            '{"type":"hello","type":"heartbeat"}',
            valid.replace('"protocol_version": 1', '"protocol_version": true'),
            valid.replace('"last_received_seq": 0', '"last_received_seq": NaN'),
            valid.replace(str(self.creds.device_id), "not-a-uuid"),
            valid.replace(self.clock.now().isoformat(), "not-a-time"),
            "[" * 2000 + "]" * 2000,
            "\ud800",
        ]
        for text in bad:
            with self.subTest(text=text[:30]), self.assertRaises(ProtocolError):
                decode(text)
        self.assertEqual(decode(valid)["type"], "hello")

    def test_monitor_offline_three_misses_and_hooks_close(self) -> None:
        opened: list[DeviceConnection] = []
        closed: list[bool] = []

        @asynccontextmanager
        async def hook(connection: DeviceConnection) -> AsyncIterator[None]:
            opened.append(connection)
            try:
                yield
            finally:
                closed.append(True)

        async def sleep(seconds: float) -> None:
            self.clock.advance(seconds)
            await asyncio.sleep(0)

        self.gateway.hooks = (hook,)
        self.gateway.sleep = sleep
        with self.connect() as ws:
            self.hello(ws)
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.assertEqual(self.clock.monotonic(), 45)
        self.assertTrue(closed)
        self.assertEqual(opened[0].binding.device_id, self.creds.device_id)
        self.assertTrue(opened[0].closed)

    def test_revocation_family_tenant_and_ledger_unavailable_stop_monitor(self) -> None:
        originals = copy.deepcopy(self.store.data)
        for failure in ("family", "tenant", "device", "ledger"):
            self.store.data = copy.deepcopy(originals)

            async def sleep(seconds: float, failure: str = failure) -> None:
                self.clock.advance(seconds)
                if failure == "ledger":
                    self.enterContext(
                        patch.object(self.ledger, "latest", side_effect=LedgerError())
                    )
                    return
                if failure == "family":
                    path = "device_credential_families/" + self.device_identity.family_id
                    self.store.data[path]["status"] = "revoked"
                elif failure == "device":
                    path = f"tenants/{self.tenant}/devices/{self.creds.device_id}"
                    self.store.data[path]["status"] = "revoked"
                else:
                    self.store.data[f"tenants/{self.tenant}"]["status"] = "deleted"
                await asyncio.sleep(0)

            self.gateway.sleep = sleep
            with self.subTest(failure=failure), self.connect() as ws:
                self.hello(ws)
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()

    def test_delivery_hook_is_scoped_and_backpressure_cleans_up(self) -> None:
        closed: list[bool] = []

        @asynccontextmanager
        async def hook(connection: DeviceConnection) -> AsyncIterator[None]:
            self.assertEqual(
                connection.binding.device_path,
                f"tenants/{self.tenant}/devices/{self.creds.device_id}",
            )
            try:
                for _ in range(17):
                    connection.enqueue(
                        Delivery("error", lambda tx, binding: {"code": "test", "retryable": False})
                    )
                yield
            finally:
                closed.append(True)

        self.gateway.hooks = (hook,)
        with self.connect() as ws:
            self.hello(ws)
            with self.assertRaises(WebSocketDisconnect):
                while True:
                    ws.receive_json()
        self.assertEqual(closed, [True])

    def test_delivery_output_validated_and_watch_errors_do_not_log_secrets(self) -> None:
        @asynccontextmanager
        async def hook(connection: DeviceConnection) -> AsyncIterator[None]:
            connection.enqueue(
                Delivery("error", lambda tx, binding: {"code": "ok", "retryable": False})
            )
            yield

        self.gateway.hooks = (hook,)
        with self.connect() as ws:
            self.hello(ws)
            self.assertEqual(ws.receive_json()["type"], "error")

        @asynccontextmanager
        async def broken(connection: DeviceConnection) -> AsyncIterator[None]:
            raise RuntimeError("synthetic-frame-token-secret")
            yield

        self.gateway.hooks = (broken,)
        with self.assertLogs("brainoid_api", logging.INFO) as logs, self.connect() as ws:
            self.hello(ws)
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.assertNotIn("synthetic-frame-token-secret", " ".join(logs.output))

    def test_real_protocol_frames_and_server_logs_are_secret_free(self) -> None:
        async def exercise() -> None:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    self.app,
                    lifespan="off",
                    log_config=None,
                    access_log=False,
                    ws=DeviceWebSocketProtocol,
                    ws_max_size=MAX_FRAME_BYTES,
                    ws_per_message_deflate=False,
                )
            )
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(server.started)
                url = f"ws://127.0.0.1:{port}{self.url}"
                with self.assertLogs(level=logging.INFO) as logs:
                    with self.assertRaises(InvalidStatus):
                        async with connect(
                            url + "?token=synthetic-query-secret",
                            additional_headers=self.headers,
                            subprotocols=[Subprotocol(SUBPROTOCOL)],
                        ):
                            pass
                    with self.assertRaises(InvalidStatus):
                        async with connect(
                            url,
                            additional_headers=[
                                ("authorization", self.headers["authorization"]),
                                ("authorization", "Bearer synthetic-duplicate-secret"),
                            ],
                            subprotocols=[Subprotocol(SUBPROTOCOL)],
                        ):
                            pass
                    for content in (
                        None,
                        b"synthetic-frame-secret",
                        "z" * (MAX_FRAME_BYTES + 1),
                        b"\xff",
                        ["z" * 40000, "z" * 40000],
                    ):
                        async with connect(
                            url,
                            additional_headers=self.headers,
                            subprotocols=[Subprotocol(SUBPROTOCOL)],
                        ) as ws:
                            if content is None:
                                await ws.send(json.dumps(self.frame()))
                                self.assertEqual(json.loads(await ws.recv())["type"], "hello_ack")
                            else:
                                if content == b"\xff":
                                    await ws.send(content, text=True)
                                else:
                                    await ws.send(content)
                                with self.assertRaises(ConnectionClosed):
                                    await ws.recv()
                combined = " ".join(logs.output)
                for secret in (
                    self.creds.access_token,
                    "synthetic-query-secret",
                    "synthetic-frame-secret",
                    "synthetic-duplicate-secret",
                ):
                    self.assertNotIn(secret, combined)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 10)
                listener.close()

        asyncio.run(exercise())


class DeviceGatewayCancellationTests(GatewayFixture):
    def test_timeout_drains_late_exception_without_asyncio_secret_log(self) -> None:
        from threading import Event

        from starlette.websockets import WebSocket

        async def exercise() -> None:
            binding = self.leases.acquire(self.device_identity, "test")
            connection = DeviceConnection(self.gateway, cast(WebSocket, object()), binding)
            unblock = Event()
            messages: list[dict[str, Any]] = []
            loop = asyncio.get_running_loop()
            prior = loop.get_exception_handler()
            loop.set_exception_handler(lambda loop, context: messages.append(context))

            def delayed() -> None:
                unblock.wait(timeout=5)
                raise RuntimeError("synthetic-delayed-secret")

            try:
                with patch("brainoid_api.device_gateway.IO_TIMEOUT", 0.01):
                    with self.assertRaises(TimeoutError):
                        await connection.io(delayed)
                self.assertEqual(len(connection._pending), 1)
                unblock.set()
                await asyncio.sleep(0.01)
                await connection.drain()
                self.assertFalse(messages)
            finally:
                unblock.set()
                loop.set_exception_handler(prior)

        asyncio.run(exercise())

    def test_delayed_thread_handoff_rechecks_expiry_before_output(self) -> None:
        from starlette.websockets import WebSocket

        async def exercise() -> None:
            binding = self.leases.acquire(self.device_identity, "test")
            sent: list[Any] = []

            async def send(message: Any) -> None:
                sent.append(message)

            async def receive() -> Any:
                return {"type": "websocket.connect"}

            ws = WebSocket({"type": "websocket"}, receive, send)
            await ws.accept()
            connection = DeviceConnection(self.gateway, ws, binding)
            original = connection._io

            async def delayed(call: Any) -> Any:
                result = await original(call)
                self.clock.advance(45)
                return result

            with patch.object(connection, "_io", delayed), self.assertRaises(APIError):
                await connection.send("error", {"code": "test", "retryable": False})
            self.assertEqual([item["type"] for item in sent], ["websocket.accept"])
            await connection.drain()

        asyncio.run(exercise())

    def test_cancellation_closes_hooks_and_lease_even_when_finalizer_raises(self) -> None:
        from starlette.websockets import WebSocket

        async def exercise() -> None:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            queue.put_nowait({"type": "websocket.connect"})
            queue.put_nowait({"type": "websocket.receive", "text": json.dumps(self.frame())})
            sent: list[Any] = []
            opened = asyncio.Event()
            closed: list[bool] = []

            async def send(message: Any) -> None:
                sent.append(message)

            @asynccontextmanager
            async def hook(connection: DeviceConnection) -> AsyncIterator[None]:
                opened.set()
                try:
                    yield
                finally:
                    closed.append(True)
                    raise RuntimeError("synthetic-finalizer-secret")

            self.gateway.hooks = (hook,)
            ws = WebSocket(
                {
                    "type": "websocket",
                    "query_string": b"",
                    "headers": [
                        (b"authorization", self.headers["authorization"].encode()),
                        (b"sec-websocket-protocol", SUBPROTOCOL.encode()),
                    ],
                },
                queue.get,
                send,
            )
            task = asyncio.create_task(self.gateway.serve(ws, self.creds.device_id))
            await asyncio.wait_for(opened.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(closed, [True])
            self.assertEqual(sent[-1]["type"], "websocket.close")
            self.assertFalse(self.store.data[self.leases.path(self.device_identity)]["online"])

        with self.assertLogs("brainoid_api", logging.INFO) as logs:
            asyncio.run(exercise())
        self.assertNotIn("synthetic-finalizer-secret", " ".join(logs.output))

    def test_lost_acquire_response_relinquishes_only_own_durable_lease(self) -> None:
        original = self.leases.acquire

        def failed(
            identity: Any, connection_id: str, *, admit: Callable[[], None] | None = None
        ) -> Any:
            original(identity, connection_id, admit=admit)
            raise RuntimeError("synthetic-storage-secret")

        with patch.object(self.leases, "acquire", failed), self.connect() as ws:
            ws.send_json(self.frame())
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        lease = self.store.data[self.leases.path(self.device_identity)]
        self.assertEqual(lease["generation"], 1)
        self.assertFalse(lease["online"])


class DeviceGatewayAdmissionTests(GatewayFixture):
    def test_queued_acquire_timeout_and_cancel_preserve_healthy_new_connection(self) -> None:
        from starlette.websockets import WebSocket

        async def exercise(stop: str) -> None:
            queue: asyncio.Queue[Any] = asyncio.Queue()
            queue.put_nowait({"type": "websocket.connect"})
            accepted, closed = asyncio.Event(), asyncio.Event()
            started, unblock = Event(), Event()
            limiter = anyio.to_thread.current_default_thread_limiter()
            prior_tokens = limiter.total_tokens
            limiter.total_tokens = 1

            def block() -> None:
                started.set()
                unblock.wait(timeout=5)

            async def send(message: Any) -> None:
                if message["type"] == "websocket.accept":
                    accepted.set()
                elif message["type"] == "websocket.close":
                    closed.set()

            ws = WebSocket(
                {
                    "type": "websocket",
                    "query_string": b"",
                    "headers": [
                        (b"authorization", self.headers["authorization"].encode()),
                        (b"sec-websocket-protocol", SUBPROTOCOL.encode()),
                    ],
                },
                queue.get,
                send,
            )
            serve: asyncio.Task[Any] | None = None
            blocker: asyncio.Task[Any] | None = None
            try:
                with patch(
                    "brainoid_api.device_gateway.IO_TIMEOUT", 0.05 if stop == "timeout" else 5.0
                ):
                    serve = asyncio.create_task(self.gateway.serve(ws, self.creds.device_id))
                    await asyncio.wait_for(accepted.wait(), 2)
                    blocker = asyncio.create_task(run_in_threadpool(block))
                    for _ in range(1000):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(started.is_set())
                    queue.put_nowait(
                        {"type": "websocket.receive", "text": json.dumps(self.frame())}
                    )
                    for _ in range(1000):
                        if limiter.statistics().tasks_waiting:
                            break
                        await asyncio.sleep(0.001)
                    self.assertEqual(limiter.statistics().tasks_waiting, 1)
                    if stop == "cancel":
                        serve.cancel()
                    elif stop == "disconnect":
                        queue.put_nowait({"type": "websocket.disconnect", "code": 1000})
                    await asyncio.wait_for(closed.wait(), 2)
                    # Queued acquisition has not entered a transaction yet.
                    self.assertNotIn(self.leases.path(self.device_identity), self.store.data)
                    fresh = DeviceLeases(self.store, self.repo, now=self.clock.now)
                    healthy = fresh.acquire(self.device_identity, "healthy-other-instance")
                    fresh.check(healthy)
                    unblock.set()
                    await asyncio.wait_for(blocker, 2)
                    if stop == "cancel":
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(serve, 2)
                    else:
                        await asyncio.wait_for(serve, 2)
                    fresh.check(healthy)
                    lease = self.store.data[self.leases.path(self.device_identity)]
                    self.assertEqual(lease["generation"], healthy.generation)
                    self.assertEqual(lease["connection_id"], "healthy-other-instance")
                    self.assertTrue(lease["online"])
            finally:
                unblock.set()
                for task in (blocker, serve):
                    if task is not None:
                        await asyncio.gather(task, return_exceptions=True)
                limiter.total_tokens = prior_tokens

        initial = copy.deepcopy(self.store.data)
        for stop in ("timeout", "cancel", "disconnect"):
            self.store.data = copy.deepcopy(initial)
            with self.subTest(stop=stop):
                asyncio.run(exercise(stop))

    def test_acquire_delay_or_retry_crossing_deadline_cannot_replace_healthy(self) -> None:
        original = self.store.run
        initial = copy.deepcopy(self.store.data)
        for retry in (False, True):
            self.store.data = copy.deepcopy(initial)
            healthy = self.leases.acquire(self.device_identity, "healthy")
            deadline = self.clock.monotonic() + 1

            def admit(deadline: float = deadline) -> None:
                if self.clock.monotonic() >= deadline:
                    raise TimeoutError()

            def delayed(work: Callable[[Transaction], Any], retry: bool = retry) -> Any:
                if retry:
                    # An aborted attempt's buffered write is discarded.
                    work(MemoryTransaction(self.store.data))
                    self.clock.advance(1)
                    return original(work)
                transaction = MemoryTransaction(self.store.data)
                get = transaction.get

                def read(path: str) -> Any:
                    value = get(path)
                    if path == self.leases.path(self.device_identity):
                        self.clock.advance(1)
                    return value

                with patch.object(transaction, "get", read):
                    return work(transaction)

            with self.subTest(retry=retry), patch.object(self.store, "run", delayed):
                with self.assertRaises(TimeoutError):
                    self.leases.acquire(self.device_identity, "expired", admit=admit)
            self.leases.check(healthy)
            self.assertEqual(
                self.store.data[self.leases.path(self.device_identity)]["generation"], 1
            )

    def test_queued_connection_work_is_not_admitted_after_close(self) -> None:
        from starlette.websockets import WebSocket

        async def exercise() -> None:
            binding = self.leases.acquire(self.device_identity, "current")
            connection = DeviceConnection(self.gateway, cast(WebSocket, object()), binding)
            limiter = anyio.to_thread.current_default_thread_limiter()
            prior_tokens = limiter.total_tokens
            limiter.total_tokens = 1
            started, unblock = Event(), Event()
            invoked: list[bool] = []

            def block() -> None:
                started.set()
                unblock.wait(timeout=5)

            def effect(tx: Transaction, binding: Any) -> None:
                invoked.append(True)
                tx.set("test/effect", {"started": True})

            blocker = asyncio.create_task(run_in_threadpool(block))
            work: asyncio.Task[Any] | None = None
            try:
                for _ in range(1000):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue(started.is_set())
                work = asyncio.create_task(connection.run(effect))
                for _ in range(1000):
                    if limiter.statistics().tasks_waiting:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(limiter.statistics().tasks_waiting, 1)
                connection.closed = True
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work
                unblock.set()
                await blocker
                await connection.drain()
                self.assertEqual(invoked, [])
                self.assertNotIn("test/effect", self.store.data)
                self.leases.check(binding)
            finally:
                unblock.set()
                await asyncio.gather(
                    blocker, *([work] if work is not None else []), return_exceptions=True
                )
                await connection.drain()
                limiter.total_tokens = prior_tokens

        asyncio.run(exercise())

    def test_connection_callback_does_not_start_after_policy_read_crosses_deadline(self) -> None:
        binding = self.leases.acquire(self.device_identity, "current")
        deadline = self.clock.monotonic() + 1
        current = self.leases.current
        invoked: list[bool] = []

        def admit() -> None:
            if self.clock.monotonic() >= deadline:
                raise TimeoutError()

        def delayed(tx: Transaction, target: Any) -> Any:
            result = current(tx, target)
            self.clock.advance(1)
            return result

        with patch.object(self.leases, "current", delayed), self.assertRaises(TimeoutError):
            self.leases.run(binding, lambda tx: invoked.append(True), admit=admit)
        self.assertEqual(invoked, [])
        self.leases.check(binding)
