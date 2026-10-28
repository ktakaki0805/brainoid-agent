"""Real DeviceApplication, loopback WSS gateway and HA adapter execution lifecycle."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

import httpx
import uvicorn
from brainoid_api.auth.runtime import get_auth_services
from brainoid_api.device_command_watch import command_gateway
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.websocket_transport import DeviceWebSocketProtocol
from brainoid_device.app import DeviceApplication
from brainoid_device.config import CloudSettings, Settings
from test_command_executor import ExecutionFixture
from test_device_commands import FakeWatchClient
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve


class CommandTransportTests(ExecutionFixture):
    async def test_owned_actual_send_close_replays_saved_receipt_and_result(self) -> None:
        await self.initialize()

        async def close_peer(socket: ServerConnection) -> None:
            await socket.close(code=1001)

        for phase in ("accepted", "succeeded"):
            with self.subTest(phase=phase):
                self.connect()
                command = self.command_delivery()
                receipt = self.saved(command.id)
                before_posts = self.fake_ha.posts
                original_report = self.executor.report
                async with serve(close_peer, "127.0.0.1", 0) as server:
                    port = server.sockets[0].getsockname()[1]
                    async with connect(f"ws://127.0.0.1:{port}") as closed:
                        await closed.wait_closed()

                        async def send_after_close(
                            value: Any,
                            desired: str = phase,
                            report: Callable[[Any], Awaitable[bool]] = original_report,
                        ) -> bool:
                            if value.command.status == desired:
                                # Real ClientConnection.send raises ConnectionClosedOK;
                                # no synthetic CloudError stands in for the I/O failure.
                                self.transport.socket = closed
                            return await report(value)

                        with patch.object(self.executor, "report", new=send_after_close):
                            task = self.cloud.start(self.executor.run())
                            await asyncio.wait_for(task, 3)
                        await asyncio.sleep(0)
                pending = self.saved(command.id)
                self.assertEqual(pending.command.status, phase)
                self.assertEqual(pending.command.execution_id, receipt.command.execution_id)
                self.assertFalse(pending.command.result_acked)
                self.assertEqual(self.fake_ha.posts - before_posts, int(phase == "succeeded"))
                self.assertFalse(self.local_runtime.resource_failed)
                self.assertTrue(self.cloud.active)
                generation = self.transport.generation
                self.connect()
                self.assertGreater(self.transport.generation, generation)
                task = self.cloud.start(self.executor.run())
                try:
                    async with asyncio.timeout(3):
                        while not self.saved(command.id).command.result_acked:
                            await asyncio.sleep(0.01)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                saved = self.saved(command.id)
                self.assertEqual(saved.command.execution_id, receipt.command.execution_id)
                self.assertEqual(saved.reports[phase], pending.reports[phase])
                self.assertEqual(saved.command.status, "succeeded")
                self.assertEqual(self.fake_ha.posts - before_posts, 1)
                self.assertFalse(self.local_runtime.resource_failed)

    async def test_request_io_recovery_preserves_cancellation_and_unexpected_failure(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        with patch.object(self.wire, "send", side_effect=OSError("synthetic I/O failure")):
            await self.cloud.start(self.executor.run())
        self.assertFalse(self.local_runtime.resource_failed)
        self.assertEqual(self.saved(command.id).command.status, "accepted")
        self.connect()
        with patch.object(self.wire, "send", side_effect=asyncio.CancelledError):
            task = self.cloud.start(self.executor.run())
            result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertFalse(self.local_runtime.resource_failed)
        self.connect()
        with patch.object(self.wire, "send", side_effect=RuntimeError("unexpected defect")):
            task = self.cloud.start(self.executor.run())
            result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], RuntimeError)
        self.assertTrue(self.local_runtime.resource_failed)
        self.assertEqual(self.fake_ha.posts, 0)

    async def test_application_wss_delivery_result_and_lost_ack_reconnect(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        watch = FakeWatchClient(lambda: self.store.data)
        gateway = command_gateway(self.leases, cast(Any, watch), self.service)
        services = replace(
            self.app.dependency_overrides[get_auth_services](),
            device_gateway=gateway,
            private_sessions=self.sessions,
            device_snapshots=DeviceSnapshots(self.conversations, self.memories, self.sessions),
        )
        self.app.dependency_overrides[get_auth_services] = lambda: services
        self.app.state.auth_services = services
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        settings = CloudSettings(f"http://127.0.0.1:{port}", local_test=True)
        self.cloud.settings = settings
        self.cloud.journal.api = settings.api_url
        self.cloud.save()
        await self.cloud.close()
        await self.home.close()
        await self.local_runtime.close()
        self.local_storage.close()
        self.private_store.close()
        (self.root / "assets/index.html").write_text("<!doctype html><title>DEV07 fixture</title>")
        application = DeviceApplication(
            Settings(
                "127.0.0.1",
                18881,
                self.root / "assets",
                self.root / "credentials",
                self.root / "state",
                settings,
            )
        )
        assert application.cloud is not None and application.home_assistant is not None
        assert application.transport is not None and application.command_executor is not None
        application.cloud.now = lambda: self.clock.now().timestamp()
        application.home_assistant.transport = httpx.MockTransport(self.fake_ha)
        server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                lifespan="off",
                log_config=None,
                access_log=False,
                ws=DeviceWebSocketProtocol,
                ws_per_message_deflate=False,
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with application.app.app.router.lifespan_context(application.app.app):  # type: ignore[attr-defined]
                executor = application.command_executor
                original = executor.report
                lost = False

                async def lose_ack(value: Any) -> bool:
                    nonlocal lost
                    # Real status/ACK exchange commits remotely; inject the local
                    # crash boundary immediately before the durable ACK write.
                    if value.command.status == "succeeded" and not lost:
                        lost = True
                        assert application.transport is not None
                        socket = application.transport.socket
                        assert socket is not None
                        from unittest.mock import patch

                        from brainoid_device.cloud_models import CloudError

                        with patch.object(executor.journal, "acknowledge", side_effect=CloudError):
                            return await original(value)
                    return await original(value)

                executor.report = lose_ack  # type: ignore[method-assign]
                async with asyncio.timeout(10):
                    while True:
                        binding = application.storage.snapshot().registration
                        if binding:
                            saved = application.storage.command(binding.epoch, command.id)
                            if saved and saved.result_acked:
                                break
                        await asyncio.sleep(0.02)
                self.assertTrue(lost)
                self.assertGreaterEqual(application.transport.generation, 3)
                self.assertEqual(self.fake_ha.posts, 1)
                self.assertEqual(saved.status, "succeeded")
                self.assertFalse(application.runtime.resource_failed)
                self.assertTrue(application.cloud.active)
            self.assertEqual(len(application.runtime._tasks), 0)
            self.assertEqual(len(application.cloud._tasks), 0)
            self.assertIsNone(application.transport.socket)
        finally:
            application.close()
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            listener.close()

    async def test_old_cloud_without_echo_negotiation_keeps_transport_but_disables_execution(
        self,
    ) -> None:
        from unittest.mock import patch

        from brainoid_device.cloud_wire import SUBPROTOCOL, encode

        await self.initialize()
        command = self.command_delivery()
        fixture = self

        class LegacySocket:
            subprotocol = SUBPROTOCOL
            count = 0
            sent: list[dict[str, Any]] = []

            async def send(self, raw: str) -> None:
                import json

                self.sent.append(json.loads(raw))

            async def recv(self) -> str:
                self.count += 1
                if self.count == 1:
                    assert fixture.cloud.journal.credentials is not None
                    fixture.binding = fixture.leases.acquire(
                        fixture.repo.authenticate(fixture.cloud.journal.credentials.access_token),
                        "legacy-cloud",
                    )
                    lease = fixture.store.data[
                        f"tenants/{fixture.tenant}/device_connections/{fixture.body.device_id}"
                    ]
                    return encode(
                        fixture.envelope(
                            "hello_ack",
                            {
                                "heartbeat_interval_seconds": 15,
                                "lease_expires_at": lease["expires_at"].isoformat(),
                                "server_time": fixture.clock.now().isoformat(),
                                "snapshot_required": True,
                            },
                        )
                    ).decode()
                if self.count == 2:
                    fixture.clock.advance(16)
                    return encode(
                        fixture.envelope("command", fixture.saved(command.id).delivery)
                    ).decode()
                if self.count == 3:
                    lease = fixture.store.data[
                        f"tenants/{fixture.tenant}/device_connections/{fixture.body.device_id}"
                    ]
                    return encode(
                        fixture.envelope(
                            "heartbeat_ack",
                            {
                                "lease_expires_at": lease["expires_at"].isoformat(),
                                "server_time": fixture.clock.now().isoformat(),
                            },
                        )
                    ).decode()
                await asyncio.Event().wait()
                raise AssertionError

            async def close(self) -> None:
                pass

        wire = LegacySocket()

        class Connection:
            async def __aenter__(self) -> LegacySocket:
                return wire

            async def __aexit__(self, *args: Any) -> None:
                pass

        with patch("brainoid_device.device_transport.NoRedirect", return_value=Connection()):
            task = self.cloud.start(self.transport.connection(await self.cloud.token()))
            try:
                async with asyncio.timeout(3):
                    while wire.count < 4:
                        await asyncio.sleep(0.01)
                self.assertTrue(self.transport.accepted)
                self.assertFalse(self.transport.command_active)
                self.assertTrue(self.cloud.active)
                self.assertEqual([v["type"] for v in wire.sent], ["hello", "heartbeat"])
                self.assertEqual(self.fake_ha.calls, [])
                self.assertFalse(self.local_runtime.resource_failed)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
