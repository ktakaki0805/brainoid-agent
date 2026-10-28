"""Actual transport ownership of recoverable receipt races and unexpected failures."""

import asyncio
import copy
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from brainoid_api.device_gateway import DeviceGateway
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_notifications import DeviceNotifications
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_api.websocket_transport import DeviceWebSocketProtocol
from brainoid_contracts import DisplayPolicy, IanaTimezone, ResourceId, Visibility
from brainoid_device.cloud_models import CloudError
from brainoid_device.config import CloudSettings
from brainoid_device.device_transport import DeviceTransport
from brainoid_device.snapshot_sync import SnapshotSync
from brainoid_jobs.models import job_id
from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
from brainoid_runs.state import KIND, run_path
from brainoid_worker.notifications import DueReminders
from fakes.purge import FakePurgePages
from test_device_snapshot import SnapshotFixture


class SnapshotTransportTests(SnapshotFixture):
    async def until(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.02)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[None]:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        leases = DeviceLeases(self.store, self.repo, now=self.clock.now)
        notifications = DeviceNotifications(self.snapshots)
        gateway = DeviceGateway(leases, hooks=(notifications,))
        self.app.state.auth_services = replace(self.services, device_gateway=gateway)
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
            await self.until(lambda: server.started)
            await self.cloud.http.aclose()
            self.cloud.http = httpx.AsyncClient(trust_env=False)
            self.cloud.settings = CloudSettings(f"http://127.0.0.1:{port}", local_test=True)
            self.cloud.journal.api = self.cloud.settings.api_url
            self.cloud.save()
            yield
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            listener.close()

    async def receipt_race(self, *, private: bool = False, refusal: int = 404) -> None:
        async with self.serving():
            session = (
                self.sessions.start(
                    self.identity(),
                    self.tenant,
                    self.device_id,
                    PrivateSessionCreate(
                        allowed_scopes=["reminders"], acknowledge_shared_space=True
                    ),
                    "receipt-race-session",
                    "receipt-race-session-request",
                )
                if private
                else None
            )
            reminder = ReminderRequests(self.memories).create(
                self.identity(),
                self.tenant,
                ReminderCreate(
                    title="RECEIPT_RACE_SYNTHETIC",
                    due_at=self.clock.now() + timedelta(seconds=1),
                    timezone=IanaTimezone("UTC"),
                    visibility=Visibility.PRIVATE if private else Visibility.SHARED,
                    display_policy=DisplayPolicy.AUTHENTICATED_SESSION
                    if private
                    else DisplayPolicy.SHARED_DISPLAY,
                    delivery_target=DeliveryTarget(kind="device", id=self.device_id),
                ),
                "receipt-race-reminder",
            )
            self.clock.advance(1)
            DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(
                self.tenant
            )
            await self.sync.sync()
            notification = next(iter(self.sync.notifications))
            original = self.cloud.authorized

            async def lose(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                if path.endswith(":ack"):
                    raise CloudError()  # Receipt durable; ACK never commits.
                return await original(method, path, **kwargs)

            with patch.object(self.cloud, "authorized", side_effect=lose):
                with self.assertRaises(CloudError):
                    await self.sync.displayed(notification)
            denied, reconciled = asyncio.Event(), asyncio.Event()
            statuses = []
            source = f"tenants/{self.tenant}/reminders/{reminder.id}"

            async def revoke(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
                if path.endswith(":ack"):
                    if session:
                        self.sessions.end(
                            self.identity(),
                            self.tenant,
                            session.id,
                            "receipt-race-end",
                            '"1"',
                            "receipt-race-end-request",
                        )
                    else:
                        self.store.data[source]["deleted"] = True
                    status, value = await original(method, path, **kwargs)
                    self.assertEqual(status, 404)  # Real current source/session refusal.
                    statuses.append(status)
                    denied.set()
                    # Also exercise alternate expected item refusal codes after the
                    # same real current-authority denial; no success is fabricated.
                    return refusal, value
                return await original(method, path, **kwargs)

            synchronize = self.sync.sync

            async def observe() -> None:
                try:
                    await synchronize()
                except CloudError as error:
                    if error.code != "cloud_unavailable":
                        raise
                if denied.is_set():
                    reconciled.set()

            transport = DeviceTransport(self.cloud)
            running = None
            try:
                with (
                    patch.object(self.cloud, "authorized", side_effect=revoke),
                    patch.object(self.sync, "sync", side_effect=observe),
                ):
                    running = self.cloud.start(transport.run())
                    await asyncio.wait_for(denied.wait(), 8)
                    await asyncio.wait_for(reconciled.wait(), 8)
                    await asyncio.sleep(0)  # Run Runtime's completion callbacks, if any.
                    self.assertFalse(running.done())
                    self.assertFalse(self.local_runtime.resource_failed)
                    self.assertTrue(self.cloud.active)
                    self.assertIsNotNone(self.cloud.journal.credentials)
                    self.assertFalse(self.sync.notifications)
                    self.assertEqual([r.id for r in self.sync.load().displayed], [notification])
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                running = None
                self.assertEqual(statuses, [404])
                # Reopen the actual protected journal under a new sync owner. The
                # current source/session must authorize the retry again.
                if session:
                    self.sessions.start(
                        self.identity(),
                        self.tenant,
                        self.device_id,
                        PrivateSessionCreate(
                            allowed_scopes=["reminders"], acknowledge_shared_space=True
                        ),
                        "receipt-race-session-next",
                        "receipt-race-session-next-request",
                    )
                else:
                    self.store.data[source]["deleted"] = False
                self.sync = SnapshotSync(self.cloud)
                running = self.cloud.start(transport.run())
                await self.until(lambda: self.sync.ready and not self.sync.load().displayed)
                self.assertFalse(self.sync.notifications)
                self.assertFalse(self.local_runtime.resource_failed)
                self.assertTrue(self.cloud.active)
                self.assertIsNotNone(self.cloud.journal.credentials)
                delivery = next(
                    d
                    for p, d in self.store.data.items()
                    if "/deliveries/" in p and d.get("notification_id") == notification
                )
                self.assertIsNotNone(delivery["displayed_at"])
                self.assertIsNone(delivery["acknowledged_at"])
                self.assertGreaterEqual(transport.generation, 2)
            finally:
                if running:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)

    async def test_transport_receipt_source_denial_recovers_after_restart(self) -> None:
        await self.receipt_race()

    async def test_transport_receipt_session_denial_recovers_under_replacement(self) -> None:
        await self.receipt_race(private=True)

    async def test_transport_receipt_forbidden_is_item_local(self) -> None:
        await self.receipt_race(refusal=403)

    async def test_transport_receipt_gone_is_item_local(self) -> None:
        await self.receipt_race(refusal=410)

    async def test_transport_overflow_recovers_and_unexpected_failure_stays_visible(self) -> None:
        async with self.serving():
            live = self.assigned()
            source = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, live)}"
            for _ in range(128):
                run = ResourceId(str(uuid4()))
                job = copy.deepcopy(self.store.data[source])
                job.update(id=job_id(self.tenant, KIND, run), operation_id=run)
                self.store.data[f"tenants/{self.tenant}/jobs/{job['id']}"] = job
                record = copy.deepcopy(self.store.data[run_path(self.tenant, live)])
                record.update(run_id=run)
                self.store.data[run_path(self.tenant, run)] = record
            overflow = asyncio.Event()
            synchronize = self.sync.sync

            async def observe() -> None:
                try:
                    await synchronize()
                except CloudError as error:
                    if error.code == "cloud_unavailable":
                        overflow.set()
                    raise

            transport = DeviceTransport(self.cloud)
            running = None
            try:
                with patch.object(self.sync, "sync", side_effect=observe):
                    running = self.cloud.start(transport.run())
                    await asyncio.wait_for(overflow.wait(), 8)
                    self.assertTrue(self.cloud.active)
                    self.assertFalse(self.local_runtime.resource_failed)
                    for path, record in self.store.data.items():
                        if "/runs/" in path and record.get("run_id") != live:
                            record["created_at"] = (
                                self.clock.now() - timedelta(days=2)
                            ).isoformat()
                    self.sync.changed.set()
                    await self.until(lambda: self.sync.ready)
                    assert self.sync.state is not None
                    self.assertEqual([r.run_id for r in self.sync.state.assigned_runs], [live])
                    self.assertIsNotNone(self.cloud.journal.credentials)

                async def unexpected() -> None:
                    raise RuntimeError("SYNTHETIC_UNEXPECTED_SYNC_FAILURE")

                with patch.object(self.sync, "sync", side_effect=unexpected):
                    self.sync.changed.set()
                    await self.until(lambda: self.local_runtime.resource_failed)
                self.assertTrue(self.local_runtime.resource_failed)
            finally:
                if running:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
