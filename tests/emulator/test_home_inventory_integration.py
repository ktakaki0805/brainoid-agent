"""DEV06 official Firestore transactions: exact index races, revocation and response loss."""

from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.home_assistant_entities import HomeAssistantEntities, HomeIntegrationCreate
from brainoid_api.repositories.devices import Devices
from brainoid_api.repositories.entities import EntityPage
from google.api_core.exceptions import ServiceUnavailable
from test_device_pairing_integration import DevicePairingIntegrationFixture
from test_home_inventory import body


class HomeInventoryIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        pairing = self.pairing()
        self.confirm(pairing)
        credentials = self.exchange(pairing)
        self.device = self.repo.authenticate(credentials.access_token)
        devices = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"h" * 32, now=lambda: self.now),
            now=lambda: self.now,
        )
        self.home = HomeAssistantEntities(
            self.store, devices, self.repo, ledger=self.ledger, now=lambda: self.now
        )
        self.integration = self.home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=self.device.sub, name="Synthetic HA"),
            "home-emulator-create",
        )

    def sync(self, key: str = "home-emulator-sync01") -> EntityPage:
        return self.home.sync(
            self.device, self.tenant, self.integration.id, body("light.first", "light.second"), key
        )

    def test_same_and_distinct_key_races_create_one_index_per_exact_id(self) -> None:
        results = self.concurrent(self.sync, self.sync)
        self.assertIsInstance(results[0], EntityPage)
        self.assertEqual(results[0], results[1])
        results = self.concurrent(
            partial(self.sync, "home-emulator-race01"), partial(self.sync, "home-emulator-race02")
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list(self.db.collection_group("home_targets").stream())), 2)
        self.assertEqual(len(list(self.db.collection_group("entity_external_ids").stream())), 2)
        assert isinstance(results[0], EntityPage)
        self.assertEqual(len({item.id for item in results[0].items}), 2)

    def test_response_loss_receipt_replay_does_not_resurrect_removed_targets(self) -> None:
        commit = self.db._firestore_api.commit

        def lose(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            self.fails(self.sync, ErrorCode.TEMPORARILY_UNAVAILABLE)
        first = self.sync()
        self.home.sync(
            self.device,
            self.tenant,
            self.integration.id,
            body("light.first"),
            "home-emulator-remove",
        )
        replay = self.sync()
        self.assertEqual(len(replay.items), 1)
        self.assertIn(replay.items[0].id, {item.id for item in first.items})
        self.assertEqual(len(list(self.db.collection_group("home_targets").stream())), 2)

    def test_current_family_membership_and_integration_revocation_reject_replay(self) -> None:
        self.sync()
        family = self.db.document("device_credential_families/" + self.device.family_id)
        family.update({"status": "revoked"})
        self.fails(self.sync, ErrorCode.AUTHENTICATION_REQUIRED)
        family.update({"status": "active"})
        member = self.db.document(f"tenants/{self.tenant}/memberships/demo-a-owner")
        member.update({"status": "revoked"})
        self.fails(self.sync, ErrorCode.RESOURCE_EXPIRED)
        member.update({"status": "active"})
        self.home.revoke(self.identity(), self.tenant, self.integration.id, "home-emulator-revoke")
        self.fails(self.sync, ErrorCode.RESOURCE_EXPIRED)

    def test_distinct_integration_namespaces_and_expired_commit_release(self) -> None:
        first = self.sync()
        second = self.home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=self.device.sub, name="Synthetic other HA"),
            "home-emulator-other1",
        )
        page = self.home.sync(
            self.device, self.tenant, second.id, body("light.first"), "home-emulator-other2"
        )
        self.assertNotIn(page.items[0].id, {item.id for item in first.items})
        commit = self.db._firestore_api.commit
        before = self.now

        def delay(*args: Any, **kwargs: Any) -> Any:
            result = commit(*args, **kwargs)
            from datetime import UTC, datetime

            self.now = datetime.fromtimestamp(self.device.exp, UTC)
            return result

        with patch.object(self.db._firestore_api, "commit", side_effect=delay):
            self.fails(partial(self.sync, "home-emulator-expiry"), ErrorCode.RESOURCE_NOT_FOUND)
        self.now = before
        self.assertEqual(self.sync("home-emulator-expiry"), first)

    def test_maximum_inventory_then_another_integration_with_official_transactions(self) -> None:
        maximum = body(*(f"light.item_{i}" for i in range(100)))
        first = self.home.sync(
            self.device, self.tenant, self.integration.id, maximum, "home-maximum-official"
        )
        self.assertEqual(len(first.items), 100)
        other = self.home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=self.device.sub, name="Second bounded inventory"),
            "home-second-maximum",
        )
        second = self.home.sync(self.device, self.tenant, other.id, maximum, "home-another-maximum")
        self.assertEqual(len(second.items), 100)
        self.assertFalse({i.id for i in first.items} & {i.id for i in second.items})
        self.assertEqual(len(list(self.db.collection_group("home_targets").stream())), 200)
        self.assertEqual(
            self.home.sync(self.device, self.tenant, other.id, maximum, "home-another-maximum"),
            second,
        )

    def test_exact_legacy_array_lookup_blocks_private_deleted_reuse(self) -> None:
        reference = {
            "provider": "home_assistant",
            "integration_id": self.integration.id,
            "external_id": "light.legacy",
        }
        for _ in range(3):
            self.db.document(f"tenants/{self.tenant}/entities/{uuid4()}").set(
                {"external_refs": [reference], "visibility": "private", "deleted": True}
            )
        self.fails(
            lambda: self.home.sync(
                self.device,
                self.tenant,
                self.integration.id,
                body("light.legacy"),
                "home-legacy-official",
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(len(list(self.db.collection_group("home_targets").stream())), 0)
        # A different exact ID is independent of those hidden historical rows.
        self.assertEqual(len(self.sync().items), 2)
