"""MEM-02 real Firestore contention, ambiguous candidates and response-loss recovery."""

from datetime import timedelta
from functools import partial
from typing import Any
from unittest.mock import patch

from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.entities import Entities, EntityCreate, EntityView
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_contracts import FirebaseUid, ResourceId, TenantContext
from brainoid_contracts.transactions import Transaction
from brainoid_memory import Entity, EntityInput, EntityPatch
from brainoid_memory.entity_resolver import external_key
from brainoid_memory.models import ExternalRef
from brainoid_policy.scopes import PolicyDenied
from google.api_core.exceptions import ServiceUnavailable
from test_device_pairing_integration import DevicePairingIntegrationFixture

INTEGRATION = ResourceId("00000000-0000-0000-0000-000000000020")


class Inventory:
    def resolve(
        self, tx: Transaction, context: TenantContext, reference: ExternalRef
    ) -> EntityInput:
        doc = tx.get(
            f"tenants/{context.tenant_id}/provider_inventory/"
            + external_key(context.tenant_id, reference)
        )
        if doc is None:
            raise PolicyDenied()
        return EntityInput.model_validate(doc)


class EntityIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.entities = Entities(
            self.store,
            self.tenants,
            Cursors(b"e" * 32, now=lambda: self.now),
            ledger=self.ledger,
            credentials=self.repo,
            inventory=Inventory(),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.entity_body = EntityCreate.model_validate(
            {
                "kind": "appliance",
                "label": "照明",
                "aliases": ["light"],
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        # Bootstrap demo's pre-MEM-01 entities are intentionally invalid memory records;
        # keep them for pairing fixtures, and query only the canonical created records.

    def create_entity(self, key: str = "entity-emulator-key") -> EntityView:
        return self.entities.create(self.identity(), self.tenant, self.entity_body, key)

    def reference(self) -> ExternalRef:
        ref = ExternalRef(
            provider="home_assistant", integration_id=INTEGRATION, external_id="opaque/../light.α"
        )
        self.db.document(f"tenants/{self.tenant}/integrations/{INTEGRATION}").set(
            {
                "id": INTEGRATION,
                "tenant_id": self.tenant,
                "provider": "home_assistant",
                "status": "active",
            }
        )
        self.db.document(
            f"tenants/{self.tenant}/provider_inventory/{external_key(self.tenant, ref)}"
        ).set(
            {
                **self.entity_body.model_dump(mode="json"),
                "external_refs": [ref.model_dump(mode="json")],
            }
        )
        return ref

    def test_shared_null_owner_patch_replay_preserves_canonical_record(self) -> None:
        created = self.create_entity()
        path = f"tenants/{self.tenant}/entities/{created.id}"
        self.db.document(path).update({"owner_user_id": None})
        self.assertIsNone(self.entities.get(self.identity(), self.tenant, created.id).owner_user_id)
        update = partial(
            self.entities.update,
            self.identity(),
            self.tenant,
            created.id,
            EntityPatch(label="Changed"),
            "review-owner-none-patch",
            '"1"',
        )
        updated = update()
        self.assertEqual((updated.label, updated.version), ("Changed", 2))
        self.assertEqual(update(), updated)
        self.assertEqual(self.entities.get(self.identity(), self.tenant, created.id), updated)
        self.assertNotIn("owner_user_id", updated.model_dump(mode="json"))
        page = self.entities.list(self.identity(), self.tenant, query="Changed")
        self.assertEqual(
            page.model_dump(mode="json"),
            {
                "items": [updated.model_dump(mode="json")],
                "next_cursor": None,
            },
        )
        document = self.db.document(path).get().to_dict()
        assert document is not None
        self.assertIn("owner_user_id", document)
        self.assertIsNone(document["owner_user_id"])
        self.assertEqual(Entity.model_validate(document).version, 2)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 2)

    def test_same_key_contention_and_compare_and_swap(self) -> None:
        results = self.concurrent(self.create_entity, self.create_entity)
        self.assertIsInstance(results[0], EntityView)
        self.assertEqual(results[0], results[1])
        first = self.create_entity()
        updates = self.concurrent(
            *[
                partial(
                    self.entities.update,
                    self.identity(),
                    self.tenant,
                    first.id,
                    EntityPatch(label=name),
                    "entity-emulator-update-" + name,
                    '"1"',
                )
                for name in ("first", "second")
            ]
        )
        self.assertEqual(sum(isinstance(r, EntityView) for r in updates), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, updates)
        self.assertEqual(self.entities.get(self.identity(), self.tenant, first.id).version, 2)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 2)

    def test_concurrent_external_references_converge_and_integrations_are_distinct(self) -> None:
        ref = self.reference()
        results = self.concurrent(
            *[
                partial(
                    self.entities.register,
                    self.identity(),
                    self.tenant,
                    ref,
                    "entity-external-race-" + key,
                )
                for key in ("first", "second")
            ]
        )
        self.assertIsInstance(results[0], EntityView)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list(self.db.collection_group("entity_external_ids").stream())), 1)
        other = ref.model_copy(
            update={"integration_id": ResourceId("00000000-0000-0000-0000-000000000021")}
        )
        self.db.document(f"tenants/{self.tenant}/integrations/{other.integration_id}").set(
            {
                "id": other.integration_id,
                "tenant_id": self.tenant,
                "provider": "home_assistant",
                "status": "active",
            }
        )
        self.db.document(
            f"tenants/{self.tenant}/provider_inventory/{external_key(self.tenant, other)}"
        ).set(
            {
                **self.entity_body.model_dump(mode="json"),
                "external_refs": [other.model_dump(mode="json")],
            }
        )
        second = self.entities.register(
            self.identity(), self.tenant, other, "entity-external-other"
        )
        first = self.entities.register(self.identity(), self.tenant, ref, "entity-external-first")
        self.assertNotEqual(first.id, second.id)

    def test_ambiguous_private_tenant_and_cursor_boundaries(self) -> None:
        first, second = self.create_entity(), self.create_entity("entity-second-light")
        private = self.entities.create(
            self.identity(),
            self.tenant,
            EntityCreate.model_validate(
                {
                    "kind": "person",
                    "label": "SECRET",
                    "aliases": ["SECRET_ALIAS"],
                    "visibility": "private",
                    "display_policy": "never",
                }
            ),
            "entity-private-key",
        )
        identity = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-admin")})
        page = self.entities.list(identity, self.tenant, query="照明", limit=1)
        self.assertIsNotNone(page.next_cursor)
        self.db.document(f"tenants/{self.tenant}/entities/{private.id}").update({"version": 2})
        next_page = self.entities.list(
            identity, self.tenant, query="照明", limit=1, cursor=page.next_cursor
        )
        self.assertEqual({page.items[0].id, next_page.items[0].id}, {first.id, second.id})
        self.assertEqual(self.entities.list(identity, self.tenant, query="SECRET").items, [])
        self.fails(
            partial(self.entities.get, identity, self.tenant, private.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        other = ResourceId("257e4adc-6163-518b-b46d-d8500e7c569d")
        self.fails(
            partial(self.entities.get, identity, other, first.id), ErrorCode.RESOURCE_NOT_FOUND
        )

    def test_lost_commit_has_one_entity_receipt_and_current_replay_permission(self) -> None:
        commit = self.db._firestore_api.commit

        def lose(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic lost response")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            self.fails(self.create_entity, ErrorCode.TEMPORARILY_UNAVAILABLE)
        result = self.create_entity()
        self.assertEqual(result.version, 1)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 1)
        self.db.document(f"tenants/{self.tenant}/memberships/demo-a-owner").update(
            {"status": "revoked"}
        )
        self.fails(self.create_entity, ErrorCode.RESOURCE_NOT_FOUND)

    def test_pi_person_creation_current_family_and_private_session(self) -> None:
        pairing = self.pairing()
        self.confirm(pairing)
        credentials = self.exchange(pairing)
        device = self.repo.authenticate(credentials.access_token)
        session = PrivateSessions(
            self.store, self.tenants, self.ledger, self.repo, now=lambda: self.now
        ).start(
            self.identity(),
            self.tenant,
            device.sub,
            PrivateSessionCreate(
                allowed_scopes=["memory.read", "memory.write"], acknowledge_shared_space=True
            ),
            "entity-pi-session-key",
            "fixture-request",
        )
        path = f"tenants/{self.tenant}/private_sessions/{session.id}"
        before = self.db.document(path).get().to_dict()
        body = EntityCreate.model_validate(
            {
                "kind": "person",
                "label": "Alex",
                "visibility": "private",
                "display_policy": "authenticated_session",
            }
        )
        created = self.entities.create(
            device, self.tenant, body, "entity-pi-create-key", session.id
        )
        self.assertEqual(created.owner_user_id, self.identity().user_id)
        self.assertEqual(self.entities.get(self.identity(), self.tenant, created.id), created)
        self.assertEqual(self.entities.get(device, self.tenant, created.id, session.id), created)
        self.assertEqual(before, self.db.document(path).get().to_dict())
        self.fails(
            partial(
                self.entities.create,
                device,
                self.tenant,
                self.entity_body,
                "entity-pi-appliance-key",
                session.id,
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.now += timedelta(seconds=60)
        self.fails(
            partial(self.entities.get, device, self.tenant, created.id, session.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_commit_response_after_expiry_hides_body_but_retains_atomic_receipt(self) -> None:
        identity = self.identity()
        commit = self.db._firestore_api.commit
        original_now = self.now

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = commit(*args, **kwargs)
            self.now = identity.expires_at
            return result

        with patch.object(self.db._firestore_api, "commit", side_effect=delayed):
            self.fails(
                partial(
                    self.entities.create,
                    identity,
                    self.tenant,
                    self.entity_body,
                    "entity-delayed-key",
                ),
                ErrorCode.RESOURCE_NOT_FOUND,
            )
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 1)
        self.now = original_now
        result = self.entities.create(identity, self.tenant, self.entity_body, "entity-delayed-key")
        self.assertEqual(result.version, 1)
