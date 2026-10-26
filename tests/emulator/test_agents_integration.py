"""RUN-03 official Firestore concurrency, lost response replay and device read fences."""

from typing import Any
from unittest.mock import patch

from brainoid_agent_core.definition import AgentDefinition
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentPatch, Agents
from brainoid_contracts import FirebaseUid, ResourceId
from google.api_core.exceptions import ServiceUnavailable
from test_device_pairing_integration import DevicePairingIntegrationFixture


class AgentIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.agents = Agents(
            self.store,
            self.tenants,
            Cursors(b"a" * 32, now=lambda: self.now),
            ledger=self.ledger,
            credentials=self.repo,
            now=lambda: self.now,
        )
        self.agent = self.agents.list(self.identity(), self.tenant).items[0]

    def update(
        self, *, admin: bool = False, key: str = "emulator-agent-update-key"
    ) -> AgentDefinition:
        identity = self.identity()
        if admin:
            identity = identity.model_copy(update={"user_id": FirebaseUid("demo-a-admin")})
        return self.agents.update(
            identity,
            self.tenant,
            self.agent.id,
            AgentPatch(persona="FIXTURE_SHARED_PERSONA"),
            key,
            '"1"',
        )

    def test_competing_owner_admin_updates_and_same_key_replay(self) -> None:
        results = self.concurrent(
            self.update, lambda: self.update(admin=True, key="emulator-agent-admin-key")
        )
        self.assertEqual(sum(isinstance(r, AgentDefinition) for r in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        self.assertEqual(self.agents.get(self.identity(), self.tenant, self.agent.id).version, 2)

    def test_lost_commit_replays_and_revoked_member_cannot_replay(self) -> None:
        commit = self.db._firestore_api.commit

        def lose(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic lost response")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            with self.assertRaises(APIError) as error:
                self.update()
        self.assertEqual(error.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        replay = self.update()
        self.assertEqual(replay.version, 2)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 1)
        self.db.document(f"tenants/{self.tenant}/memberships/demo-a-owner").update(
            {"role": "member"}
        )
        with self.assertRaises(APIError) as denied:
            self.update()
        self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)

    def test_same_version_from_smartphone_and_device_and_current_family(self) -> None:
        pairing = self.pairing()
        self.confirm(pairing)
        credentials = self.exchange(pairing)
        identity = self.repo.authenticate(credentials.access_token)
        updated = self.update()
        from_device = self.agents.get(identity, self.tenant, self.agent.id)
        self.assertEqual(from_device, updated)
        self.assertEqual(self.agents.list(identity, self.tenant).items, [updated])
        other = ResourceId("257e4adc-6163-518b-b46d-d8500e7c569d")
        self.fails(
            lambda: self.agents.get(identity, other, self.agent.id), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.db.document("device_credential_families/" + identity.family_id).update(
            {"status": "revoked"}
        )
        self.fails(
            lambda: self.agents.get(identity, self.tenant, self.agent.id),
            ErrorCode.AUTHENTICATION_REQUIRED,
        )
