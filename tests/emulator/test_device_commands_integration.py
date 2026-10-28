"""DEV-05 real Firestore races and server-watch delivery through runtime WSS."""

from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.app import create_app
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.device_command_watch import CommandWatch
from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_notifications import DeviceNotifications
from brainoid_api.device_protocol import SUBPROTOCOL
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.settings import load_settings
from brainoid_contracts import ResourceId
from brainoid_memory.commands import Command, command_path
from fakes.home import home_fixture
from google.api_core.exceptions import Aborted, ServiceUnavailable
from starlette.testclient import TestClient
from test_api_foundation import EMULATOR_ENV
from test_memories_integration import MemoryIntegrationFixture


class DeviceCommandsIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.command_body, documents = home_fixture(self.tenant, self.identity().user_id, self.now)
        for path, doc in documents.items():
            self.db.document(path).set(doc)
        self.family = ResourceId(str(uuid4()))
        self.family_path = "device_credential_families/" + self.family
        self.db.document(self.family_path).set(
            {
                "tenant_id": self.tenant,
                "device_id": self.command_body.device_id,
                "epoch": self.tokens.epoch,
                "authorization_version": 1,
                "connection_generation": 1,
                "status": "active",
                "expires_at": self.now + timedelta(days=30),
            }
        )
        self.token = self.tokens.issue(
            tenant_id=self.tenant,
            device_id=self.command_body.device_id,
            family_id=self.family,
            authorization_version=1,
            connection_generation=1,
        )
        self.device = self.repo.authenticate(self.token)
        self.leases = DeviceLeases(self.store, self.repo, now=lambda: self.now)
        self.binding = self.leases.acquire(self.device, "emulator-owner")
        self.commands = CommandRequests(self.memories)
        self.service = DeviceCommands(self.memories)
        self.execution = ResourceId(str(uuid4()))

    def create_command(self) -> Command:
        return self.commands.create(
            self.identity(), self.tenant, self.command_body, "dev05-emu-" + str(uuid4())
        )

    def status(self, command: ResourceId, status: str, **extra: Any) -> dict[str, Any]:
        return self.leases.run(
            self.binding,
            lambda tx: self.service.status(
                tx,
                self.binding,
                {
                    "command_id": command,
                    "execution_id": self.execution,
                    "status": status,
                    "observed_at": self.now.isoformat(),
                    **extra,
                },
            ),
        )

    def accepted(self) -> Command:
        command = self.create_command()
        self.leases.run(self.binding, lambda tx: self.service.deliver(tx, self.binding, command.id))
        self.assertEqual(self.status(command.id, "accepted")["result"], "recorded")
        return command

    def start(self, command: ResourceId, execution: ResourceId | None = None) -> dict[str, Any]:
        return self.leases.run(
            self.binding,
            lambda tx: self.service.start(
                tx,
                self.binding,
                {
                    "command_id": command,
                    "execution_id": execution or self.execution,
                },
            ),
        )

    def cancel(self, command: ResourceId) -> Command:
        return self.commands.cancel(
            self.identity(), self.tenant, command, "dev05-cancel-" + str(uuid4()), None
        )

    def test_actual_claim_cancel_orderings_and_concurrent_transactions(self) -> None:
        first = self.accepted()
        self.cancel(first.id)
        self.assertEqual(self.start(first.id)["result"], "rejected")
        second = self.accepted()
        self.assertEqual(self.start(second.id)["result"], "granted")
        self.fails(partial(self.cancel, second.id), ErrorCode.CONFLICT)
        for _ in range(3):
            command = self.accepted()
            cancel, start = self.concurrent(
                partial(self.cancel, command.id), partial(self.start, command.id)
            )
            assert isinstance(start, dict)
            saved = self.commands.get(self.identity(), self.tenant, command.id)
            if saved.status == "running":
                self.assertEqual(cancel, ErrorCode.CONFLICT)
                self.assertEqual(start["result"], "granted")
                self.assertEqual(saved.execution_id, self.execution)
            else:
                self.assertEqual(saved.status, "cancelled")
                self.assertIsInstance(cancel, Command)
                self.assertEqual(start["result"], "rejected")
                self.assertIsNone(saved.execution_id)

    def test_simultaneous_same_and_different_execution_claims_never_rebind(self) -> None:
        command = self.accepted()
        one, two = self.concurrent(partial(self.start, command.id), partial(self.start, command.id))
        assert isinstance(one, dict) and isinstance(two, dict)
        self.assertEqual(one, two)
        self.assertEqual(one["result"], "granted")
        command = self.accepted()
        one, two = self.concurrent(
            partial(self.start, command.id),
            partial(self.start, command.id, ResourceId(str(uuid4()))),
        )
        assert isinstance(one, dict) and isinstance(two, dict)
        self.assertEqual(one["result"], "granted")
        self.assertEqual(two["result"], "rejected")
        self.assertEqual(
            self.commands.get(self.identity(), self.tenant, command.id).execution_id, self.execution
        )

    def test_grant_and_result_commit_response_loss_recover_exact_receipts(self) -> None:
        command = self.accepted()
        commit = self.db._firestore_api.commit

        def lost(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic DEV05 response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(partial(self.start, command.id), ErrorCode.TEMPORARILY_UNAVAILABLE)
        grant = self.start(command.id)
        self.assertEqual(grant["result"], "granted")
        self.binding = DeviceLeases(
            FirestoreTransactions(self.db), self.repo, now=lambda: self.now
        ).acquire(self.device, "new-instance")
        self.assertEqual(self.start(command.id), grant)
        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(
                partial(
                    self.status,
                    command.id,
                    "succeeded",
                    observed_state={"power": "on", "brightness_pct": 40},
                ),
                ErrorCode.TEMPORARILY_UNAVAILABLE,
            )
        self.assertEqual(
            self.status(
                command.id, "succeeded", observed_state={"power": "on", "brightness_pct": 40}
            )["result"],
            "recorded",
        )
        self.assertEqual(
            self.status(command.id, "succeeded", observed_state={"power": "off"})["result"],
            "rejected",
        )

    def test_retry_rechecks_family_and_does_not_commit_running(self) -> None:
        command = self.accepted()
        commit = self.db._firestore_api.commit
        revoked = False

        def retry(*args: Any, **kwargs: Any) -> Any:
            nonlocal revoked
            request = kwargs.get("request", {})
            if not revoked and request.get("transaction"):
                revoked = True
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                self.db.document(self.family_path).update({"status": "revoked"})
                raise Aborted("synthetic current-family race")  # type: ignore[no-untyped-call]
            return commit(*args, **kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=retry):
            with self.assertRaises(APIError):
                self.start(command.id)
        saved = self.db.document(command_path(self.tenant, command.id)).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["status"], "accepted")
        self.assertIsNone(saved["execution_id"])

    def test_runtime_firestore_watch_cross_instance_delivery_reconnect_and_status(self) -> None:
        app = create_app(settings_loader=lambda: load_settings(EMULATOR_ENV))

        def frame(kind: str, generation: int = 1, **payload: Any) -> dict[str, Any]:
            return {
                "protocol_version": 1,
                "message_id": str(uuid4()),
                "type": kind,
                "sent_at": self.now.isoformat(),
                "payload": payload,
                **({} if kind == "hello" else {"connection_generation": generation}),
            }

        def hello(ws: Any) -> int:
            ws.send_json(
                frame(
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
            return int(ack["connection_generation"])

        with TestClient(app) as client:
            gateway = app.state.auth_services.device_gateway
            self.assertEqual(
                tuple(type(hook) for hook in gateway.hooks), (CommandWatch, DeviceNotifications)
            )
            with client.websocket_connect(
                f"/v1/devices/{self.command_body.device_id}/connect",
                headers={"Authorization": "Bearer " + self.token},
                subprotocols=[SUBPROTOCOL],
            ) as ws:
                generation = hello(ws)
                command = self.create_command()  # separate repository/client from runtime watch
                delivered = ws.receive_json()
                self.assertEqual(delivered["type"], "command")
                self.assertEqual(delivered["payload"]["command_id"], command.id)
            with client.websocket_connect(
                f"/v1/devices/{self.command_body.device_id}/connect",
                headers={"Authorization": "Bearer " + self.token},
                subprotocols=[SUBPROTOCOL],
            ) as ws:
                next_generation = hello(ws)
                self.assertGreater(next_generation, generation)
                self.assertEqual(ws.receive_json()["payload"], delivered["payload"])
                payload = {"command_id": command.id, "execution_id": self.execution}
                ws.send_json(
                    frame(
                        "command_status",
                        next_generation,
                        **payload,
                        status="accepted",
                        observed_at=self.now.isoformat(),
                    )
                )
                self.assertEqual(ws.receive_json()["payload"]["result"], "recorded")
                ws.send_json(frame("command_start", next_generation, **payload))
                self.assertEqual(ws.receive_json()["payload"]["result"], "granted")
        lease = self.db.document(self.leases.path(self.device)).get().to_dict()
        assert lease is not None
        self.assertFalse(lease["online"])
