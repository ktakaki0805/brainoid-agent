"""DEV-05: canonical command races, grant/ACK replay and the real WSS route."""

import copy
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.auth.runtime import AuthServices
from brainoid_api.device_command_watch import command_gateway
from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_protocol import SUBPROTOCOL
from brainoid_api.errors import APIError
from brainoid_api.repositories.commands import CommandRequests
from brainoid_contracts import ResourceId
from brainoid_memory.commands import command_path
from brainoid_policy.scopes import PolicyDenied
from fakes.home import home_fixture
from fakes.memory_repository import TENANT
from starlette.websockets import WebSocketDisconnect
from test_notes import NotesFixture


class FakeWatchClient:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.path = ""
        self.device = ""
        self.callback: Any = None
        self.is_active = True
        self.closed = False
        self.ready = Event()

    def collection(self, path: str) -> Any:
        self.path = path
        return self

    def where(self, *, filter: Any) -> Any:
        assert filter.field_path == "device_id" and filter.op_string == "=="
        self.device = filter.value
        return self

    def on_snapshot(self, callback: Any) -> Any:
        self.callback = callback
        self.emit()
        self.ready.set()
        return self

    def emit(self) -> None:
        documents = [
            SimpleNamespace(id=path.rsplit("/", 1)[1], to_dict=lambda doc=doc: copy.deepcopy(doc))
            for path, doc in self.data().items()
            if path.rsplit("/", 1)[0] == self.path and doc["device_id"] == self.device
        ]
        self.callback(list(reversed(documents)), [], None)

    def unsubscribe(self) -> None:
        self.closed = True
        self.is_active = False


class DeviceCommandFixture(NotesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.command_body, documents = home_fixture(TENANT, self.identity().user_id, self.now)
        self.store.data.update(documents)
        self.family = ResourceId(str(uuid4()))
        self.family_path = "device_credential_families/" + self.family
        self.store.data[self.family_path] = {
            "tenant_id": TENANT,
            "device_id": self.command_body.device_id,
            "epoch": self.tokens.epoch,
            "authorization_version": 1,
            "connection_generation": 1,
            "status": "active",
            "expires_at": self.now + timedelta(days=30),
        }
        self.token = self.tokens.issue(
            tenant_id=TENANT,
            device_id=self.command_body.device_id,
            family_id=self.family,
            authorization_version=1,
            connection_generation=1,
        )
        self.device_identity = self.credentials.authenticate(self.token)
        self.leases = DeviceLeases(self.store, self.credentials, now=lambda: self.now)
        self.binding = self.leases.acquire(self.device_identity, "test-owner")
        self.commands = CommandRequests(self.memories)
        self.service = DeviceCommands(self.memories)
        self.watch = FakeWatchClient(lambda: self.store.data)
        self.gateway = command_gateway(self.leases, cast(Any, self.watch), self.service)
        self.app.state.auth_services = AuthServices(device_gateway=self.gateway)
        self.execution = ResourceId(str(uuid4()))

    def create_command(self) -> Any:
        return self.commands.create(
            self.identity(), TENANT, self.command_body, "dev05-create-" + str(uuid4())
        )

    def deliver(self, identity: ResourceId) -> Any:
        return self.leases.run(
            self.binding, lambda tx: self.service.deliver(tx, self.binding, identity)
        )

    def start(self, identity: ResourceId, execution: ResourceId | None = None) -> Any:
        return self.leases.run(
            self.binding,
            lambda tx: self.service.start(
                tx,
                self.binding,
                {
                    "command_id": identity,
                    "execution_id": execution or self.execution,
                },
            ),
        )

    def status(self, identity: ResourceId, status: str, **extra: Any) -> Any:
        return self.leases.run(
            self.binding,
            lambda tx: self.service.status(
                tx,
                self.binding,
                {
                    "command_id": identity,
                    "execution_id": self.execution,
                    "status": status,
                    "observed_at": self.now.isoformat(),
                    **extra,
                },
            ),
        )

    def accept(self) -> Any:
        record = self.create_command()
        self.deliver(record.id)
        self.assertEqual(self.status(record.id, "accepted")["result"], "recorded")
        return record

    def frame(self, kind: str, generation: int = 1, **payload: Any) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "message_id": str(uuid4()),
            "type": kind,
            "sent_at": self.now.isoformat(),
            "payload": payload,
            **({} if kind == "hello" else {"connection_generation": generation}),
        }

    def connect(self) -> Any:
        return self.client.websocket_connect(
            f"/v1/devices/{self.command_body.device_id}/connect",
            headers={"Authorization": "Bearer " + self.token},
            subprotocols=[SUBPROTOCOL],
        )

    def hello(self, ws: Any) -> Any:
        ws.send_json(
            self.frame(
                "hello",
                device_id=self.command_body.device_id,
                software_version="test",
                supported_protocol_versions=[1],
                capabilities=[],
                last_received_seq=0,
            )
        )
        ack = ws.receive_json()
        self.assertEqual(ack["type"], "hello_ack")
        self.assertNotIn("command_response_ids", ack["payload"])
        return ack["connection_generation"]


class DeviceCommandTests(DeviceCommandFixture):
    def test_accepted_receipt_is_cancellable_and_cancel_first_never_grants(self) -> None:
        command = self.accept()
        current = self.commands.get(self.identity(), TENANT, command.id)
        self.assertEqual(current.status, "accepted")
        self.assertIsNone(current.execution_id)
        self.commands.cancel(self.identity(), TENANT, command.id, "dev05-cancel-first", None)
        self.assertEqual(self.start(command.id)["result"], "rejected")

    def test_claim_first_cancel_conflicts_same_id_replay_and_different_id_refused(self) -> None:
        command = self.accept()
        granted = self.start(command.id)
        self.assertEqual(granted["result"], "granted")
        version = self.commands.get(self.identity(), TENANT, command.id).version
        with self.assertRaises(APIError) as error:
            self.commands.cancel(self.identity(), TENANT, command.id, "dev05-cancel-after", None)
        self.assertEqual(error.exception.code.value, "conflict")
        self.assertEqual(self.start(command.id), granted)
        self.assertEqual(self.commands.get(self.identity(), TENANT, command.id).version, version)
        self.assertEqual(self.start(command.id, ResourceId(str(uuid4())))["result"], "rejected")
        old = self.binding
        self.binding = self.leases.acquire(self.device_identity, "reconnected")
        self.assertEqual(self.start(command.id), granted)
        with self.assertRaises(APIError):
            self.leases.run(
                old,
                lambda tx: self.service.start(
                    tx,
                    old,
                    {
                        "command_id": command.id,
                        "execution_id": self.execution,
                    },
                ),
            )
        self.now += timedelta(seconds=30)
        self.assertEqual(self.start(command.id)["reason_code"], "expired")

    def test_lost_claim_and_status_ack_reconcile_exact_payload(self) -> None:
        command = self.accept()

        def lost() -> None:
            self.store.after_commit = lambda: None
            raise RuntimeError("synthetic response loss")

        self.store.after_commit = lost
        with self.assertRaises(RuntimeError):
            self.start(command.id)
        self.assertEqual(self.start(command.id)["result"], "granted")
        self.store.after_commit = lost
        with self.assertRaises(RuntimeError):
            self.status(
                command.id, "succeeded", observed_state={"power": "on", "brightness_pct": 40}
            )
        ack = self.status(
            command.id, "succeeded", observed_state={"power": "on", "brightness_pct": 40}
        )
        self.assertEqual(ack["result"], "recorded")
        self.assertEqual(
            self.status(command.id, "succeeded", observed_state={"power": "off"})["result"],
            "rejected",
        )
        self.assertEqual(
            self.status(
                command.id,
                "succeeded",
                execution_id=str(uuid4()),
                observed_state={"power": "on", "brightness_pct": 40},
            )["result"],
            "rejected",
        )

    def test_observation_must_match_action_and_unknown_can_reconcile_after_expiry(self) -> None:
        command = self.accept()
        self.start(command.id)
        for state in (
            {},
            {"power": "on"},
            {"power": "off", "brightness_pct": 40},
            {"power": "on", "brightness_pct": 39},
        ):
            self.assertEqual(
                self.status(command.id, "succeeded", observed_state=state)["result"], "rejected"
            )
        self.assertEqual(self.status(command.id, "outcome_unknown")["result"], "recorded")
        self.assertEqual(self.start(command.id)["result"], "rejected")
        self.now += timedelta(seconds=31)
        self.assertEqual(
            self.status(
                command.id, "succeeded", observed_state={"power": "on", "brightness_pct": 40}
            )["result"],
            "recorded",
        )
        self.assertEqual(self.commands.get(self.identity(), TENANT, command.id).status, "succeeded")

    def test_deadline_crossing_during_reads_and_after_commit_blocks_grant(self) -> None:
        command = self.accept()
        original = self.service._open

        def delayed(*args: Any) -> Any:
            result = original(*args)
            self.now += timedelta(seconds=30)
            return result

        with patch.object(self.service, "_open", side_effect=delayed):
            self.assertEqual(self.start(command.id)["result"], "rejected")
        command = self.accept()
        self.leases.heartbeat(self.binding)
        self.store.after_commit = lambda: setattr(self, "now", self.now + timedelta(seconds=30))
        with self.assertRaises(PolicyDenied):
            self.start(command.id)
        self.store.after_commit = lambda: None
        self.assertEqual(self.store.data[command_path(TENANT, command.id)]["status"], "running")

    def test_current_authority_deletion_and_original_family_block_delivery_and_start(self) -> None:
        command = self.accept()
        original = copy.deepcopy(self.store.data)
        paths: dict[str, dict[str, Any]] = {
            f"tenants/{TENANT}/devices/{self.command_body.device_id}": {
                "allowed_scopes": ["shared"]
            },
            f"tenants/{TENANT}/entities/{self.command_body.target_entity_id}": {"deleted": True},
            self.family_path: {"status": "revoked"},
            f"tenants/{TENANT}": {"status": "deleting"},
        }
        paths.update(
            {
                path: {"status": "revoked"}
                for path in original
                if "/home_targets/" in path or "/integrations/" in path
            }
        )
        for path, change in paths.items():
            with self.subTest(path=path):
                self.store.data = copy.deepcopy(original)
                self.store.data[path].update(change)
                with self.assertRaises((APIError, PolicyDenied)):
                    self.start(command.id)
                self.assertIsNone(self.store.data[command_path(TENANT, command.id)]["execution_id"])
        self.store.data = original

    def test_route_scoped_watch_start_ack_and_cleanup(self) -> None:
        command = self.create_command()
        with self.connect() as ws:
            generation = self.hello(ws)
            delivered = ws.receive_json()
            self.assertEqual(delivered["type"], "command")
            self.assertEqual(delivered["payload"]["command_id"], command.id)
            self.assertEqual(self.watch.path, f"tenants/{TENANT}/commands")
            self.assertEqual(self.watch.device, self.command_body.device_id)
            payload = {"command_id": command.id, "execution_id": self.execution}
            ws.send_json(
                self.frame(
                    "command_status",
                    generation,
                    **payload,
                    status="accepted",
                    observed_at=self.now.isoformat(),
                )
            )
            self.assertEqual(ws.receive_json()["payload"], {**payload, "result": "recorded"})
            ws.send_json(self.frame("command_start", generation, **payload))
            grant = ws.receive_json()["payload"]
            self.assertEqual(grant["result"], "granted")
            self.assertNotIn("request_message_id", grant)
            ws.send_json(
                self.frame(
                    "command_status",
                    generation,
                    **payload,
                    status="succeeded",
                    observed_at=self.now.isoformat(),
                    observed_state={"power": "on", "brightness_pct": 40},
                )
            )
            self.assertEqual(ws.receive_json()["payload"]["result"], "recorded")
        self.assertTrue(self.watch.closed)

    def test_reconnect_orders_assigned_unseen_before_new_delivery_and_deduplicates(self) -> None:
        first = self.create_command()
        previous = self.deliver(first.id)  # commit succeeded, socket response lost
        second = self.create_command()
        with self.connect() as ws:
            generation = self.hello(ws)
            one, two = ws.receive_json(), ws.receive_json()
            self.assertEqual(one["payload"], previous)
            self.assertEqual(two["payload"]["command_id"], second.id)
            self.assertGreater(two["payload"]["delivery_seq"], one["payload"]["delivery_seq"])
            self.watch.emit()
            ws.send_json(
                self.frame(
                    "heartbeat", generation, last_received_seq=two["payload"]["delivery_seq"]
                )
            )
            self.assertEqual(ws.receive_json()["type"], "heartbeat_ack")

    def test_running_unknown_oversized_wrong_direction_and_old_generation_close(self) -> None:
        for kind, status, offset in (
            ("command_status", "running", 0),
            ("unknown", "accepted", 0),
            ("command_start_result", "accepted", 0),
            ("command_status", "accepted", -1),
        ):
            self.watch = FakeWatchClient(lambda: self.store.data)
            self.app.state.auth_services = AuthServices(
                device_gateway=command_gateway(self.leases, cast(Any, self.watch), self.service)
            )
            with self.connect() as ws:
                generation = self.hello(ws)
                self.assertTrue(self.watch.ready.wait(2))
                ws.send_json(
                    self.frame(
                        kind,
                        generation + offset,
                        command_id=str(uuid4()),
                        execution_id=self.execution,
                        status=status,
                        observed_at=self.now.isoformat(),
                    )
                )
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
            self.assertTrue(self.watch.closed)

    def test_watch_overflow_closes_and_unsubscribes(self) -> None:
        for _ in range(17):
            self.create_command()
        with (
            patch("brainoid_api.device_command_watch.MAX_PENDING_COMMANDS", 16),
            self.connect() as ws,
        ):
            self.hello(ws)
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.assertTrue(self.watch.closed)

    def test_backlog_expired_commands_are_skipped_and_live_batch_drains_in_order(self) -> None:
        for _ in range(20):
            self.create_command()
        self.now += timedelta(seconds=30)
        current = [self.create_command() for _ in range(20)]
        with self.connect() as ws:
            self.hello(ws)
            frames = [ws.receive_json()["payload"] for _ in current]
            self.assertEqual({frame["command_id"] for frame in frames}, {c.id for c in current})
            self.assertEqual([frame["delivery_seq"] for frame in frames], list(range(1, 21)))
        self.assertTrue(self.watch.closed)

    def test_private_home_only_session_retry_expiry_and_no_idle_renewal(self) -> None:
        from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions

        body, documents = home_fixture(
            TENANT,
            self.identity().user_id,
            self.now,
            private=True,
            display="authenticated_session",
            device=self.command_body.device_id,
        )
        self.store.data.update(documents)
        self.command_body = body
        command = self.create_command()
        self.assertIsNone(self.deliver(command.id))
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        )
        with self.connect() as ws:
            generation = self.hello(ws)
            ws.send_json(self.frame("heartbeat", generation, last_received_seq=0))
            self.assertEqual(ws.receive_json()["type"], "heartbeat_ack")
            session = sessions.start(
                self.identity(),
                TENANT,
                self.command_body.device_id,
                PrivateSessionCreate(
                    allowed_scopes=["home.control"], acknowledge_shared_space=True
                ),
                "dev05-session-key",
                "test",
            )
            delivered = ws.receive_json()
            self.assertEqual(delivered["payload"]["private_session_id"], session.id)
            path = sessions._path(TENANT, session.id)
            before = copy.deepcopy(self.store.data[path])
            ws.send_json(
                self.frame(
                    "heartbeat",
                    generation,
                    last_received_seq=1,
                    active_private_session_id=session.id,
                )
            )
            self.assertEqual(ws.receive_json()["type"], "heartbeat_ack")
            self.assertEqual(self.store.data[path], before)
        self.binding = self.leases.acquire(self.device_identity, "private-check")
        self.status(command.id, "accepted")
        self.store.data[path]["idle_expires_at"] = self.now
        with self.assertRaises(PolicyDenied):
            self.start(command.id)

    def test_current_account_fence_and_original_device_family_block_claim(self) -> None:
        from brainoid_contracts.deletion import lifecycle_account_path
        from brainoid_policy.accounts import AccountUnavailable

        command = self.accept()
        self.store.data[lifecycle_account_path(self.identity().user_id)] = {"mode": "delete"}
        with self.assertRaises((PolicyDenied, AccountUnavailable)):
            self.start(command.id)
        self.assertIsNone(self.store.data[command_path(TENANT, command.id)]["execution_id"])

    def test_watch_acquisition_anyio_cancellation_joins_and_unsubscribes(self) -> None:
        import asyncio
        from threading import Event

        import anyio
        from brainoid_api.device_command_watch import CommandWatch
        from brainoid_api.device_gateway import DeviceConnection

        entered, release = Event(), Event()
        subscribe = self.watch.on_snapshot

        def blocked(callback: Any) -> Any:
            entered.set()
            release.wait(5)
            return subscribe(callback)

        async def scenario() -> None:
            connection = DeviceConnection(self.gateway, cast(Any, None), self.binding)
            scopes = []

            async def owned() -> None:
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    async with CommandWatch(cast(Any, self.watch), self.service)(connection):
                        await asyncio.Future()

            task = asyncio.create_task(owned())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            scopes[0].cancel()
            release.set()
            await asyncio.wait_for(task, 5)
            self.assertTrue(self.watch.closed)

        with patch.object(self.watch, "on_snapshot", side_effect=blocked):
            asyncio.run(scenario())

    def test_grant_deadline_clamps_to_private_authority_and_never_extends_on_retry(self) -> None:
        from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions

        body, documents = home_fixture(
            TENANT,
            self.identity().user_id,
            self.now,
            private=True,
            display="authenticated_session",
            device=self.command_body.device_id,
        )
        self.store.data.update(documents)
        self.command_body = body
        sessions = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        )
        session = sessions.start(
            self.identity(),
            TENANT,
            self.command_body.device_id,
            PrivateSessionCreate(allowed_scopes=["home.control"], acknowledge_shared_space=True),
            "dev05-clamped-session",
            "test",
        )
        deadline = self.now + timedelta(seconds=2)
        path = sessions._path(TENANT, session.id)
        self.store.data[path]["idle_expires_at"] = deadline
        command = self.accept()
        grant = self.start(command.id)
        self.assertEqual(grant["expires_at"], deadline.isoformat())
        self.store.data[path]["idle_expires_at"] = self.now + timedelta(seconds=60)
        self.binding = self.leases.acquire(self.device_identity, "new-transport")
        self.assertEqual(self.start(command.id), grant)
        self.now = deadline
        self.assertEqual(self.start(command.id)["result"], "rejected")

    def test_all_home_actions_require_matching_nonempty_observation(self) -> None:
        from brainoid_memory.commands import CommandCreate

        base = self.command_body.model_dump(mode="json")
        inventory = next(path for path in self.store.data if "/home_targets/" in path)
        actions = [
            ("light.turn_off", {}, {"power": "off"}, {"power": "off", "brightness_pct": 1}),
            (
                "climate.set_temperature",
                {"temperature_c": 20},
                {"temperature_c": 20},
                {"temperature_c": 21},
            ),
            (
                "climate.set_hvac_mode",
                {"hvac_mode": "heat"},
                {"hvac_mode": "heat"},
                {"hvac_mode": "cool"},
            ),
        ]
        for action, parameters, good, bad in actions:
            self.store.data[inventory]["allowed_actions"] = [action]
            self.command_body = CommandCreate.model_validate(
                {**base, "operation": {"action": action, "parameters": parameters}}
            )
            command = self.accept()
            self.start(command.id)
            self.assertEqual(
                self.status(command.id, "succeeded", observed_state=bad)["result"], "rejected"
            )
            self.assertEqual(
                self.status(command.id, "succeeded", observed_state=good)["result"], "recorded"
            )

    def test_delayed_grant_handoff_does_not_release_after_deadline(self) -> None:
        import asyncio

        from brainoid_api.device_gateway import Delivery, DeviceConnection

        command = self.accept()
        sent = []

        async def send(text: str) -> None:
            sent.append(text)

        async def scenario() -> None:
            socket = SimpleNamespace(send_text=send)
            connection = DeviceConnection(self.gateway, cast(Any, socket), self.binding)
            io = connection._io

            async def delayed(call: Any) -> Any:
                result = await io(call)
                self.now += timedelta(seconds=30)
                return result

            with patch.object(connection, "_io", delayed), self.assertRaises(PolicyDenied):
                await connection._transmit(
                    Delivery(
                        "command_start_result",
                        lambda tx, binding: self.service.start(
                            tx, binding, {"command_id": command.id, "execution_id": self.execution}
                        ),
                    )
                )
            await connection.drain()

        asyncio.run(scenario())
        self.assertEqual(sent, [])
        self.assertEqual(self.store.data[command_path(TENANT, command.id)]["status"], "running")
