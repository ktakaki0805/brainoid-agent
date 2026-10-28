"""DEV07/T017-019/T036: real cloud transactions, device runtime/SQLite and HA HTTP."""

import asyncio
import copy
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.errors import APIError
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.repositories.entities import Entities
from brainoid_api.repositories.memories import Memories
from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import frame
from brainoid_device.command_executor import CommandExecutor
from brainoid_device.device_transport import DeviceTransport
from brainoid_device.home_assistant import HomeAssistant, configure
from brainoid_device.ledger import DeviceStorage, LedgerConflict, StorageUnavailable
from brainoid_device.snapshot_sync import SnapshotSync
from brainoid_policy.scopes import PolicyDenied
from fakes.home import home_fixture
from test_device_cloud import CloudFixture
from test_home_assistant import SECRET, FakeHA


class AppliedHA(FakeHA):
    def __init__(self) -> None:
        super().__init__()
        self.states["light.fixture"] = {**self.states["light.first"], "entity_id": "light.fixture"}
        self.apply = True
        self.after_post: Any = None
        self.before_post: Any = None
        self.observation_loss = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            if self.apply:
                self.states["light.fixture"]["state"] = "on"
                self.states["light.fixture"]["attributes"]["brightness"] = 102
            result = await super().__call__(request)
            if self.after_post:
                self.after_post()
            return result
        if self.observation_loss and self.posts:
            raise httpx.ReadError("synthetic observation loss")
        result = await super().__call__(request)
        if self.before_post:
            self.before_post()
        return result

    @property
    def posts(self) -> int:
        return sum(method == "POST" for method, _, _ in self.calls)


class DirectWire:
    """Only the wire is synthetic; server handlers and grants/receipts are real."""

    def __init__(self, fixture: "ExecutionFixture") -> None:
        self.fixture = fixture
        self.sent: list[dict[str, Any]] = []
        self.lose = ""
        self.closed = False

    async def send(self, raw: str) -> None:
        value = frame(raw)
        self.sent.append(value)
        f = self.fixture
        kind, payload = value["type"], value["payload"]
        handler = f.service.start if kind == "command_start" else f.service.status
        result = f.leases.run(f.binding, lambda tx: handler(tx, f.binding, payload))
        result["request_message_id"] = value["message_id"]
        status = payload.get("status", "start")
        if self.lose == status:
            raise CloudError()
        reply = f.envelope(
            "command_start_result" if kind == "command_start" else "command_status_ack", result
        )
        f.executor.sample(reply)
        f.transport.response(reply)

    async def close(self) -> None:
        self.closed = True


class ExecutionFixture(CloudFixture):
    async def initialize(self, *, private: bool = False) -> None:
        await self.paired()
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        self.body, documents = home_fixture(
            self.tenant,
            self.identity().user_id,
            self.clock.now(),
            device=credentials.device_id,
            private=private,
            display="authenticated_session" if private else None,
        )
        device_path = f"tenants/{self.tenant}/devices/{credentials.device_id}"
        documents[device_path] = {**self.store.data[device_path], **documents[device_path]}
        self.store.data.update(documents)
        cursors = Cursors(b"e" * 32, now=self.clock.now)
        self.memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        entities = Entities(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, memories=self.memories, entities=entities
        )
        integration = next(doc["id"] for path, doc in documents.items() if "/integrations/" in path)
        configure(
            self.local_storage,
            self.private_store,
            {
                "origin": "http://127.0.0.1:8123",
                "token": SECRET,
                "integration_id": integration,
                "allowed_entity_ids": ["light.fixture"],
            },
        )
        self.fake_ha = AppliedHA()
        self.home = HomeAssistant(
            self.local_runtime,
            self.local_storage,
            self.private_store,
            cloud=self.cloud,
            transport=httpx.MockTransport(self.fake_ha),
        )
        self.addAsyncCleanup(self.home.close)
        self.leases = DeviceLeases(self.store, self.repo, now=self.clock.now)
        self.service = DeviceCommands(self.memories)
        self.commands = CommandRequests(self.memories)
        self.transport = DeviceTransport(self.cloud, monotonic=lambda: self.clock.now().timestamp())
        self.executor = CommandExecutor(
            self.transport, self.home, monotonic=lambda: self.clock.now().timestamp()
        )
        self.transport.commands = self.executor
        if private:
            from brainoid_api.services.private_sessions import PrivateSessionCreate

            self.private_session = self.sessions.start(
                self.identity(),
                self.tenant,
                credentials.device_id,
                PrivateSessionCreate(
                    allowed_scopes=["home.control"], acknowledge_shared_space=True
                ),
                "dev07-session-start",
                "dev07-session-start-request",
            )
            services = self.app.dependency_overrides[get_auth_services]()
            self.app.dependency_overrides[get_auth_services] = lambda: replace(
                services,
                private_sessions=self.sessions,
                device_snapshots=DeviceSnapshots(self.conversations, self.memories, self.sessions),
            )
            await SnapshotSync(self.cloud).sync()
        self.connect()

    def envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return frame(
            json.dumps(
                {
                    "protocol_version": 1,
                    "type": kind,
                    "message_id": str(uuid4()),
                    "connection_generation": self.binding.generation,
                    "sent_at": self.clock.now().isoformat(),
                    "payload": payload,
                }
            )
        )

    def connect(self) -> None:
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        self.binding = self.leases.acquire(
            self.repo.authenticate(credentials.access_token), "dev07-fixture"
        )
        self.transport.generation = self.binding.generation
        self.wire = DirectWire(self)
        self.transport.socket = cast(Any, self.wire)
        registration = self.local_storage.snapshot().registration
        assert registration is not None
        self.local_storage.set_generation(registration.epoch, self.binding.generation)
        lease = self.store.data[f"tenants/{self.tenant}/device_connections/{credentials.device_id}"]
        self.executor.begin(
            self.envelope(
                "hello_ack",
                {
                    "heartbeat_interval_seconds": 15,
                    "server_time": self.clock.now().isoformat(),
                    "lease_expires_at": lease["expires_at"].isoformat(),
                    "snapshot_required": True,
                },
            )
        )

    def command_delivery(self) -> Any:
        command = self.commands.create(self.identity(), self.tenant, self.body, str(uuid4()))
        payload = self.leases.run(
            self.binding, lambda tx: self.service.deliver(tx, self.binding, command.id)
        )
        assert payload is not None
        self.executor.receive(self.envelope("command", payload))
        return command

    def saved(self, identity: str) -> Any:
        return self.executor.journal.get(self.executor.epoch, identity)

    def reopen(self) -> None:
        self.local_storage.close()
        self.local_storage = DeviceStorage(
            self.root / "state",
            self.private_store,
            cleanup_private=self.local_runtime.clear_private,
        )
        self.cloud.storage = self.home.storage = self.local_storage
        self.executor = CommandExecutor(
            self.transport, self.home, monotonic=lambda: self.clock.now().timestamp()
        )
        self.transport.commands = self.executor
        self.connect()


class CommandExecutionTests(ExecutionFixture):
    async def test_success_duplicate_command_grant_and_new_generation_execute_once(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        await self.executor.execute(command.id)
        saved = self.saved(command.id)
        self.assertEqual(
            (saved.command.status, saved.command.result_acked, self.fake_ha.posts),
            ("succeeded", True, 1),
        )
        self.assertEqual(
            self.commands.get(self.identity(), self.tenant, command.id).status, "succeeded"
        )
        self.executor.receive(self.envelope("command", saved.delivery))
        await self.executor.execute(command.id)
        self.reopen()
        self.executor.receive(self.envelope("command", saved.delivery))
        await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.posts, 1)
        self.assertFalse(self.local_runtime.resource_failed)

    async def test_lost_terminal_ack_restart_replays_exact_payload_without_delivery(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        self.wire.lose = "succeeded"
        with self.assertRaises(CloudError):
            await self.executor.execute(command.id)
        report = self.saved(command.id).reports["succeeded"]
        self.assertFalse(self.saved(command.id).command.result_acked)
        self.assertIsNone(
            self.leases.run(
                self.binding, lambda tx: self.service.deliver(tx, self.binding, command.id)
            )
        )
        self.reopen()
        self.assertIn(command.id, self.executor.journal.pending(self.executor.epoch))
        await self.executor.execute(command.id)
        self.assertEqual(self.wire.sent[0]["payload"], report)
        self.assertTrue(self.saved(command.id).command.result_acked)
        self.assertEqual(self.fake_ha.posts, 1)

    async def test_lost_accept_or_grant_response_never_calls_ha_then_reconciles(self) -> None:
        await self.initialize()
        for lost in ("accepted", "start"):
            command = self.command_delivery()
            before = len(self.fake_ha.calls)
            self.wire.lose = lost
            with self.assertRaises(CloudError):
                await self.executor.execute(command.id)
            self.assertEqual(len(self.fake_ha.calls), before)
            execution = self.saved(command.id).command.execution_id
            self.reopen()
            await self.executor.execute(command.id)
            self.assertEqual(self.saved(command.id).command.execution_id, execution)
        self.assertEqual(self.fake_ha.posts, 2)

    async def test_crash_immediately_before_and_after_durable_start(self) -> None:
        await self.initialize()
        for committed in (False, True):
            command = self.command_delivery()
            original = self.local_storage.record_started

            def crash(
                *args: Any, commit: bool = committed, start: Any = original, **kwargs: Any
            ) -> bool:
                if commit:
                    start(*args, **kwargs)
                raise asyncio.CancelledError

            with patch.object(self.local_storage, "record_started", side_effect=crash):
                with self.assertRaises(asyncio.CancelledError):
                    await self.executor.execute(command.id)
            posts = self.fake_ha.posts
            self.reopen()
            await self.executor.execute(command.id)
            self.assertEqual(self.fake_ha.posts, posts + (0 if committed else 1))
            self.assertEqual(
                self.saved(command.id).command.status,
                "outcome_unknown" if committed else "succeeded",
            )

    async def test_post_or_observation_loss_never_becomes_success(self) -> None:
        await self.initialize()
        for phase in ("response_loss", "observation_loss", "mismatch"):
            command = self.command_delivery()
            self.fake_ha.fail = "response_loss" if phase == "response_loss" else ""
            self.fake_ha.observation_loss = phase == "observation_loss"
            self.fake_ha.apply = phase != "mismatch"
            self.fake_ha.states["light.fixture"]["state"] = "off"
            self.fake_ha.states["light.fixture"]["attributes"]["brightness"] = 0
            # observation_loss starts only after this command's POST.
            if phase == "observation_loss":
                self.fake_ha.calls.clear()
            await self.executor.execute(command.id)
            self.assertEqual(self.saved(command.id).command.status, "outcome_unknown")
            posts = self.fake_ha.posts
            self.reopen()
            await self.executor.execute(command.id)
            self.assertEqual(self.fake_ha.posts, posts)
            self.fake_ha.observation_loss = False

    async def test_clock_skew_report_uses_server_time_and_rollback_blocks_ha(self) -> None:
        await self.initialize()
        self.cloud.now = lambda: self.clock.now().timestamp() + 2
        self.connect()
        command = self.command_delivery()
        await self.executor.execute(command.id)
        self.assertEqual(self.saved(command.id).command.status, "succeeded")
        self.assertEqual(
            self.saved(command.id).reports["succeeded"]["observed_at"], self.clock.now().isoformat()
        )
        command = self.command_delivery()
        self.cloud.now = lambda: self.clock.now().timestamp() - 2
        before = len(self.fake_ha.calls)
        with self.assertRaises((CloudError, LedgerConflict)):
            await self.executor.execute(command.id)
        self.assertEqual(len(self.fake_ha.calls), before)

    async def test_expiry_cancel_and_wrong_tenant_target_prevent_provider_calls(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        self.commands.cancel(self.identity(), self.tenant, command.id, str(uuid4()), None)
        await self.executor.execute(command.id)
        command = self.command_delivery()
        self.clock.advance(31)
        self.connect()
        await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.calls, [])
        self.assertNotEqual(self.saved(command.id).command.status, "started")
        copied = copy.deepcopy(self.saved(command.id).delivery)
        copied["tenant_id"] = str(uuid4())
        with self.assertRaises(LedgerConflict):
            self.executor.receive(self.envelope("command", copied))
        copied = copy.deepcopy(self.saved(command.id).delivery)
        copied["target_entity_id"] = str(uuid4())
        with self.assertRaises(LedgerConflict):
            self.executor.receive(self.envelope("command", copied))

    async def test_cloud_allowlist_change_during_ha_preflight_blocks_post(self) -> None:
        await self.initialize()
        command = self.command_delivery()

        def revoke() -> None:
            self.store.data[f"tenants/{self.tenant}/devices/{self.body.device_id}"][
                "allowed_entity_ids"
            ] = []

        self.fake_ha.before_post = revoke
        # The real server refuses the second same-execution claim after HA GET.
        with self.assertRaises(PolicyDenied):
            await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.posts, 0)

    async def test_reset_or_old_generation_during_preflight_and_storage_fault(self) -> None:
        await self.initialize()
        for fault in ("generation", "storage"):
            command = self.command_delivery()
            if fault == "generation":
                self.fake_ha.before_post = lambda: setattr(
                    self.transport, "generation", self.transport.generation + 1
                )
                with self.assertRaises(CloudError):
                    await self.executor.execute(command.id)
                self.fake_ha.before_post = None
                self.connect()
            else:
                with patch.object(
                    self.local_storage, "record_started", side_effect=StorageUnavailable
                ):
                    with self.assertRaises(StorageUnavailable):
                        await self.executor.execute(command.id)
            self.assertEqual(self.fake_ha.posts, 0)

    async def test_reordered_shared_counter_sequences_are_durable_without_cursor_rewind(
        self,
    ) -> None:
        await self.initialize()
        first, second = self.command_delivery(), self.command_delivery()
        saved_first, saved_second = self.saved(first.id), self.saved(second.id)
        # New registration fixture with the same authorized deliveries, reversed.
        with self.local_storage._transaction() as db:
            db.execute("DELETE FROM commands")
            db.execute("UPDATE registration SET delivery_cursor=0")
        self.executor.receive(self.envelope("command", saved_second.delivery))
        self.executor.receive(self.envelope("command", saved_first.delivery))
        self.assertEqual(self.local_storage.snapshot().registration.delivery_cursor, 2)  # type: ignore[union-attr]
        await self.executor.execute(first.id)
        await self.executor.execute(second.id)
        self.assertEqual(self.fake_ha.posts, 2)

    async def test_control_target_needs_no_memory_read_and_rechecks_release_authority(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        prefix = f"/v1/tenants/{self.tenant}"
        status, _ = await self.cloud.authorized(
            "GET", f"{prefix}/entities/{command.target_entity_id}"
        )
        self.assertEqual(status, 404)
        status, target = await self.cloud.authorized(
            "GET", f"{prefix}/commands/{command.id}/target"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(target), {"command_id", "tenant_id", "device_id", "target_entity_id", "reference"}
        )
        self.assertEqual(target["reference"]["external_id"], "light.fixture")
        original = self.store.run

        def expire_after_commit(work: Any) -> Any:
            result = original(work)
            self.clock.advance(901)
            return result

        assert self.cloud.journal.credentials is not None
        device = self.repo.authenticate(self.cloud.journal.credentials.access_token)
        with (
            patch.object(self.store, "run", side_effect=expire_after_commit),
            self.assertRaises(APIError),
        ):
            self.commands.target(device, self.tenant, command.id)

    async def test_short_grant_and_untrusted_clock_never_enter_ha(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        path = f"tenants/{self.tenant}/device_connections/{self.body.device_id}"
        self.store.data[path]["expires_at"] = self.clock.now() + timedelta(seconds=3)
        with self.assertRaises(CloudError):
            await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.calls, [])
        self.cloud.now = lambda: self.clock.now().timestamp() + 60
        with self.assertRaises(CloudError):
            self.connect()
        self.assertEqual(self.fake_ha.calls, [])

    async def test_private_session_revoked_during_preflight_has_no_post(self) -> None:
        await self.initialize(private=True)
        command = self.command_delivery()

        def revoke() -> None:
            path = f"tenants/{self.tenant}/private_sessions/{self.private_session.id}"
            self.store.data[path]["status"] = "ended"

        self.fake_ha.before_post = revoke
        with self.assertRaises(PolicyDenied):
            await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.posts, 0)

    async def test_post_acceptance_crash_retains_uncertainty_and_never_reposts(self) -> None:
        await self.initialize()
        for durable_acceptance in (False, True):
            command = self.command_delivery()
            method = "finish" if durable_acceptance else "accepted"
            with patch.object(self.executor.journal, method, side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await self.executor.execute(command.id)
            posts = self.fake_ha.posts
            self.reopen()
            await self.executor.execute(command.id)
            self.assertEqual(self.fake_ha.posts, posts)
            self.assertEqual(
                self.saved(command.id).command.status,
                "succeeded" if durable_acceptance else "outcome_unknown",
            )

    async def test_owned_response_loss_keeps_runtime_healthy(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        self.wire.lose = "start"
        task = self.cloud.start(self.executor.run())
        await asyncio.wait_for(task, 3)
        self.assertFalse(self.local_runtime.resource_failed)
        self.assertTrue(self.cloud.active)
        self.assertEqual(self.fake_ha.calls, [])
        self.connect()
        task = self.cloud.start(self.executor.run())
        try:
            async with asyncio.timeout(3):
                while not self.saved(command.id).command.result_acked:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.local_runtime.resource_failed)
        self.assertEqual(self.fake_ha.posts, 1)

    async def test_cancelled_receipts_do_not_exhaust_pending_capacity_or_fake_acks(self) -> None:
        await self.initialize()
        with patch("brainoid_device.command_journal.MAX_PENDING", 2):
            cancelled = []
            for _ in range(4):
                command = self.command_delivery()
                self.commands.cancel(self.identity(), self.tenant, command.id, str(uuid4()), None)
                await self.executor.execute(command.id)
                value = self.saved(command.id)
                self.assertTrue(value.settled)
                self.assertFalse(value.command.result_acked)
                self.assertEqual(value.command.status, "accepted")
                cancelled.append(command.id)
            command = self.command_delivery()
            await self.executor.execute(command.id)
            self.assertEqual(self.fake_ha.posts, 1)
            self.assertEqual(self.executor.journal.pending(self.executor.epoch), [])
            self.executor.journal.prune(
                self.executor.epoch,
                now_ms=int(self.clock.now().timestamp() * 1000) + 8 * 86400 * 1000,
            )
            self.assertTrue(
                all(
                    self.local_storage.command(self.executor.epoch, identity) is None
                    for identity in cancelled
                )
            )

    async def test_expired_claim_without_local_start_reports_nonexecution(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        path = f"tenants/{self.tenant}/device_connections/{self.body.device_id}"
        self.store.data[path]["expires_at"] = self.clock.now() + timedelta(seconds=3)
        with self.assertRaises(CloudError):
            await self.executor.execute(command.id)
        self.assertEqual(self.saved(command.id).command.status, "accepted")
        self.clock.advance(4)
        self.connect()
        await self.executor.execute(command.id)
        value = self.saved(command.id)
        self.assertEqual(value.command.status, "failed")
        self.assertTrue(value.command.result_acked)
        self.assertEqual(self.fake_ha.calls, [])

    async def test_duplicate_grant_and_late_accepted_ack_do_not_match_new_requests(self) -> None:
        await self.initialize()
        command = self.command_delivery()
        original = self.transport.response
        first_grant: dict[str, Any] | None = None

        def duplicate_grant(reply: dict[str, Any]) -> None:
            nonlocal first_grant
            if reply["type"] == "command_start_result":
                if first_grant is not None:
                    original(first_grant)
                    return
                first_grant = copy.deepcopy(reply)
            original(reply)

        with patch.object(self.transport, "response", side_effect=duplicate_grant):
            await self.executor.execute(command.id)
        self.assertEqual(self.fake_ha.posts, 0)
        self.assertEqual(self.saved(command.id).command.status, "failed")
        self.connect()
        command = self.command_delivery()
        accepted: dict[str, Any] | None = None

        def late_accept(reply: dict[str, Any]) -> None:
            nonlocal accepted
            status = self.wire.sent[-1]["payload"].get("status")
            if status == "accepted":
                accepted = copy.deepcopy(reply)
            if status == "succeeded":
                assert accepted is not None
                original(accepted)
            else:
                original(reply)

        with (
            patch.object(self.transport, "response", side_effect=late_accept),
            self.assertRaises(CloudError),
        ):
            await self.executor.execute(command.id)
        self.assertFalse(self.saved(command.id).command.result_acked)
        self.connect()
        await self.executor.execute(command.id)
        self.assertTrue(self.saved(command.id).command.result_acked)
        self.assertEqual(self.fake_ha.posts, 1)
