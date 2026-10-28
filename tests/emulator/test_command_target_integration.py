"""DEV07 exact control target resolution on official Auth/Firestore fixtures."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.errors import APIError
from brainoid_api.repositories.commands import CommandRequests
from brainoid_contracts import ResourceId
from fakes.home import home_fixture
from test_memories_integration import MemoryIntegrationFixture


class CommandTargetIntegrationTests(MemoryIntegrationFixture):
    def test_control_only_target_current_fingerprint_actor_and_cross_tenant_denials(self) -> None:
        body, documents = home_fixture(self.tenant, self.identity().user_id, self.now)
        for path, doc in documents.items():
            self.db.document(path).set(doc)
        family = ResourceId(str(uuid4()))
        self.db.document("device_credential_families/" + family).set(
            {
                "tenant_id": self.tenant,
                "device_id": body.device_id,
                "epoch": self.tokens.epoch,
                "authorization_version": 1,
                "connection_generation": 1,
                "status": "active",
                "expires_at": self.now + timedelta(days=30),
            }
        )
        device = self.repo.authenticate(
            self.tokens.issue(
                tenant_id=self.tenant,
                device_id=body.device_id,
                family_id=family,
                authorization_version=1,
                connection_generation=1,
            )
        )
        commands = CommandRequests(self.memories)
        record = commands.create(self.identity(), self.tenant, body, str(uuid4()))
        target = commands.target(device, self.tenant, record.id)
        self.assertEqual(target.reference.external_id, "light.fixture")
        self.assertEqual(target.device_id, body.device_id)
        with self.assertRaises(APIError):
            commands.target(device, ResourceId(str(uuid4())), record.id)
        for path, update in (
            (f"tenants/{self.tenant}/devices/{body.device_id}", {"allowed_entity_ids": []}),
            (next(p for p in documents if "/home_targets/" in p), {"version": 2}),
            (f"tenants/{self.tenant}/memberships/demo-a-owner", {"status": "revoked"}),
            ("device_credential_families/" + family, {"status": "revoked"}),
        ):
            previous = self.db.document(path).get().to_dict()
            self.db.document(path).update(update)
            with self.assertRaises(APIError):
                commands.target(device, self.tenant, record.id)
            assert previous is not None
            self.db.document(path).set(previous)
        original = self.store.run

        def after_commit(work: Any) -> Any:
            result = original(work)
            self.now += timedelta(seconds=901)
            return result

        with patch.object(self.store, "run", side_effect=after_commit), self.assertRaises(APIError):
            commands.target(device, self.tenant, record.id)
