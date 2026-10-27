"""Official local Firestore concurrency and durable WSS authority regression."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.app import create_app
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.device_leases import ConnectionBinding, DeviceLeases
from brainoid_api.device_protocol import SUBPROTOCOL
from brainoid_api.errors import APIError
from brainoid_api.settings import load_settings
from brainoid_contracts import ResourceId
from brainoid_policy.control_ledger import Change, DeviceTarget
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.transaction import Transaction as SDKTransaction
from starlette.testclient import TestClient
from test_api_foundation import EMULATOR_ENV
from test_device_pairing_integration import DevicePairingIntegrationFixture


class DeviceGatewayIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        pairing = self.pairing()
        self.confirm(pairing)
        self.creds = self.exchange(pairing)
        self.identity_device = self.repo.authenticate(self.creds.access_token)
        self.leases = DeviceLeases(self.store, self.repo, now=lambda: self.now)

    def test_concurrent_instances_allocate_unique_durable_generation_and_old_close_cas(
        self,
    ) -> None:
        barrier = Barrier(2)

        def acquire(owner: str) -> ConnectionBinding:
            repository = DeviceLeases(
                FirestoreTransactions(self.db), self.repo, now=lambda: self.now
            )
            barrier.wait(timeout=10)
            return repository.acquire(self.identity_device, owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(acquire, name) for name in ("first", "second")]
            bindings = [future.result(timeout=30) for future in futures]
        bindings.sort(key=lambda item: item.generation)
        old, new = bindings
        self.assertEqual([item.generation for item in bindings], [1, 2])
        with self.assertRaises(APIError):
            self.leases.run(old, lambda tx: tx.set("gateway_test/effect", {"started": True}))
        self.assertFalse(self.db.document("gateway_test/effect").get().exists)
        self.leases.release(self.identity_device, old.connection_id)
        self.leases.check(new)
        self.leases.run(new, lambda tx: tx.set("gateway_test/effect", {"started": True}))
        self.assertTrue(self.db.document("gateway_test/effect").get().exists)
        self.leases.release(self.identity_device, new.connection_id)
        recreated = DeviceLeases(FirestoreTransactions(self.db), self.repo, now=lambda: self.now)
        self.assertEqual(recreated.acquire(self.identity_device, "third").generation, 3)
        self.assertEqual(self.repo.authenticate(self.creds.access_token), self.identity_device)

    def test_lease_expiry_and_pending_external_device_deny(self) -> None:
        first = self.leases.acquire(self.identity_device, "one")
        self.now += timedelta(seconds=45)
        with self.assertRaises(APIError):
            self.leases.heartbeat(first)
        current = self.leases.acquire(self.identity_device, "two")
        self.ledger.prepare(
            Change(
                operation_id=ResourceId(str(uuid4())),
                tenant_id=self.tenant,
                expected_version=1,
                authorization_version=2,
                effect="deny",
                target=DeviceTarget(device_id=self.creds.device_id, status="revoked"),
            )
        )
        with self.assertRaises(APIError):
            self.leases.check(current)
        with self.assertRaises(APIError):
            self.leases.acquire(self.identity_device, "three")
        self.leases.release(self.identity_device, "two")
        self.assertIs(
            (self.db.document(self.leases.path(self.identity_device)).get().to_dict() or {})[
                "online"
            ],
            False,
        )

    def test_default_runtime_composes_gateway_and_uses_durable_lease(self) -> None:
        app = create_app(settings_loader=lambda: load_settings(EMULATOR_ENV))
        with TestClient(app) as client:
            self.assertIsNotNone(app.state.auth_services.device_gateway)
            with client.websocket_connect(
                f"/v1/devices/{self.creds.device_id}/connect",
                headers={"authorization": "Bearer " + self.creds.access_token},
                subprotocols=[SUBPROTOCOL],
            ) as ws:
                ws.send_text(
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "message_id": str(uuid4()),
                            "type": "hello",
                            "sent_at": self.now.isoformat(),
                            "payload": {
                                "device_id": self.creds.device_id,
                                "software_version": "test",
                                "supported_protocol_versions": [1],
                                "capabilities": [],
                                "last_received_seq": 0,
                            },
                        }
                    )
                )
                ack = ws.receive_json()
                self.assertEqual(ack["type"], "hello_ack")
                self.assertEqual(ack["connection_generation"], 1)
        lease = self.db.document(self.leases.path(self.identity_device)).get().to_dict()
        self.assertIsNotNone(lease)
        self.assertIs((lease or {})["online"], False)

    def test_cancelled_acquire_and_sdk_retry_preserve_current_connection(self) -> None:
        healthy = self.leases.acquire(self.identity_device, "healthy")
        stopped = Event()

        def admit() -> None:
            if stopped.is_set():
                raise TimeoutError()

        original = self.repo.authorize

        def delayed(*args: Any) -> Any:
            result = original(*args)
            stopped.set()
            return result

        with patch.object(self.repo, "authorize", delayed), self.assertRaises(TimeoutError):
            self.leases.acquire(self.identity_device, "cancelled-during-read", admit=admit)
        self.leases.check(healthy)
        stopped.clear()
        commits: list[bool] = []

        def abort_before_commit(tx: SDKTransaction) -> Any:
            # Exercise the real SDK retry path after real Emulator reads, without
            # committing this attempt; cancellation precedes the next callback.
            commits.append(True)
            # A real server abort releases its locks. The injected failure must
            # do so too, otherwise the test strands the first SDK transaction.
            tx._rollback()
            stopped.set()
            raise Aborted("synthetic aborted acquire")  # type: ignore[no-untyped-call]

        with patch.object(SDKTransaction, "_commit", abort_before_commit):
            with self.assertRaises(TimeoutError):
                self.leases.acquire(self.identity_device, "cancelled-retry", admit=admit)
        self.assertEqual(commits, [True])
        self.leases.check(healthy)
        state = self.db.document(self.leases.path(self.identity_device)).get().to_dict() or {}
        self.assertEqual(state["generation"], healthy.generation)
        self.assertEqual(state["connection_id"], "healthy")
        self.assertTrue(state["online"])
