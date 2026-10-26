"""MEM-02/T-001/T-009: public CRUD, ambiguous candidates and trusted external IDs."""

import copy
import unittest
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceTokens
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import (
    DeviceCredentialRepository,
    PairingConfirm,
    PairingCreate,
    PairingExchange,
)
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.tenants import TenantCreate, Tenants
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions
from brainoid_api.settings import load_settings
from brainoid_contracts import FirebaseUid, ResourceId, TenantContext
from brainoid_contracts.transactions import Transaction
from brainoid_memory import Entity, EntityInput, EntityPatch
from brainoid_memory.entity_resolver import external_key
from brainoid_memory.models import ExternalRef
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import NOW, OTHER, TENANT, USER, GraphStore, authority_data
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_device_pairing import EPOCH, KEYRING

KEY = "entity-fixture-key-001"
INTEGRATION = ResourceId("00000000-0000-0000-0000-000000000020")


class FixtureInventory:
    """Synthetic provider-verified snapshot, stored independently of API input."""

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


class EntityFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.uid = USER
        self.store = GraphStore()
        self.store.data = authority_data()
        self.ledger = ControlLedger(MemoryObjects())
        self.cursors = Cursors(b"e" * 32, now=lambda: self.now)
        self.tenants = Tenants(self.store, self.cursors, ledger=self.ledger, now=lambda: self.now)
        self.tokens = DeviceTokens(
            KEYRING,
            project="demo-brainoid",
            credential_epoch=EPOCH,
            test_only=True,
            now=lambda: self.now,
        )
        self.credentials = DeviceCredentialRepository(
            self.store, self.tenants, self.ledger, self.tokens, now=lambda: self.now
        )
        self.entities = Entities(
            self.store,
            self.tenants,
            self.cursors,
            ledger=self.ledger,
            credentials=self.credentials,
            inventory=FixtureInventory(),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        self.app.dependency_overrides[reader_identity] = lambda: self.identity()
        self.app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.credentials,
            entities=self.entities,
        )
        self.client = self.enterContext(TestClient(self.app))
        self.url = f"/v1/tenants/{TENANT}/entities"

    def identity(self) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=self.uid,
            issued_at=NOW,
            authenticated_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

    def create_entity(self, key: str = KEY, **changes: Any) -> Any:
        return self.client.post(
            self.url,
            json={
                "kind": "appliance",
                "label": "照明",
                "aliases": ["light"],
                "visibility": "shared",
                "display_policy": "shared_display",
                **changes,
            },
            headers={"Idempotency-Key": key},
        )

    def inventory(self, *, tenant: ResourceId = TENANT, **changes: Any) -> ExternalRef:
        reference = ExternalRef(
            provider="home_assistant",
            integration_id=INTEGRATION,
            external_id="opaque/../light.α",
        )
        self.store.data[f"tenants/{tenant}/integrations/{INTEGRATION}"] = {
            "id": INTEGRATION,
            "tenant_id": tenant,
            "provider": "home_assistant",
            "status": "active",
        }
        self.store.data[
            f"tenants/{tenant}/provider_inventory/{external_key(tenant, reference)}"
        ] = {
            "kind": "appliance",
            "label": "照明",
            "aliases": ["provider alias"],
            "visibility": "shared",
            "display_policy": "shared_display",
            "external_refs": [reference.model_dump(mode="json")],
            **changes,
        }
        return reference

    def device(self, allowed: list[ResourceId] | None = None) -> Any:
        pairing = self.credentials.create(
            PairingCreate(device_name="Fixture Pi", installation_nonce="n" * 32),
            "fixture-pairing-key",
            "127.0.0.1",
        )
        self.credentials.confirm(
            self.identity(),
            TENANT,
            PairingConfirm(
                user_code=pairing.user_code,
                device_name="Fixture Pi",
                allowed_entity_ids=allowed or [],
            ),
            "fixture-confirm-key",
            "127.0.0.1",
        )
        return self.credentials.exchange(
            pairing.pairing_id,
            PairingExchange(poll_secret=pairing.poll_secret),
            "fixture-exchange-key",
            "127.0.0.1",
        )

    def session(self, device: ResourceId) -> ResourceId:
        result = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        ).start(
            self.identity(),
            TENANT,
            device,
            PrivateSessionCreate(
                allowed_scopes=["memory.read", "memory.write"], acknowledge_shared_space=True
            ),
            "entity-private-session-key",
            "fixture-request",
        )
        return result.id

    def fails(self, action: Any, code: ErrorCode) -> None:
        with self.assertRaises(APIError) as error:
            action()
        self.assertEqual(error.exception.code, code)


class EntityTests(EntityFixture):
    def test_server_bootstrap_membership_works_but_tracked_missing_ledger_is_denied(self) -> None:
        tenant = self.tenants.create(
            self.identity(), TenantCreate(name="Fixture"), "entity-tenant-bootstrap"
        )
        body = EntityCreate.model_validate(
            {
                "kind": "topic",
                "label": "Topic",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        entity = self.entities.create(self.identity(), tenant.id, body, KEY)
        self.assertEqual(self.entities.get(self.identity(), tenant.id, entity.id), entity)
        member = next(
            d
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{tenant.id}/memberships/")
        )
        member["authorization_operation_id"] = str(uuid4())
        self.fails(
            lambda: self.entities.create(self.identity(), tenant.id, body, KEY),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_crud_wire_contract_replay_patch_and_atomic_receipt(self) -> None:
        self.store.retry = lambda: None
        first = self.create_entity()
        self.assertEqual(first.status_code, 201, first.text)
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        validate = Draft202012Validator(
            {"$ref": "#/components/schemas/Entity", "components": spec["components"]}
        ).validate
        validate(first.json())
        self.assertEqual(first.headers["etag"], '"1"')
        self.assertEqual(self.create_entity().json(), first.json())
        self.assertEqual(self.create_entity(label="different").status_code, 409)
        identity = first.json()["id"]
        url = self.url + "/" + identity
        updated = self.client.patch(
            url,
            json={"aliases": ["灯り"]},
            headers={"Idempotency-Key": "entity-patch-key-001", "If-Match": '"1"'},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.headers["etag"], '"2"')
        validate(updated.json())
        self.assertEqual(self.client.get(url).json(), updated.json())
        self.assertEqual(
            self.client.patch(
                url,
                json={"label": "new"},
                headers={"Idempotency-Key": "entity-patch-key-002", "If-Match": '"1"'},
            ).status_code,
            412,
        )
        stored = self.store.data[f"tenants/{TENANT}/entities/{identity}"]
        self.assertEqual(stored["created_by"], USER)
        self.assertEqual(len([p for p in self.store.data if "/idempotency_records/" in p]), 2)
        # Canonical shared records may have no owner. Omit that optional field while
        # retaining the required null next_cursor on the final page.
        stored["owner_user_id"] = None
        page = self.client.get(self.url)
        Draft202012Validator(
            {"$ref": "#/components/schemas/EntityPage", "components": spec["components"]}
        ).validate(page.json())
        self.assertNotIn("owner_user_id", page.json()["items"][0])

    def test_shared_null_owner_patch_preserves_storage_and_http_replay_contract(self) -> None:
        created = self.create_entity().json()
        path = f"tenants/{TENANT}/entities/{created['id']}"
        self.store.data[path]["owner_user_id"] = None
        Entity.model_validate(self.store.data[path])
        url = self.url + "/" + created["id"]
        self.assertEqual(self.client.get(url).status_code, 200)
        headers = {"Idempotency-Key": "review-owner-none-patch", "If-Match": '"1"'}
        updated = self.client.patch(url, json={"label": "Changed"}, headers=headers)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual((updated.json()["label"], updated.json()["version"]), ("Changed", 2))
        self.assertEqual(updated.headers["etag"], '"2"')
        self.assertNotIn("owner_user_id", updated.json())
        for response in (
            self.client.get(url),
            self.client.patch(url, json={"label": "Changed"}, headers=headers),
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), updated.json())
            self.assertEqual(response.headers["etag"], '"2"')
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json(), {"items": [updated.json()], "next_cursor": None})
        self.assertIn("owner_user_id", self.store.data[path])
        canonical = Entity.model_validate(self.store.data[path])
        self.assertIsNone(canonical.owner_user_id)
        self.assertEqual(canonical.version, 2)
        self.assertEqual(len([p for p in self.store.data if "/idempotency_records/" in p]), 2)

    def test_same_names_aliases_and_two_tenants_are_candidates_never_merge(self) -> None:
        ids = [
            self.create_entity(key=f"entity-room-key-{i:03}", aliases=[name]).json()["id"]
            for i, name in enumerate(("living room light", "bedroom light"))
        ]
        results = self.client.get(self.url, params={"query": "照明"}).json()
        self.assertEqual({v["id"] for v in results["items"]}, set(ids))
        self.assertIsNone(results["next_cursor"])
        self.assertEqual(
            len(self.client.get(self.url, params={"query": "BEDROOM"}).json()["items"]), 1
        )
        for i in range(2):
            self.assertEqual(
                self.create_entity(
                    key=f"entity-person-{i:03}", kind="person", label="Alex"
                ).status_code,
                201,
            )
        self.assertEqual(
            len(self.client.get(self.url, params={"query": "Alex"}).json()["items"]), 2
        )
        self.assertEqual(self.client.get(f"/v1/tenants/{OTHER}/entities/{ids[0]}").status_code, 404)
        self.assertEqual(self.entities.list(self.identity(), OTHER).items, [])

    def test_hidden_private_changes_do_not_affect_shared_cursor_or_alias_search(self) -> None:
        for i in range(2):
            self.create_entity(key=f"entity-shared-{i:03}")
        private = self.create_entity(
            key="entity-private-key",
            label="SECRET_LABEL",
            aliases=["SECRET_ALIAS"],
            visibility="private",
            display_policy="never",
        ).json()
        self.uid = FirebaseUid("other / opaque member")
        self.store.data[f"tenants/{TENANT}/memberships/other"] = {
            "tenant_id": TENANT,
            "user_id": self.uid,
            "role": "admin",
            "status": "active",
            "authorization_version": 1,
        }
        first = self.client.get(self.url, params={"limit": 1}).json()
        private_path = f"tenants/{TENANT}/entities/{private['id']}"
        self.store.data[private_path]["version"] += 1
        self.assertEqual(first, self.client.get(self.url, params={"limit": 1}).json())
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": first["next_cursor"]}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(self.url, params={"query": "SECRET"}).json(),
            {
                "items": [],
                "next_cursor": None,
            },
        )
        self.assertEqual(self.client.get(self.url + "/" + private["id"]).status_code, 404)

    def test_cursor_binds_query_limit_actor_current_authority_and_visible_versions(self) -> None:
        first = self.create_entity().json()
        self.create_entity(key="entity-second-key")
        cursor = self.client.get(self.url, params={"limit": 1}).json()["next_cursor"]
        params: dict[str, Any]
        parameters: tuple[dict[str, Any], ...] = (
            {"limit": 2},
            {"limit": 1, "query": "light"},
            {"limit": 1, "cursor": "tampered"},
        )
        for params in parameters:
            self.assertEqual(
                self.client.get(self.url, params={"cursor": cursor, **params}).status_code, 400
            )
        self.store.data[f"tenants/{TENANT}/entities/{first['id']}"]["version"] += 1
        self.assertEqual(
            self.client.get(self.url, params={"limit": 1, "cursor": cursor}).status_code, 400
        )
        self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["status"] = "revoked"
        self.assertEqual(
            self.client.get(self.url, params={"limit": 1, "cursor": cursor}).status_code, 404
        )
        self.assertEqual(self.create_entity().status_code, 404)

    def test_request_authority_external_claims_and_patch_shape_rejected(self) -> None:
        extra: dict[str, Any]
        for extra in (
            {"external_refs": []},
            {"owner_user_id": "other"},
            {"kind": "device"},
            {"label": "x" * 201},
            {"aliases": ["a"] * 11},
        ):
            self.assertEqual(self.create_entity(**extra).status_code, 422)
        entity = self.create_entity().json()["id"]
        body: dict[str, Any]
        for body in (
            {},
            {"label": None},
            {"kind": "topic"},
            {"visibility": "shared"},
            {"external_refs": []},
            {"version": 4},
            {"aliases": None},
        ):
            self.assertEqual(self.client.patch(self.url + "/" + entity, json=body).status_code, 422)
        self.assertEqual(
            self.client.patch(self.url + "/" + entity, json={"label": "new"}).status_code, 428
        )
        self.assertEqual(self.client.get(self.url, params={"query": "x" * 201}).status_code, 422)
        self.assertEqual(
            self.client.get(self.url, headers={"X-Private-Session": str(uuid4())}).status_code, 400
        )
        self.assertEqual(
            self.client.get(
                self.url,
                headers=[("X-Private-Session", str(uuid4())), ("X-Private-Session", str(uuid4()))],
            ).status_code,
            400,
        )

    def test_device_private_person_creation_edit_no_idle_extension_and_control_allowlists(
        self,
    ) -> None:
        appliance = self.create_entity().json()
        credentials = self.device([ResourceId(appliance["id"])])
        session = self.session(credentials.device_id)
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + credentials.access_token,
            "X-Private-Session": session,
            "Idempotency-Key": "entity-device-create",
        }
        session_path = f"tenants/{TENANT}/private_sessions/{session}"
        before = copy.deepcopy(self.store.data[session_path])
        result = self.client.post(
            self.url,
            json={
                "kind": "person",
                "label": "Alex",
                "visibility": "private",
                "display_policy": "authenticated_session",
            },
            headers=headers,
        )
        self.assertEqual(result.status_code, 201, result.text)
        entity = result.json()["id"]
        self.assertEqual(self.store.data[f"tenants/{TENANT}/entities/{entity}"]["created_by"], USER)
        self.assertEqual(
            self.store.data[f"tenants/{TENANT}/entities/{entity}"]["created_via_device_id"],
            credentials.device_id,
        )
        self.assertEqual(
            self.client.patch(
                self.url + "/" + entity,
                json={"label": "Alex 2"},
                headers={**headers, "Idempotency-Key": "entity-device-patch", "If-Match": '"1"'},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 200)
        self.assertEqual(self.store.data[session_path], before)
        for kind in ("appliance", "room"):
            self.assertEqual(
                self.client.post(
                    self.url,
                    json={
                        "kind": kind,
                        "label": "New",
                        "visibility": "shared",
                        "display_policy": "shared_display",
                    },
                    headers={**headers, "Idempotency-Key": "entity-device-new-" + kind},
                ).status_code,
                404,
            )
        self.assertEqual(
            self.client.get(self.url + "/" + credentials.device_id, headers=headers).status_code,
            404,
        )
        self.now += timedelta(seconds=60)
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 404)

    def test_unknown_speaker_cannot_write_and_device_family_revocation_is_current(self) -> None:
        credentials = self.device()
        identity = self.credentials.authenticate(credentials.access_token)
        body = EntityCreate.model_validate(
            {
                "kind": "topic",
                "label": "Topic",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        self.fails(
            lambda: self.entities.create(identity, TENANT, body, KEY), ErrorCode.OPERATION_FORBIDDEN
        )
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        self.fails(lambda: self.entities.list(identity, TENANT), ErrorCode.AUTHENTICATION_REQUIRED)

    def test_atomic_failure_retries_and_expiry_during_read_and_after_commit(self) -> None:
        self.store.fail_commit = True
        before = copy.deepcopy(self.store.data)
        body = EntityCreate.model_validate(
            {
                "kind": "topic",
                "label": "Topic",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        with self.assertRaises(RuntimeError):
            self.entities.create(self.identity(), TENANT, body, KEY)
        self.assertEqual(self.store.data, before)
        self.store.fail_commit = False
        identity = self.identity()
        self.store.after_commit = lambda: setattr(self, "now", identity.expires_at)
        self.fails(
            lambda: self.entities.create(identity, TENANT, body, KEY), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.assertEqual(len([p for p in self.store.data if "/entities/" in p]), 1)
        self.assertEqual(len([p for p in self.store.data if "/idempotency_records/" in p]), 1)
        self.store.after_commit = lambda: None
        self.now = NOW
        entity = self.entities.create(identity, TENANT, body, KEY)
        self.store.after_get = lambda p: (
            setattr(self, "now", identity.expires_at) if p.endswith(entity.id) else None
        )
        self.fails(
            lambda: self.entities.get(identity, TENANT, entity.id), ErrorCode.RESOURCE_NOT_FOUND
        )

    def test_provider_dedup_tenant_integration_namespace_and_no_label_merge(self) -> None:
        plain = self.create_entity().json()
        ref = self.inventory()
        first = self.entities.register(self.identity(), TENANT, ref, KEY)
        again = self.entities.register(self.identity(), TENANT, ref, "external-second-key")
        self.assertEqual(first, again)
        self.assertNotEqual(first.id, plain["id"])
        other_ref = self.inventory(tenant=OTHER)
        other = self.entities.register(self.identity(), OTHER, other_ref, KEY)
        self.assertNotEqual(first.id, other.id)
        self.assertEqual(first.external_refs, other.external_refs)
        self.assertEqual(len([p for p in self.store.data if "/entity_external_ids/" in p]), 2)

    def test_external_replay_checks_integration_inventory_private_tombstone_and_epoch(self) -> None:
        ref = self.inventory(visibility="private", display_policy="never")
        first = self.entities.register(self.identity(), TENANT, ref, KEY)
        snapshot = copy.deepcopy(self.store.data)
        for changes in (
            {"deleted": True},
            {"suppress_reuse": True},
            {"deletion_epoch": 1},
            {"kind": "person"},
        ):
            self.store.data = copy.deepcopy(snapshot)
            self.store.data[f"tenants/{TENANT}/entities/{first.id}"].update(changes)
            for key in (KEY, "external-another-key"):
                self.fails(
                    partial(self.entities.register, self.identity(), TENANT, ref, key),
                    ErrorCode.RESOURCE_NOT_FOUND,
                )
        self.store.data = copy.deepcopy(snapshot)
        self.inventory()  # A later shared snapshot cannot implicitly publish the old private entity.
        self.fails(
            lambda: self.entities.register(self.identity(), TENANT, ref, "external-share-key"),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.store.data = copy.deepcopy(snapshot)
        self.store.data[f"tenants/{TENANT}/integrations/{INTEGRATION}"]["status"] = "revoked"
        self.fails(
            lambda: self.entities.register(self.identity(), TENANT, ref, KEY),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_external_legacy_missing_index_adoption_and_private_nonowner_denial(self) -> None:
        ref = self.inventory(visibility="private", display_policy="never")
        first = self.entities.register(self.identity(), TENANT, ref, KEY)
        index = f"tenants/{TENANT}/entity_external_ids/{external_key(TENANT, ref)}"
        del self.store.data[index]
        self.assertEqual(
            self.entities.register(self.identity(), TENANT, ref, "legacy-adopt-key").id, first.id
        )
        self.uid = FirebaseUid("other-owner")
        self.store.data[f"tenants/{TENANT}/memberships/other"] = {
            "tenant_id": TENANT,
            "user_id": self.uid,
            "role": "owner",
            "status": "active",
            "authorization_version": 1,
        }
        self.fails(
            lambda: self.entities.register(self.identity(), TENANT, ref, "legacy-other-key"),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(len([p for p in self.store.data if "/entities/" in p]), 1)

    def test_infrastructure_policy_failure_is_not_an_empty_candidate_page(self) -> None:
        self.create_entity()
        with patch(
            "brainoid_memory.repository.EntityTransaction.get", side_effect=PolicyUnavailable()
        ):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_revocation_on_transaction_retry_aborts_both_entity_and_receipt(self) -> None:
        def revoke() -> None:
            self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["status"] = "revoked"

        self.store.retry = revoke
        self.assertEqual(self.create_entity().status_code, 404)
        self.assertFalse(
            any("/entities/" in p or "/idempotency_records/" in p for p in self.store.data)
        )

    def test_final_read_expiry_aborts_patch_and_its_receipt(self) -> None:
        result = self.create_entity().json()
        path = f"tenants/{TENANT}/entities/{result['id']}"
        before = copy.deepcopy(self.store.data)
        self.store.after_get = lambda p: (
            setattr(self, "now", NOW + timedelta(hours=1)) if p == path else None
        )
        self.fails(
            lambda: self.entities.update(
                self.identity(),
                TENANT,
                ResourceId(result["id"]),
                EntityPatch(label="late"),
                "entity-expired-patch",
                '"1"',
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.store.data, before)

    def test_old_mutation_receipts_do_not_authorize_another_deletion_epoch(self) -> None:
        result = self.create_entity().json()
        entity = ResourceId(result["id"])
        self.entities.update(
            self.identity(),
            TENANT,
            entity,
            EntityPatch(label="updated"),
            "entity-epoch-patch",
            '"1"',
        )
        self.store.data[f"tenants/{TENANT}/entities/{entity}"]["deletion_epoch"] = 1
        self.assertEqual(self.create_entity().status_code, 403)
        self.fails(
            lambda: self.entities.update(
                self.identity(),
                TENANT,
                entity,
                EntityPatch(label="updated"),
                "entity-epoch-patch",
                '"1"',
            ),
            ErrorCode.OPERATION_FORBIDDEN,
        )

    def test_device_family_and_session_expiry_after_commit_do_not_return_content(self) -> None:
        credentials = self.device()
        identity = self.credentials.authenticate(credentials.access_token)
        session = self.session(credentials.device_id)
        body = EntityCreate.model_validate(
            {
                "kind": "topic",
                "label": "private topic",
                "visibility": "private",
                "display_policy": "authenticated_session",
            }
        )
        family = "device_credential_families/" + identity.family_id
        self.store.data[family]["expires_at"] = NOW + timedelta(seconds=30)
        self.store.after_commit = lambda: setattr(self, "now", NOW + timedelta(seconds=30))
        self.fails(
            lambda: self.entities.create(identity, TENANT, body, KEY, session),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(len([p for p in self.store.data if "/entities/" in p]), 1)
        self.store.after_commit = lambda: None
        self.now = NOW
        self.store.data[family]["expires_at"] = NOW + timedelta(days=1)
        self.store.after_commit = lambda: setattr(self, "now", NOW + timedelta(seconds=60))
        self.fails(
            lambda: self.entities.create(identity, TENANT, body, "entity-session-expiry", session),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(len([p for p in self.store.data if "/entities/" in p]), 2)

    def test_external_registration_checks_current_scope_provider_owner_and_index(self) -> None:
        ref = self.inventory()
        first = self.entities.register(self.identity(), TENANT, ref, KEY)
        snapshot = copy.deepcopy(self.store.data)
        integration = f"tenants/{TENANT}/integrations/{INTEGRATION}"
        change: dict[str, Any]
        for change in (
            {"tenant_id": OTHER},
            {"provider": "google_calendar"},
            {"owner_user_id": "another-user"},
            {"id": str(uuid4())},
        ):
            self.store.data = copy.deepcopy(snapshot)
            self.store.data[integration].update(change)
            self.fails(
                lambda: self.entities.register(self.identity(), TENANT, ref, KEY),
                ErrorCode.RESOURCE_NOT_FOUND,
            )
        self.store.data = copy.deepcopy(snapshot)
        self.store.data[f"tenants/{TENANT}/entity_external_ids/{external_key(TENANT, ref)}"][
            "entity_id"
        ] = str(uuid4())
        self.fails(
            lambda: self.entities.register(self.identity(), TENANT, ref, KEY),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.store.data = copy.deepcopy(snapshot)
        self.store.data[f"tenants/{TENANT}/provider_inventory/{external_key(TENANT, ref)}"][
            "kind"
        ] = "person"
        self.fails(
            lambda: self.entities.register(self.identity(), TENANT, ref, "external-person-key"),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(len([p for p in self.store.data if "/entities/" in p]), 1)
        self.assertIn(first.id, str(self.store.data))
