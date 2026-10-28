"""DEV06 canonical inventory admission, replay authority and existing command boundary."""

import asyncio
import copy
import dataclasses
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError
from brainoid_api.home_assistant_entities import (
    HomeAssistantEntities,
    HomeIntegrationCreate,
    HomeInventorySync,
)
from brainoid_api.repositories.commands import CommandRequests
from brainoid_api.repositories.devices import DevicePatch, Devices
from brainoid_api.repositories.entities import Entities
from brainoid_api.repositories.memories import Memories
from brainoid_contracts.access import fingerprint
from brainoid_device.home_assistant import HomeAssistant, configure
from brainoid_memory.commands import CommandCreate
from fastapi import FastAPI
from test_device_cloud import CloudFixture
from test_device_pairing import DevicePairingFixture
from test_home_assistant import SECRET, FakeHA
from test_turn_run import StrictStore


def body(*ids: str) -> HomeInventorySync:
    return HomeInventorySync.model_validate(
        {
            "entities": [
                {
                    "external_id": external,
                    "label": "同じ照明",
                    "domain": "light",
                    "supported_actions": ["light.turn_on", "light.turn_off"],
                }
                for external in ids
            ]
        }
    )


class HomeInventoryTests(DevicePairingFixture):
    def setUp(self) -> None:
        with patch("test_device_pairing.MemoryStore", StrictStore):
            super().setUp()
        self.creds = self.credentials()
        self.device = self.repo.authenticate(self.creds.access_token)
        self.cursors = Cursors(b"h" * 32, now=self.clock.now)
        self.management = Devices(
            self.store, self.tenants, self.ledger, self.cursors, now=self.clock.now
        )
        self.home = HomeAssistantEntities(
            self.store, self.management, self.repo, ledger=self.ledger, now=self.clock.now
        )
        self.entities = Entities(
            self.store,
            self.tenants,
            self.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            inventory=self.home.inventory,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        cast(FastAPI, self.client.app).dependency_overrides[get_auth_services] = lambda: (
            AuthServices(
                tenants=self.tenants,
                devices=self.repo,
                entities=self.entities,
                home_assistant=self.home,
            )
        )
        self.integration = self.home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=self.device.sub, name="Synthetic HA"),
            "home-create-fixture",
        )
        self.url = f"/v1/tenants/{self.tenant}/integrations/{self.integration.id}/home-assistant/entities:sync"

    def sync(
        self, inventory: HomeInventorySync | None = None, key: str = "home-sync-fixture-01"
    ) -> Any:
        return self.home.sync(
            self.device,
            self.tenant,
            self.integration.id,
            inventory or body("light.first", "light.second"),
            key,
        )

    def test_exact_ids_shared_server_scope_and_idempotent_current_page(self) -> None:
        page = self.sync()
        self.assertEqual(len({e.id for e in page.items}), 2)
        self.assertEqual({e.label for e in page.items}, {"同じ照明"})
        self.assertEqual({e.visibility.value for e in page.items}, {"shared"})
        before = copy.deepcopy(self.store.data)
        self.assertEqual(self.sync(), page)
        self.assertEqual(self.store.data, before)
        targets = {p: d for p, d in before.items() if "/home_targets/" in p}
        self.sync(key="home-noop-fixture-01")
        self.assertEqual(
            targets, {p: d for p, d in self.store.data.items() if "/home_targets/" in p}
        )
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/devices/{self.device.sub}"][
                "allowed_entity_ids"
            ],
            [],
        )
        registered = self.entities.register(
            self.identity(), self.tenant, page.items[0].external_refs[0], "register-canonical-ha-01"
        )
        self.assertEqual(registered.id, page.items[0].id)

    def test_removed_rename_replay_and_scope_are_not_identity_changes(self) -> None:
        initial = self.sync()
        one = body("light.first").model_dump(mode="json")
        one["entities"][0]["label"] = "Renamed at HA"
        self.sync(HomeInventorySync.model_validate(one), "home-rename-fixture1")
        replay = self.sync()
        self.assertEqual(len(replay.items), 1)
        self.assertEqual(replay.items[0].label, "同じ照明")
        self.assertEqual(
            sum(
                v["status"] == "revoked"
                for p, v in self.store.data.items()
                if "/home_targets/" in p
            ),
            1,
        )
        self.assertIn(replay.items[0].id, {e.id for e in initial.items})
        readded = self.sync(key="home-readd-fixture01")
        self.assertEqual({e.id for e in readded.items}, {e.id for e in initial.items})

    def test_http_contract_requires_device_and_rejects_ownership_or_arbitrary_provider_data(
        self,
    ) -> None:
        response = self.client.post(
            self.url,
            json=body("light.first").model_dump(mode="json"),
            headers={
                "Authorization": "Bearer " + self.creds.access_token,
                "Idempotency-Key": "home-http-fixture01",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(self.creds.access_token, response.text)
        self.assertEqual(
            self.client.post(
                self.url,
                json=body().model_dump(mode="json"),
                headers={"Idempotency-Key": "home-no-device-key"},
            ).status_code,
            401,
        )
        for extra in (
            {"token": "synthetic-not-forwarded"},
            {"visibility": "private"},
            {"owner_user_id": "other"},
            {"url": "http://untrusted.invalid"},
        ):
            result = self.client.post(
                self.url,
                json={**body().model_dump(mode="json"), **extra},
                headers={
                    "Authorization": "Bearer " + self.creds.access_token,
                    "Idempotency-Key": "home-bad-fixture-01",
                },
            )
            self.assertEqual(result.status_code, 422)
        created = self.client.post(
            f"/v1/tenants/{self.tenant}/integrations/home-assistant",
            json={"device_id": str(self.device.sub), "name": "Other HA"},
            headers={"Idempotency-Key": "home-http-create-01"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(
            self.client.get(
                f"/v1/tenants/{self.tenant}/integrations/{self.integration.id}/home-assistant"
            ).status_code,
            200,
        )

    def test_revoked_current_authority_denies_first_attempt_and_replay(self) -> None:
        self.sync()
        for path, change in [
            (f"tenants/{self.tenant}/memberships/demo-a-owner", {"status": "revoked"}),
            ("device_credential_families/" + self.device.family_id, {"status": "revoked"}),
            (f"tenants/{self.tenant}/devices/{self.device.sub}", {"status": "revoked"}),
            (f"tenants/{self.tenant}/integrations/{self.integration.id}", {"status": "revoked"}),
            (f"tenants/{self.tenant}", {"status": "deleted"}),
        ]:
            before = copy.deepcopy(self.store.data)
            self.store.data[path].update(change)
            for key in ("home-sync-fixture-01", "home-new-denied-key"):
                with self.assertRaises(APIError):
                    self.sync(key=key)
            self.store.data = before
        with self.assertRaises(APIError):
            self.home.sync(
                self.device, self.other, self.integration.id, body(), "home-foreign-tenant"
            )
        revoked = self.home.revoke(
            self.identity(), self.tenant, self.integration.id, "home-revoke-fixture"
        )
        self.assertEqual(revoked.status, "revoked")
        with self.assertRaises(APIError):
            self.sync()

    def test_deletion_fences_private_legacy_and_index_corruption_fail_closed(self) -> None:
        page = self.sync()
        index_path = next(p for p in self.store.data if "/entity_external_ids/" in p)
        for path in (index_path, f"tenants/{self.tenant}/entities/{page.items[0].id}"):
            before = copy.deepcopy(self.store.data)
            fence = "resource_deletion_fences/" + fingerprint(path)
            self.store.data[fence] = {"status": "deleted"}
            filtered = self.sync(key="home-fenced-fixture1")
            self.assertEqual(len(filtered.items), 1)
            self.assertEqual(self.repo.authenticate(self.creds.access_token), self.device)
            self.assertEqual(self.sync(key="home-fenced-fixture2"), filtered)
            self.store.data = before
        current = copy.deepcopy(self.store.data)
        self.store.data[index_path]["reference"]["external_id"] = "light.foreign"
        with self.assertRaises(APIError):
            self.sync(key="home-index-corrupt01")
        self.store.data = current
        del self.store.data[index_path]
        with self.assertRaises(APIError):
            self.sync(key="home-legacy-collision")

    def test_discovery_requires_admin_grant_before_existing_commands_accept(self) -> None:
        page = self.sync()
        memories = Memories(
            self.store,
            self.tenants,
            self.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        commands = CommandRequests(memories)
        command_body = CommandCreate.model_validate(
            {
                "device_id": self.device.sub,
                "target_entity_id": page.items[0].id,
                "operation": {"action": "light.turn_on", "parameters": {"brightness_pct": 40}},
            }
        )
        with self.assertRaises(APIError):
            commands.create(self.identity(), self.tenant, command_body, "home-no-auto-grant01")
        self.management.update(
            self.identity(),
            self.tenant,
            self.device.sub,
            DevicePatch(allowed_entity_ids=[page.items[0].id], allowed_scopes=["home.control"]),
            "home-explicit-grant1",
            '"1"',
            "fixture-request",
        )
        accepted = commands.create(
            self.identity(), self.tenant, command_body, "home-explicit-command1"
        )
        self.assertEqual(accepted.status, "queued")
        self.home.revoke(self.identity(), self.tenant, self.integration.id, "home-revoke-command1")
        with self.assertRaises(APIError):
            commands.create(self.identity(), self.tenant, command_body, "home-after-revoke-01")

    def test_targeted_legacy_lookup_and_large_indexed_catalogs(self) -> None:
        self.sync()
        with patch(
            "test_turn_run.StrictTransaction.find_array",
            side_effect=AssertionError("unnecessary lookup"),
        ):
            self.sync(key="home-indexed-no-scan")
        for _ in range(120):
            self.store.data[f"tenants/{self.tenant}/entities/{uuid4()}"] = {"kind": "appliance"}
        with (
            patch(
                "test_turn_run.StrictTransaction.collection",
                side_effect=AssertionError("unbounded scan"),
            ),
            patch(
                "test_turn_run.StrictTransaction.query_page",
                side_effect=AssertionError("catalog page"),
            ),
        ):
            self.assertEqual(len(self.sync(body("light.new"), "home-large-catalog1").items), 1)
        for _ in range(3):
            self.store.data[f"tenants/{self.tenant}/entities/{uuid4()}"] = {
                "external_refs": [
                    {
                        "provider": "home_assistant",
                        "integration_id": self.integration.id,
                        "external_id": "light.legacy",
                    }
                ],
                "visibility": "private",
                "deleted": True,
            }
        with self.assertRaises(APIError):
            self.sync(body("light.legacy"), "home-private-legacy")

    def test_inventory_changes_only_invalidate_the_affected_command_target(self) -> None:
        entity = self.sync(body("light.first")).items[0]
        # This fixture represents a previously authorized device; no IAM transition
        # occurs while the queued command and inventory changes are compared.
        device = self.store.data[f"tenants/{self.tenant}/devices/{self.device.sub}"]
        device["allowed_entity_ids"] = [entity.id]
        device["allowed_scopes"].append("home.control")
        memories = Memories(
            self.store,
            self.tenants,
            self.cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        commands = CommandRequests(memories)
        intent = CommandCreate.model_validate(
            {
                "device_id": self.device.sub,
                "target_entity_id": entity.id,
                "operation": {"action": "light.turn_on", "parameters": {}},
            }
        )
        accepted = commands.create(self.identity(), self.tenant, intent, "home-stable-command")
        for snapshot in (body("light.first", "light.second"), body("light.first")):
            self.sync(snapshot, str(uuid4()))
            self.assertEqual(self.repo.authenticate(self.creds.access_token), self.device)
            self.assertEqual(commands.get(self.identity(), self.tenant, accepted.id), accepted)
        shrink = body("light.first").model_dump(mode="json")
        shrink["entities"][0]["supported_actions"] = ["light.turn_off"]
        self.sync(HomeInventorySync.model_validate(shrink), "home-target-shrink1")
        with self.assertRaises(APIError):
            commands.get(self.identity(), self.tenant, accepted.id)
        self.sync(body("light.first"), "home-target-restore")
        second = commands.create(self.identity(), self.tenant, intent, "home-removal-command")
        self.sync(body(), "home-target-removal")
        with self.assertRaises(APIError):
            commands.get(self.identity(), self.tenant, second.id)

    def test_fenced_admin_metadata_mutation_replay_and_postcommit_publication(self) -> None:
        path = f"tenants/{self.tenant}/integrations/{self.integration.id}"
        fence = "resource_deletion_fences/" + fingerprint(path)
        self.store.data[fence] = {"status": "deleted"}
        calls: list[Callable[[], Any]] = [
            lambda: self.home.get(self.identity(), self.tenant, self.integration.id),
            lambda: self.home.revoke(
                self.identity(), self.tenant, self.integration.id, "home-fenced-revoke1"
            ),
            lambda: self.home.create(
                self.identity(),
                self.tenant,
                HomeIntegrationCreate(device_id=self.device.sub, name="Synthetic HA"),
                "home-create-fixture",
            ),
        ]
        for call in calls:
            with self.assertRaises(APIError):
                call()
        self.assertEqual(self.store.data[path]["status"], "active")
        del self.store.data[fence]
        original_run = self.store.run

        def after_commit(work: Any) -> Any:
            result = original_run(work)
            for candidate, doc in list(self.store.data.items()):
                if "/integrations/" in candidate and doc.get("name") == "Postcommit":
                    self.store.data["resource_deletion_fences/" + fingerprint(candidate)] = {
                        "status": "deleted"
                    }
            return result

        with patch.object(self.store, "run", side_effect=after_commit), self.assertRaises(APIError):
            self.home.create(
                self.identity(),
                self.tenant,
                HomeIntegrationCreate(device_id=self.device.sub, name="Postcommit"),
                "home-postcommit-create",
            )
        self.home.revoke(self.identity(), self.tenant, self.integration.id, "home-replay-revoke01")
        self.store.data[fence] = {"status": "deleted"}
        with self.assertRaises(APIError):
            self.home.revoke(
                self.identity(), self.tenant, self.integration.id, "home-replay-revoke01"
            )

    def test_suppressed_metadata_and_revoke_response_release_are_fenced(self) -> None:
        path = f"tenants/{self.tenant}/integrations/{self.integration.id}"
        for flag in ("deleted", "suppress_reuse"):
            self.store.data[path][flag] = True
            with self.assertRaises(APIError):
                self.home.get(self.identity(), self.tenant, self.integration.id)
            with self.assertRaises(APIError):
                self.home.revoke(self.identity(), self.tenant, self.integration.id, str(uuid4()))
            del self.store.data[path][flag]
        original_run = self.store.run

        def after_revoke_commit(work: Any) -> Any:
            result = original_run(work)
            if self.store.data[path]["status"] == "revoked":
                self.store.data["resource_deletion_fences/" + fingerprint(path)] = {
                    "status": "deleted"
                }
            return result

        with (
            patch.object(self.store, "run", side_effect=after_revoke_commit),
            self.assertRaises(APIError),
        ):
            self.home.revoke(
                self.identity(), self.tenant, self.integration.id, "home-release-revoke"
            )
        self.assertEqual(self.store.data[path]["status"], "revoked")


class HomeCloudCompositionTests(CloudFixture):
    async def test_actual_cloud_client_sync_has_no_ha_secret_and_never_grants_control(self) -> None:
        await self.paired()
        management = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"h" * 32, now=self.clock.now),
            now=self.clock.now,
        )
        home = HomeAssistantEntities(
            self.store, management, self.repo, ledger=self.ledger, now=self.clock.now
        )
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        integration = home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=credentials.device_id, name="Synthetic HA"),
            "home-cloud-create01",
        )
        services = self.app.dependency_overrides[get_auth_services]()
        self.app.dependency_overrides[get_auth_services] = lambda: dataclasses.replace(
            services, home_assistant=home
        )
        configure(
            self.local_storage,
            self.private_store,
            {
                "origin": "http://127.0.0.1:8123",
                "token": SECRET,
                "integration_id": integration.id,
                "allowed_entity_ids": ["light.first", "light.second"],
            },
        )
        adapter = HomeAssistant(
            self.local_runtime,
            self.local_storage,
            self.private_store,
            cloud=self.cloud,
            transport=httpx.MockTransport(FakeHA()),
        )
        task = self.local_runtime.start_task(adapter.synchronize())
        try:
            async with asyncio.timeout(3):
                while adapter.status != "synchronized":
                    await asyncio.sleep(0.01)
            requests = [
                content
                for path, content, _ in self.proxy.requests
                if path.endswith("entities:sync")
            ]
            self.assertEqual(len(requests), 1)
            self.assertNotIn(SECRET.encode(), requests[0])
            self.assertEqual(len([p for p in self.store.data if "/home_targets/" in p]), 2)
            self.assertEqual(
                self.store.data[f"tenants/{self.tenant}/devices/{credentials.device_id}"][
                    "allowed_entity_ids"
                ],
                [],
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.close()

    async def configured_home(
        self,
    ) -> tuple[
        HomeAssistantEntities, Any, FakeHA, HomeAssistant, asyncio.Queue[None], asyncio.Queue[None]
    ]:
        await self.paired()
        management = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"h" * 32, now=self.clock.now),
            now=self.clock.now,
        )
        home = HomeAssistantEntities(
            self.store, management, self.repo, ledger=self.ledger, now=self.clock.now
        )
        credentials = self.cloud.journal.credentials
        assert credentials is not None
        integration = home.create(
            self.identity(),
            self.tenant,
            HomeIntegrationCreate(device_id=credentials.device_id, name="Cycle test"),
            "home-cycle-create01",
        )
        services = self.app.dependency_overrides[get_auth_services]()
        self.app.dependency_overrides[get_auth_services] = lambda: dataclasses.replace(
            services, home_assistant=home
        )
        configure(
            self.local_storage,
            self.private_store,
            {
                "origin": "http://127.0.0.1:8123",
                "token": SECRET,
                "integration_id": integration.id,
                "allowed_entity_ids": ["light.first", "light.second"],
            },
        )
        arrived: asyncio.Queue[None] = asyncio.Queue()
        resume: asyncio.Queue[None] = asyncio.Queue()

        async def pause(seconds: float) -> None:
            await arrived.put(None)
            await resume.get()

        fake = FakeHA()
        adapter = HomeAssistant(
            self.local_runtime,
            self.local_storage,
            self.private_store,
            cloud=self.cloud,
            transport=httpx.MockTransport(fake),
            sleep=pause,
        )
        return home, integration, fake, adapter, arrived, resume

    async def test_deleted_item_stays_excluded_while_live_siblings_keep_synchronizing(self) -> None:
        home, integration, fake, adapter, arrived, resume = await self.configured_home()
        task = self.local_runtime.start_task(adapter.synchronize())
        try:
            async with asyncio.timeout(5):
                await arrived.get()
                target_path, target = next(
                    (p, d)
                    for p, d in self.store.data.items()
                    if "/home_targets/" in p and d["reference"]["external_id"] == "light.first"
                )
                entity_path = f"tenants/{self.tenant}/entities/{target['entity_id']}"
                entity_before = copy.deepcopy(self.store.data[entity_path])
                self.store.data["resource_deletion_fences/" + fingerprint(entity_path)] = {
                    "status": "deleted"
                }
                for label in ("survivor update one", "survivor update two"):
                    fake.states["light.second"]["attributes"]["friendly_name"] = label
                    await resume.put(None)
                    await arrived.get()
                    self.assertEqual(adapter.status, "synchronized")
                    self.assertIsNotNone(adapter.config)
                    self.assertTrue(self.cloud.active)
                    credentials = self.cloud.journal.credentials
                    assert credentials is not None
                    self.repo.authenticate(credentials.access_token)
                    self.assertEqual(self.store.data[target_path]["status"], "revoked")
                    self.assertEqual(self.store.data[entity_path], entity_before)
                    surviving = next(
                        d
                        for p, d in self.store.data.items()
                        if "/home_inventory/" in p
                        and d["reference"]["external_id"] == "light.second"
                    )
                    self.assertEqual(surviving["item"]["label"], label)
                home.revoke(self.identity(), self.tenant, integration.id, "home-cycle-revoke01")
                await resume.put(None)
                await task
                self.assertIsNone(adapter.config)
                self.assertTrue(self.cloud.active)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.close()

    async def test_exact_cloud_payload_excludes_reflected_token_and_origin(self) -> None:
        _, _, fake, adapter, arrived, _ = await self.configured_home()
        fake.states["light.first"]["attributes"]["friendly_name"] = "reflected " + SECRET
        fake.states["light.second"]["attributes"]["friendly_name"] = (
            "reflected http://127.0.0.1:8123"
        )
        task = self.local_runtime.start_task(adapter.synchronize())
        try:
            async with asyncio.timeout(3):
                await arrived.get()
            exported = next(
                content
                for path, content, _ in self.proxy.requests
                if path.endswith("entities:sync")
            )
            self.assertNotIn(SECRET.encode(), exported)
            self.assertNotIn(b"http://127.0.0.1:8123", exported)
            import json

            payload = json.loads(exported)
            self.assertEqual(
                {item["label"] for item in payload["entities"]}, {"light.first", "light.second"}
            )
            self.assertEqual(adapter.status, "synchronized")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await adapter.close()
