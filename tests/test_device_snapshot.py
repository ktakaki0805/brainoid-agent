"""T021/T030/T035: canonical scoped recovery and actual installed client paging."""

import asyncio
import copy
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.auth.runtime import get_auth_services
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.errors import APIError
from brainoid_api.repositories.memories import Memories
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, IanaTimezone, ResourceId, Visibility
from brainoid_device.cloud_models import CloudError
from brainoid_device.snapshot_sync import SnapshotSync
from brainoid_runs.models import ConversationCreate, TurnCreate
from test_device_cloud import CloudFixture


class SnapshotFixture(CloudFixture):
    async def asyncSetUp(self) -> None:
        await self.paired()
        self.memories = Memories(
            self.store,
            self.tenants,
            self.conversations.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            allow_untracked_bootstrap=True,
            now=self.clock.now,
        )
        self.snapshots = DeviceSnapshots(self.conversations, self.memories, self.sessions)
        services = self.app.dependency_overrides[get_auth_services]()
        self.services = replace(
            services,
            memories=self.memories,
            private_sessions=self.sessions,
            device_snapshots=self.snapshots,
        )
        self.app.dependency_overrides[get_auth_services] = lambda: self.services
        self.sync = SnapshotSync(self.cloud)
        assert self.cloud.journal.credentials is not None
        self.device_id = self.cloud.journal.credentials.device_id
        self.device_identity = self.repo.authenticate(self.cloud.journal.credentials.access_token)

    def assigned(self, *, device: ResourceId | None = None) -> ResourceId:
        conversation = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=self.agent_id,
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
            ),
            str(uuid4()),
        ).id
        result = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation,
            TurnCreate(
                text="SYNTHETIC SNAPSHOT INPUT",
                output_mode="text",
                output_device_id=device or self.device_id,
            ),
            str(uuid4()),
        )
        # Complete the admission slot; this test concerns output metadata, not worker execution.
        self.store.run(
            lambda tx: self.conversations.usage.prepare_release(tx, self.tenant, result.run_id)()
        )
        return result.run_id


class SnapshotTests(SnapshotFixture):
    async def test_voice_wait_missing_or_invalid_header_has_no_server_boundary(self) -> None:
        original = self.cloud.http.send
        for header in (None, "invalid", "2026-10-29"):

            async def send(request: Any, header: str | None = header, **kwargs: Any) -> Any:
                response = await original(request, **kwargs)
                if request.url.path.endswith("/snapshot:validate"):
                    response.headers.pop("X-Brainoid-Observed-At", None)
                    if header is not None:
                        response.headers["X-Brainoid-Observed-At"] = header
                return response

            with patch.object(self.cloud.http, "send", side_effect=send):
                await self.sync.sync()
            self.assertIsNone(self.sync.observed_at)

    async def test_voice_wait_boundary_is_current_validation_server_time(self) -> None:
        self.assigned()
        await self.sync.sync()
        self.assertEqual(self.sync.observed_at, self.clock.now().timestamp())
        self.assertEqual(self.cloud.snapshot_observed_at, self.clock.now().timestamp())

    async def test_speech_assignment_retains_mode_without_snapshot_autoplay(self) -> None:
        run = self.assigned()
        self.store.data[f"tenants/{self.tenant}/runs/{run}"]["output_mode"] = "speech"
        await self.sync.sync()
        assert self.sync.state is not None
        self.assertEqual(self.sync.state.assigned_runs[0].output_mode, "speech")
        self.assertFalse((await self.sync.public())["play_speech"])

    async def test_all_pages_loss_resume_process_restart_and_no_body_cache(self) -> None:
        runs = {self.assigned() for _ in range(23)}
        original = self.cloud.authorized
        calls = 0

        async def lost(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise CloudError()
            return await original(method, path, **kwargs)

        with patch.object(self.cloud, "authorized", side_effect=lost):
            with self.assertRaises(CloudError):
                await self.sync.sync()
        self.assertFalse(self.sync.ready)
        assert self.sync.recovery is not None and self.sync.recovery.progress is not None
        self.assertIsNotNone(self.sync.recovery.progress.cursor)
        self.assertNotIn(
            b"SYNTHETIC SNAPSHOT INPUT", self.private_store.read("snapshot-recovery-v1")
        )
        resumed = SnapshotSync(self.cloud)
        await resumed.sync()
        assert resumed.state is not None
        self.assertEqual({run.run_id for run in resumed.state.assigned_runs}, runs)
        self.assertFalse((await resumed.public())["play_speech"])
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.assertEqual(registration.delivery_cursor, 0)

    async def test_private_session_scope_expiry_and_cursor_invalidation(self) -> None:
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["reminders"], acknowledge_shared_space=True),
            "snapshot-reminders",
            "snapshot-request",
        )
        await self.sync.sync()
        current = self.sync.session()
        assert current is not None and self.sync.state is not None
        self.assertEqual(current.id, session.id)
        self.assertEqual(self.sync.state.assigned_runs, [])
        self.assertEqual(
            self.sessions.get(self.identity(), self.tenant, session.id).idle_expires_at,
            session.idle_expires_at,
        )
        self.clock.advance(61)
        self.assertIsNone(self.sync.session())
        self.assertIsNone(self.sync.state)
        await self.sync.sync()
        self.assertTrue(self.sync.ready)
        self.assertIsNone(self.sync.session())

    async def test_scoped_signed_cursor_and_changed_assignment_restart(self) -> None:
        for _ in range(21):
            self.assigned()
        first = self.snapshots.page(self.device_identity).release()
        self.assertIsNotNone(first.next_cursor)
        self.assigned()
        self.snapshots.page(self.device_identity, first.next_cursor).release()
        # New arrivals do not invalidate a scan; the next complete cycle recovers them.
        with self.assertRaises(APIError):
            self.snapshots.page(self.device_identity, "malformed")
        assert self.cloud.journal.credentials is not None
        result = self.client.get(
            f"/v1/devices/{uuid4()}/snapshot",
            headers={"Authorization": "Bearer " + self.cloud.journal.credentials.access_token},
        )
        self.assertEqual(result.status_code, 404)
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.local_storage.set_generation(registration.epoch, 8)
        await self.sync.sync()
        assert self.sync.state is not None
        self.assertEqual(self.sync.state.connection_generation, 1)
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.assertEqual(registration.connection_generation, 8)

    async def test_expired_history_over_cap_does_not_block_current_and_wrong_output(self) -> None:
        from brainoid_jobs.models import job_id
        from brainoid_runs.state import KIND, run_path

        run = self.assigned()
        job_path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, run)}"
        source_job = self.store.data[job_path]
        source_run = self.store.data[run_path(self.tenant, run)]
        for _ in range(140):
            identity = ResourceId(str(uuid4()))
            job = copy.deepcopy(source_job)
            job["id"] = job_id(self.tenant, KIND, identity)
            job["operation_id"] = identity
            self.store.data[f"tenants/{self.tenant}/jobs/{job['id']}"] = job
            record = copy.deepcopy(source_run)
            record.update(
                run_id=identity, created_at=(self.clock.now() - timedelta(days=2)).isoformat()
            )
            self.store.data[run_path(self.tenant, identity)] = record
        await self.sync.sync()
        assert self.sync.state is not None
        self.assertEqual([r.run_id for r in self.sync.state.assigned_runs], [run])
        self.store.data[job_path]["output"]["device_id"] = str(uuid4())
        await self.sync.sync()
        self.assertEqual(self.sync.state.assigned_runs, [])

    async def test_private_run_revoked_session_and_late_operation_clear(self) -> None:
        from brainoid_device.ui_bridge import GetConversation

        session = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            "private-run-session",
            "private-run-request",
        )
        conversation = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=self.agent_id,
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
            ),
            str(uuid4()),
        )
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conversation.id,
            TurnCreate(
                text="PRIVATE_TEST_BODY", output_mode="text", output_device_id=self.device_id
            ),
            str(uuid4()),
        )
        await self.sync.sync()
        assert self.sync.state is not None
        self.assertEqual([r.run_id for r in self.sync.state.assigned_runs], [run.run_id])
        self.proxy.lose = str(conversation.id)
        self.proxy.release = asyncio.Event()
        task = asyncio.create_task(
            self.bridge.operation(
                GetConversation(
                    operation="get_conversation",
                    conversation_id=conversation.id,
                    private_session_id=session.id,
                )
            )
        )
        await asyncio.wait_for(self.proxy.entered.wait(), 2)
        self.sessions.end(
            self.identity(),
            self.tenant,
            session.id,
            "end-private-run-operation",
            '"1"',
            "end-request",
        )
        await self.sync.sync()
        self.proxy.release.set()
        with self.assertRaises(CloudError):
            await task
        self.assertIsNone(self.sync.session())
        assert self.sync.state is not None
        self.assertEqual(self.sync.state.assigned_runs, [])

    async def test_notification_duplicate_recovery_display_ack_and_expiry(self) -> None:
        from brainoid_api.repositories.reminders import ReminderRequests
        from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
        from brainoid_worker.notifications import DueReminders
        from fakes.purge import FakePurgePages

        ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="SYNTHETIC_NOTIFICATION",
                due_at=self.clock.now() + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
                delivery_target=DeliveryTarget(kind="device", id=self.device_id),
            ),
            "snapshot-notification",
        )
        self.clock.advance(1)
        DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(self.tenant)
        self.clock.advance(301)
        await self.sync.sync()
        self.assertFalse((await self.sync.public())["play_speech"])
        self.assertEqual(len(self.sync.notifications), 1)
        notification = next(iter(self.sync.notifications))
        await self.sync.sync()
        self.assertEqual(list(self.sync.notifications), [notification])
        reminder = self.sync.notifications[notification].reminder_id
        source = self.store.data[f"tenants/{self.tenant}/reminders/{reminder}"]
        source["deleted"] = True
        self.assertEqual((await self.sync.public())["notifications"], [])
        self.store.data[f"tenants/{self.tenant}/reminders/{reminder}"]["deleted"] = False
        await self.sync.sync()
        self.proxy.lose, self.proxy.losses = f"{notification}:ack", 1
        with self.assertRaises(CloudError):
            await self.sync.displayed(notification)
        self.assertFalse(self.sync.notifications)
        resumed = SnapshotSync(self.cloud)
        await resumed.sync()
        self.assertFalse(resumed.notifications)
        record = next(
            d
            for p, d in self.store.data.items()
            if "/deliveries/" in p and d.get("notification_id") == notification
        )
        self.assertIsNotNone(record["displayed_at"])
        self.assertIsNone(record["acknowledged_at"])
        self.assertNotIn(b"SYNTHETIC_NOTIFICATION", self.private_store.read("snapshot-recovery-v1"))

    async def socket_recovery(self, *, initial_revoke: bool = False) -> None:
        import socket

        import httpx
        import uvicorn
        from brainoid_api.device_gateway import Delivery, DeviceConnection, DeviceGateway
        from brainoid_api.device_leases import DeviceLeases
        from brainoid_api.device_notifications import DeviceNotifications
        from brainoid_api.websocket_transport import DeviceWebSocketProtocol
        from brainoid_device.config import CloudSettings
        from brainoid_device.device_transport import DeviceTransport

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
        original = DeviceConnection._transmit
        dropped = []

        async def transmit(connection: DeviceConnection, delivery: Delivery) -> None:
            if delivery.kind in {"session_started", "run_available"}:
                dropped.append(delivery.kind)
                return
            await original(connection, delivery)

        async def until(predicate: Any) -> None:
            async with asyncio.timeout(8):
                while not predicate():
                    await asyncio.sleep(0.02)

        connection = None
        reached, release = asyncio.Event(), asyncio.Event()
        authorized = self.cloud.authorized

        async def delayed(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            result = await authorized(method, path, **kwargs)
            if initial_revoke and path.endswith("snapshot:validate") and not reached.is_set():
                reached.set()
                await release.wait()
            return result

        try:
            await until(lambda: server.started)
            await self.cloud.http.aclose()
            self.cloud.http = httpx.AsyncClient(trust_env=False)
            self.cloud.settings = CloudSettings(f"http://127.0.0.1:{port}", local_test=True)
            self.cloud.journal.api = self.cloud.settings.api_url
            self.cloud.save()
            session = self.sessions.start(
                self.identity(),
                self.tenant,
                self.device_id,
                PrivateSessionCreate(
                    allowed_scopes=["conversation"], acknowledge_shared_space=True
                ),
                "socket-session-start",
                "socket-start",
            )
            run = self.assigned()
            transport = DeviceTransport(self.cloud)
            with (
                patch.object(DeviceConnection, "_transmit", transmit),
                patch.object(self.cloud, "authorized", side_effect=delayed),
            ):
                for generation in (1, 2):
                    connection = self.cloud.start(transport.connection(await self.cloud.token()))
                    if initial_revoke and generation == 1:
                        await asyncio.wait_for(reached.wait(), 8)
                        await until(lambda: bool(dropped))
                        self.assertIsNone(self.sync.state)
                        fence = self.cloud.output_generation
                        self.sessions.end(
                            self.identity(),
                            self.tenant,
                            session.id,
                            "socket-initial-end",
                            '"1"',
                            "socket-initial-end-request",
                        )
                        # Actual gateway -> socket -> DeviceTransport receive branch,
                        # while the authorized final HTTP response is still held.
                        await until(lambda fence=fence: self.cloud.output_generation > fence)
                        self.assertFalse(self.sync.ready)
                        self.assertIsNone(self.sync.state)
                        session = self.sessions.start(
                            self.identity(),
                            self.tenant,
                            self.device_id,
                            PrivateSessionCreate(
                                allowed_scopes=["conversation"], acknowledge_shared_space=True
                            ),
                            "socket-next-session",
                            "socket-next-session-request",
                        )
                        run = self.assigned()
                        release.set()
                    await until(
                        lambda generation=generation: (
                            transport.generation == generation and self.sync.ready
                        )
                    )
                    await until(lambda: bool(dropped))
                    assert self.sync.state is not None
                    self.assertEqual([r.run_id for r in self.sync.state.assigned_runs], [run])
                    self.assertEqual(self.sync.state.connection_generation, 1)
                    if generation == 1:
                        connection.cancel()
                        await asyncio.gather(connection, return_exceptions=True)
                self.sessions.end(
                    self.identity(),
                    self.tenant,
                    session.id,
                    "socket-session-end",
                    '"1"',
                    "socket-end",
                )
                await until(
                    lambda: (
                        self.sync.state is not None
                        and self.sync.state.active_private_session is None
                    )
                )
                self.assertTrue(self.cloud.active)
                assert self.sync.state is not None
                self.assertEqual(self.sync.state.assigned_runs, [])
        finally:
            release.set()
            if connection is not None:
                connection.cancel()
                await asyncio.gather(connection, return_exceptions=True)
            server.should_exit = True
            async with asyncio.timeout(10):
                await serving
            listener.close()

    async def test_actual_http_wss_dropped_hints_reconnect_and_scoped_revoke(self) -> None:
        await self.socket_recovery()

    async def test_actual_transport_revoke_fences_initial_held_validation_response(self) -> None:
        await self.socket_recovery(initial_revoke=True)

    async def test_mixed_command_notification_cursor_never_accepts_commands(self) -> None:
        from brainoid_api.device_commands import DeviceCommands
        from brainoid_api.device_leases import DeviceLeases
        from brainoid_api.device_notifications import DeviceNotifications
        from brainoid_api.repositories.commands import CommandRequests
        from brainoid_api.repositories.reminders import ReminderRequests
        from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
        from brainoid_worker.notifications import DueReminders
        from fakes.home import home_fixture
        from fakes.purge import FakePurgePages

        body, documents = home_fixture(
            self.tenant, self.identity().user_id, self.clock.now(), device=self.device_id
        )
        documents.pop(f"tenants/{self.tenant}/devices/{self.device_id}")
        self.store.data.update(documents)
        self.store.data[f"tenants/{self.tenant}/devices/{self.device_id}"]["allowed_entity_ids"] = [
            body.target_entity_id
        ]
        self.store.data[f"tenants/{self.tenant}/devices/{self.device_id}"]["allowed_scopes"].append(
            "home.control"
        )
        commands = CommandRequests(self.memories)
        first = commands.create(self.identity(), self.tenant, body, "snapshot-command-first")
        second = commands.create(self.identity(), self.tenant, body, "snapshot-command-second")
        ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="MIXED_SYNTHETIC_NOTICE",
                due_at=self.clock.now() + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
                delivery_target=DeliveryTarget(kind="device", id=self.device_id),
            ),
            "snapshot-mixed-notification",
        )
        self.clock.advance(1)
        DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(self.tenant)
        leases = DeviceLeases(self.store, self.repo, now=self.clock.now)
        binding = leases.acquire(self.device_identity, "snapshot-mixed-connection")
        service = DeviceCommands(self.memories)
        notifications = DeviceNotifications(self.snapshots)
        one = leases.run(binding, lambda tx: service.deliver(tx, binding, first.id))
        current = self.snapshots.page(self.device_identity).release()
        notification = current.pending_notification_ids[0]
        two = leases.run(binding, lambda tx: notifications.notification(tx, binding, notification))
        three = leases.run(binding, lambda tx: service.deliver(tx, binding, second.id))
        assert one is not None and two is not None and three is not None
        self.assertEqual(
            [one["delivery_seq"], two["delivery_seq"], three["delivery_seq"]], [1, 2, 3]
        )
        await self.sync.sync()
        assert self.sync.state is not None
        self.assertEqual(set(self.sync.state.pending_command_ids), {first.id, second.id})
        self.assertEqual(self.sync.state.delivery_cursor, 3)
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.assertEqual(registration.delivery_cursor, 0)
        self.assertEqual(
            leases.run(binding, lambda tx: service.deliver(tx, binding, first.id)), one
        )

    async def test_wire_contract_bounds_and_current_original_source(self) -> None:
        import json
        from pathlib import Path

        import yaml  # type: ignore[import-untyped]
        from brainoid_jobs.models import job_id
        from brainoid_runs.state import KIND
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

        run = self.assigned()
        page = self.snapshots.page(self.device_identity).release().model_dump(mode="json")
        schema = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        Draft202012Validator({"$ref": "#/components/schemas/DeviceSnapshot", **schema}).validate(
            page
        )
        page["assigned_runs"] *= 20
        page["next_cursor"] = "x" * 2048
        self.assertLess(len(json.dumps(page, ensure_ascii=True).encode()), 65536)
        # Saved device assignment cannot outlive the original conversation source.
        path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, run)}"
        conversation = self.store.data[path]["grant"]["resource"]["ref"]["id"]
        self.store.data[f"tenants/{self.tenant}/conversations/{conversation}"]["deleted"] = True
        current = self.snapshots.page(self.device_identity).release()
        self.assertFalse(current.assigned_runs)

    async def late_snapshot(self, stage: str) -> None:
        from brainoid_device.cloud_wire import decode

        for _ in range(21):
            self.assigned()
        old = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["conversation"], acknowledge_shared_space=True),
            "late-session-first",
            "late-session-request",
        )
        await self.sync.sync()
        original = self.cloud.authorized
        reached, release = asyncio.Event(), asyncio.Event()

        async def delayed(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            result = await original(method, path, **kwargs)
            match = (
                (stage == "first" and path.endswith("/snapshot"))
                or (stage == "continuation" and "/snapshot?" in path)
                or (stage == "validation" and path.endswith("snapshot:validate"))
            )
            if match and not reached.is_set():
                reached.set()
                await release.wait()
            return result

        with patch.object(self.cloud, "authorized", side_effect=delayed):
            task = asyncio.create_task(self.sync.sync())
            try:
                await asyncio.wait_for(reached.wait(), 3)
                self.sessions.end(
                    self.identity(), self.tenant, old.id, "late-end-session", '"1"', "late-end"
                )
                self.sync.session_hint("session_revoked", old.id)
                new = self.sessions.start(
                    self.identity(),
                    self.tenant,
                    self.device_id,
                    PrivateSessionCreate(
                        allowed_scopes=["reminders"], acknowledge_shared_space=True
                    ),
                    "late-session-next",
                    "late-next-request",
                )
                release.set()
                with self.assertRaises(CloudError) as failure:
                    await task
                self.assertEqual(failure.exception.code, "cloud_unavailable")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.sync.ready)
        self.assertIsNone(self.sync.state)
        self.assertIsNone(decode(self.private_store.read("snapshot-recovery-v1"))["progress"])
        restarted = SnapshotSync(self.cloud)
        await restarted.sync()
        self.assertTrue(restarted.ready)
        self.assertEqual(restarted.session().id, new.id)  # type: ignore[union-attr]
        self.assertTrue(self.cloud.active)

    async def test_first_response_after_revoke_and_replacement_is_discarded(self) -> None:
        await self.late_snapshot("first")

    async def test_continuation_after_revoke_and_replacement_discards_durable_prefix(self) -> None:
        await self.late_snapshot("continuation")

    async def test_validation_after_revoke_and_replacement_is_discarded(self) -> None:
        await self.late_snapshot("validation")

    async def test_late_notification_body_after_revoke_never_reinstalls(self) -> None:
        from brainoid_api.repositories.reminders import ReminderRequests
        from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
        from brainoid_worker.notifications import DueReminders
        from fakes.purge import FakePurgePages

        session = self.sessions.start(
            self.identity(),
            self.tenant,
            self.device_id,
            PrivateSessionCreate(allowed_scopes=["reminders"], acknowledge_shared_space=True),
            "notification-race-session",
            "notification-race-request",
        )
        ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="PRIVATE_LATE_BODY",
                due_at=self.clock.now() + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
                delivery_target=DeliveryTarget(kind="device", id=self.device_id),
            ),
            "notification-race-reminder",
        )
        self.clock.advance(1)
        DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(self.tenant)
        original = self.cloud.authorized
        reached, release = asyncio.Event(), asyncio.Event()

        async def delayed(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            result = await original(method, path, **kwargs)
            if method == "GET" and "/notifications/" in path and result[0] == 200:
                reached.set()
                await release.wait()
            return result

        with patch.object(self.cloud, "authorized", side_effect=delayed):
            task = asyncio.create_task(self.sync.sync())
            try:
                await asyncio.wait_for(reached.wait(), 3)
                self.sessions.end(
                    self.identity(),
                    self.tenant,
                    session.id,
                    "notification-race-end",
                    '"1"',
                    "notification-end",
                )
                self.sync.clear()
                release.set()
                with self.assertRaises(CloudError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.sync.notifications)
        self.assertFalse(self.sync.ready)
        self.assertNotIn(b"PRIVATE_LATE_BODY", self.private_store.read("snapshot-recovery-v1"))
        restarted = SnapshotSync(self.cloud)
        await restarted.sync()
        self.assertFalse(restarted.notifications)
        self.assertIsNone(restarted.session())

    async def test_bounded_history_pages_validation_and_exact_hint_reads(self) -> None:
        from brainoid_api.device_leases import ConnectionBinding
        from brainoid_api.device_notifications import DeviceNotifications
        from brainoid_contracts.device_snapshot import SnapshotSelection
        from brainoid_jobs.models import job_id
        from brainoid_runs.state import KIND, run_path
        from test_turn_run import StrictTransaction

        live = self.assigned()
        live_job = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, live)}"
        # Choose older IDs preceding the live job, so current work is beyond >16 pages.
        for _ in range(340):
            while True:
                run = ResourceId(str(uuid4()))
                key = job_id(self.tenant, KIND, run)
                if key < self.store.data[live_job]["id"]:
                    break
            job = copy.deepcopy(self.store.data[live_job])
            job.update(id=key, operation_id=run)
            self.store.data[f"tenants/{self.tenant}/jobs/{key}"] = job
            record = copy.deepcopy(self.store.data[run_path(self.tenant, live)])
            record.update(run_id=run, created_at=(self.clock.now() - timedelta(days=2)).isoformat())
            self.store.data[run_path(self.tenant, run)] = record
        original_page, original_get = StrictTransaction.query_page, StrictTransaction.get
        reads: list[str] = []
        materialized: list[int] = []

        def page(
            tx: Any, path: str, filters: dict[str, str], *, after: str | None, limit: int
        ) -> Any:
            self.assertLessEqual(limit, 21)
            rows = original_page(tx, path, filters, after=after, limit=limit)
            materialized.append(len(rows))
            return rows

        def get(tx: Any, path: str) -> Any:
            reads.append(path)
            return original_get(tx, path)

        with (
            patch.object(StrictTransaction, "query_page", page),
            patch.object(StrictTransaction, "get", get),
            patch.object(StrictTransaction, "query", side_effect=AssertionError("unbounded query")),
        ):
            cursor = None
            found = []
            while True:
                reads.clear()
                materialized.clear()
                result = self.snapshots.page(self.device_identity, cursor).release()
                self.assertLessEqual(sum(materialized), 23)
                self.assertLessEqual(sum("/runs/" in path for path in reads), 20)
                # Includes current principal, session and every source read, not just candidates.
                self.assertLess(len(reads), 100)
                found.extend(result.assigned_runs)
                cursor = result.next_cursor
                if not cursor:
                    break
            self.assertEqual([r.run_id for r in found], [live])
            selection = SnapshotSelection(
                snapshot_id=result.snapshot_id, run_ids=[live], command_ids=[], notification_ids=[]
            )
            reads.clear()
            materialized.clear()
            validated = self.snapshots.validate(self.device_identity, selection).release()
            self.assertEqual([r.run_id for r in validated.assigned_runs], [live])
            self.assertFalse(materialized)
            self.assertEqual(sum("/jobs/" in path for path in reads), 1)
            self.assertEqual(sum("/runs/" in path for path in reads), 1)
            self.assertLess(len(reads), 100)
            reads.clear()
            hint = DeviceNotifications(self.snapshots).hint("run_available", live)
            value = self.store.run(
                lambda tx: hint.load(tx, ConnectionBinding(self.device_identity, "bounded-hint", 1))
            )
            assert value is not None
            self.assertEqual(value["run_id"], live)
            self.assertFalse(materialized)
            self.assertEqual(sum("/runs/" in path for path in reads), 1)
            self.assertLess(len(reads), 100)
        await self.sync.sync()
        self.assertFalse(self.sync.ready)
        self.assertTrue(self.cloud.active)
        restarted = SnapshotSync(self.cloud)
        await restarted.sync()
        self.assertEqual([r.run_id for r in restarted.state.assigned_runs], [live])  # type: ignore[union-attr]

    async def test_final_validation_removes_revoked_prefix_and_checks_combined_budget(self) -> None:
        from brainoid_contracts.device_snapshot import SnapshotSelection
        from brainoid_jobs.models import job_id
        from brainoid_runs.state import KIND
        from pydantic import ValidationError

        runs = {self.assigned() for _ in range(23)}
        original = self.cloud.authorized
        revoked = None

        async def revoke_prefix(
            method: str, path: str, **kwargs: Any
        ) -> tuple[int, dict[str, Any]]:
            nonlocal revoked
            result = await original(method, path, **kwargs)
            if path.endswith("/snapshot") and revoked is None:
                revoked = result[1]["assigned_runs"][0]["run_id"]
                job = self.store.data[
                    f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, ResourceId(revoked))}"
                ]
                conversation = job["grant"]["resource"]["ref"]["id"]
                self.store.data[f"tenants/{self.tenant}/conversations/{conversation}"][
                    "deleted"
                ] = True
            return result

        with patch.object(self.cloud, "authorized", side_effect=revoke_prefix):
            await self.sync.sync()
        self.assertEqual({r.run_id for r in self.sync.state.assigned_runs}, runs - {revoked})  # type: ignore[union-attr]
        with self.assertRaises(ValidationError):
            SnapshotSelection(
                snapshot_id="0" * 64,
                run_ids=[ResourceId(str(uuid4())) for _ in range(100)],
                command_ids=[ResourceId(str(uuid4())) for _ in range(29)],
                notification_ids=[],
            )
        with self.assertRaises(ValidationError):
            SnapshotSelection(
                snapshot_id="0" * 64,
                run_ids=cast(Any, ["../../other"]),
                command_ids=[],
                notification_ids=[],
            )

    async def test_live_overflow_is_retryable_and_recovers_without_losing_credentials(self) -> None:
        from brainoid_jobs.models import job_id
        from brainoid_runs.state import KIND, run_path

        live = self.assigned()
        job_path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, live)}"
        for _ in range(128):
            run = ResourceId(str(uuid4()))
            job = copy.deepcopy(self.store.data[job_path])
            job.update(id=job_id(self.tenant, KIND, run), operation_id=run)
            self.store.data[f"tenants/{self.tenant}/jobs/{job['id']}"] = job
            record = copy.deepcopy(self.store.data[run_path(self.tenant, live)])
            record.update(run_id=run)
            self.store.data[run_path(self.tenant, run)] = record
        with self.assertRaises(CloudError) as failure:
            await self.sync.sync()
        self.assertEqual(failure.exception.code, "cloud_unavailable")
        self.assertTrue(self.cloud.active)
        self.assertIsNotNone(self.cloud.journal.credentials)
        self.assertFalse(self.sync.ready)
        for path, record in self.store.data.items():
            if "/runs/" in path and record.get("run_id") != live:
                record["created_at"] = (self.clock.now() - timedelta(days=2)).isoformat()
        await self.sync.sync()
        self.assertTrue(self.sync.ready)
        self.assertEqual([r.run_id for r in self.sync.state.assigned_runs], [live])  # type: ignore[union-attr]

    async def test_unconfirmed_display_survives_audio_deadline_source_revoke_and_restart(
        self,
    ) -> None:
        from brainoid_api.repositories.reminders import ReminderRequests
        from brainoid_memory.reminders import DeliveryTarget, ReminderCreate
        from brainoid_worker.notifications import DueReminders
        from fakes.purge import FakePurgePages

        reminder = ReminderRequests(self.memories).create(
            self.identity(),
            self.tenant,
            ReminderCreate(
                title="DISPLAY_UNCONFIRMED",
                due_at=self.clock.now() + timedelta(seconds=1),
                timezone=IanaTimezone("UTC"),
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
                delivery_target=DeliveryTarget(kind="device", id=self.device_id),
            ),
            "unconfirmed-display-reminder",
        )
        self.clock.advance(1)
        DueReminders(self.memories.jobs, FakePurgePages(cast(Any, self.store))).scan(self.tenant)
        await self.sync.sync()
        notification = next(iter(self.sync.notifications))
        original = self.cloud.authorized

        async def no_ack(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            if path.endswith(":ack"):
                raise CloudError()  # Loss before commit, so canonical notification remains pending.
            return await original(method, path, **kwargs)

        with patch.object(self.cloud, "authorized", side_effect=no_ack):
            with self.assertRaises(CloudError):
                await self.sync.displayed(notification)
        self.clock.advance(301)
        source_path = f"tenants/{self.tenant}/reminders/{reminder.id}"
        self.store.data[source_path]["deleted"] = True
        restarted = SnapshotSync(self.cloud)
        await restarted.sync()
        self.assertFalse(restarted.notifications)
        self.assertEqual([r.id for r in restarted.load().displayed], [notification])
        self.store.data[source_path]["deleted"] = False
        await restarted.sync()
        self.assertFalse(restarted.notifications)
        self.assertEqual(restarted.load().displayed, [])
        self.assertFalse((await restarted.public())["play_speech"])
