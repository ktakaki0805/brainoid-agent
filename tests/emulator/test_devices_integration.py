"""Real transaction conflicts and crash recovery for T-004 device revocation."""

from functools import partial
from typing import Any
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import Device, PairingConfirm, PairingCreate
from brainoid_api.repositories.devices import DevicePatch, Devices
from brainoid_api.services.device_authorization import DeviceAuthorization, DeviceBinding
from brainoid_contracts import ResourceId
from brainoid_policy.control_ledger import LedgerError
from test_device_pairing_integration import KEY, DevicePairingIntegrationFixture


class DeviceManagementIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.management = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"d" * 32, now=lambda: self.now),
            now=lambda: self.now,
        )
        self.nonce = str(uuid4())
        self.initial = self.repo.create(
            PairingCreate(device_name="Pi", installation_nonce=self.nonce), KEY, "127.0.0.1"
        )
        self.device = self.confirm(self.initial)
        self.original = self.exchange(self.initial)
        self.path = f"tenants/{self.tenant}/devices/{self.device.id}"
        self.binding = DeviceBinding(
            tenant_id=self.tenant,
            device_id=self.device.id,
            authorization_version=1,
            connection_generation=1,
        )

    def revoke(self, key: str = KEY, version: int = 1) -> None:
        self.management.revoke(
            self.identity(), self.tenant, self.device.id, key, f'"{version}"', "request-revoke"
        )

    def update(self, key: str = KEY) -> Device:
        return self.management.update(
            self.identity(),
            self.tenant,
            self.device.id,
            DevicePatch(allowed_scopes=["notes"]),
            key,
            '"1"',
            "request-shrink",
        )

    def denied(self) -> None:
        for call in (
            partial(self.repo.authenticate, self.original.access_token),
            partial(self.refresh, self.body(self.original)),
            partial(self.exchange, self.initial),
            partial(self.confirm, self.initial),
            lambda: self.store.run(
                lambda tx: DeviceAuthorization(self.ledger, now=lambda: self.now).current(
                    tx, self.binding
                )
            ),
        ):
            with self.assertRaises(APIError):
                call()

    def test_identical_shrink_retry_and_conflicting_updates_issue_one_version(self) -> None:
        results = self.concurrent(self.update, self.update)
        # A concurrent initial retry may observe pending state; repeat the same key.
        self.assertTrue(any(isinstance(r, Device) for r in results), results)
        result = self.update()
        self.assertEqual(
            (result.version, result.authorization_version, result.connection_generation), (2, 2, 2)
        )
        self.denied()
        self.fails(lambda: self.update(KEY + "competing"), ErrorCode.VERSION_CONFLICT)

    def test_deny_write_response_loss_stops_refresh_replay_before_db_apply(self) -> None:
        refresh = self.body(self.original)
        self.refresh(refresh)
        objects = self.ledger._objects
        original_create = objects.create

        def create(name: str, data: bytes) -> bool:
            result = original_create(name, data)
            if "/intents/" in name:
                raise LedgerError()
            return result

        from unittest.mock import patch

        with patch.object(objects, "create", side_effect=create):
            self.fails(self.update, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(
            (self.db.document(self.path).get().to_dict() or {})["authorization_version"], 1
        )
        with self.assertRaises(APIError):
            self.refresh(refresh)
        self.denied()
        self.update()
        self.assertEqual(
            (self.db.document(self.path).get().to_dict() or {})["connection_generation"], 2
        )

    def test_revoke_and_concurrent_explicit_replacement_keep_old_device_dead(self) -> None:
        self.revoke()
        pairing = self.repo.create(
            PairingCreate(device_name="Pi", installation_nonce=self.nonce), KEY + "new", "127.0.0.1"
        )

        def replace() -> Device:
            return self.repo.confirm(
                self.identity(),
                self.tenant,
                PairingConfirm(
                    user_code=pairing.user_code,
                    device_name="Pi replacement",
                    allowed_entity_ids=[],
                    replaces_device_id=self.device.id,
                ),
                KEY + "replace",
                "127.0.0.1",
            )

        results = self.concurrent(replace, replace)
        # An aborted read/commit can surface as retryable 503. Reconcile the SAME
        # logical request; a new key could conceal duplicate replacement effects.
        for index, result in enumerate(results):
            for _ in range(3):
                if result != ErrorCode.TEMPORARILY_UNAVAILABLE:
                    break
                try:
                    result = replace()
                except APIError as error:
                    result = error.code
            results[index] = result
        self.assertTrue(all(isinstance(result, Device) for result in results))
        self.assertEqual(results[0], results[1])
        replacement = replace()
        self.assertEqual(replacement, results[0])
        self.assertNotEqual(replacement.id, self.device.id)
        new = self.exchange(pairing)
        self.repo.authenticate(new.access_token)
        self.denied()
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/devices").stream())), 4
        )
        self.assertEqual((self.db.document(self.path).get().to_dict() or {})["status"], "revoked")
        next_pair = self.repo.create(
            PairingCreate(device_name="Pi", installation_nonce=self.nonce),
            KEY + "third",
            "127.0.0.1",
        )
        self.fails(
            lambda: self.repo.confirm(
                self.identity(),
                self.tenant,
                PairingConfirm(
                    user_code=next_pair.user_code,
                    device_name="Pi",
                    allowed_entity_ids=[],
                    replaces_device_id=self.device.id,
                ),
                KEY + "third-confirm",
                "127.0.0.1",
            ),
            ErrorCode.CONFLICT,
        )

    def test_cross_tenant_entities_member_and_tampered_canonical_document(self) -> None:
        entity = ResourceId(str(uuid4()))
        other = ResourceId(str(uuid4()))
        self.db.document(f"tenants/{self.tenant}/entities/{entity}").set(
            {"id": entity, "tenant_id": other, "kind": "appliance", "deleted": False}
        )
        self.fails(
            lambda: self.management.update(
                self.identity(),
                self.tenant,
                self.device.id,
                DevicePatch(allowed_entity_ids=[entity]),
                KEY,
                '"1"',
                "request",
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        from brainoid_contracts import FirebaseUid

        member = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-member")})
        self.fails(
            lambda: self.management.update(
                member,
                self.tenant,
                self.device.id,
                DevicePatch(allowed_scopes=["home.control"]),
                KEY,
                '"1"',
                "request",
            ),
            ErrorCode.OPERATION_FORBIDDEN,
        )
        self.assertEqual(
            (self.db.document(self.path).get().to_dict() or {})["authorization_version"], 1
        )

    def test_db_apply_response_loss_and_ledger_unavailability_never_reactivate(self) -> None:
        from unittest.mock import patch

        original_run = self.store.run
        count = 0

        def run(work: Any) -> Any:
            nonlocal count
            count += 1
            result = original_run(work)
            if count == 3:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            return result

        with patch.object(self.store, "run", side_effect=run):
            self.fails(self.revoke, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.denied()
        self.revoke()
        self.assertEqual(
            (self.db.document(self.path).get().to_dict() or {})["connection_generation"], 2
        )
        with patch.object(self.ledger, "latest", side_effect=LedgerError()):
            self.denied()
