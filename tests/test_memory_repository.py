"""MEM-01 isolation, provenance, optimistic concurrency and retry authorization."""

import copy
import unittest
from typing import Any

from brainoid_contracts import DeviceActorContext, FirebaseUid, TenantContext
from brainoid_memory import AlreadyExists, EntityPatch, MemoryRepository, VersionConflict
from brainoid_memory.sources import CanonicalSources, ResolvedSource
from brainoid_policy.control_ledger import Change, ControlLedger, MemberTarget
from brainoid_policy.models import DeviceSubject, ResourceKind, ResourceRef, Role, UserSubject
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    BEDROOM,
    BEDROOM_LIGHT,
    BEDROOM_LOCATION,
    CONVERSATION,
    FACT,
    LIGHT,
    LIVING_LOCATION,
    LIVING_ROOM,
    NOW,
    OTHER,
    OTHER_PERSON,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    AuthenticatedCaller,
    GraphStore,
    authority_data,
    context,
    entity_values,
    fact_values,
    repository,
)
from pydantic import ValidationError


class MemoryRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = GraphStore()
        self.store.data = authority_data()
        self.caller = AuthenticatedCaller()
        self.repo = repository(self.store, self.caller)
        self.context = context()
        self.repo.create_entity(self.context, LIGHT, entity_values())
        self.repo.create_entity(self.context, PERSON, entity_values(kind="person", label="Alex"))
        self.source_path = f"tenants/{TENANT}/notes/{SOURCE}"
        self.entity_path = f"tenants/{TENANT}/entities/{LIGHT}"
        self.fact_path = f"tenants/{TENANT}/facts/{FACT}"
        self.member_path = f"tenants/{TENANT}/memberships/safe-key"

    def save(self, **changes: object) -> Any:
        return self.repo.create_fact(self.context, FACT, fact_values(**changes))

    def test_t009_same_names_are_independent_ids_and_tenants(self) -> None:
        self.repo.create_entity(self.context, BEDROOM_LIGHT, entity_values())
        for room, label, light, location in (
            (LIVING_ROOM, "Living room", LIGHT, LIVING_LOCATION),
            (BEDROOM, "Bedroom", BEDROOM_LIGHT, BEDROOM_LOCATION),
        ):
            self.repo.create_entity(self.context, room, entity_values(kind="room", label=label))
            self.repo.create_fact(
                self.context,
                location,
                fact_values(
                    subject_entity_id=light,
                    predicate="located_in",
                    object_entity_id=room,
                    qualifiers={},
                ),
            )
            self.assertEqual(self.repo.get_fact(self.context, location).object_entity_id, room)
        self.repo.create_entity(
            self.context, OTHER_PERSON, entity_values(kind="person", label="Alex")
        )
        for first, second in ((LIGHT, BEDROOM_LIGHT), (PERSON, OTHER_PERSON)):
            self.assertEqual(
                self.repo.get_entity(self.context, first).label,
                self.repo.get_entity(self.context, second).label,
            )
            self.assertNotEqual(first, second)
        other = context(OTHER)
        self.repo.create_entity(other, LIGHT, entity_values(label="OTHER_TENANT_LIGHT"))
        self.assertEqual(self.repo.get_entity(self.context, LIGHT).label, "照明")
        self.assertEqual(self.repo.get_entity(other, LIGHT).label, "OTHER_TENANT_LIGHT")
        del self.store.data[f"tenants/{OTHER}/memberships/safe-key"]
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(other, LIGHT)

    def test_no_tenant_no_path_no_identity_spoof(self) -> None:
        bad: Any = None
        with self.assertRaises(TypeError):
            self.repo.get_entity(bad, LIGHT)
        bad = f"tenants/{OTHER}/entities/{LIGHT}"
        with self.assertRaises(ValidationError):
            self.repo.get_entity(self.context, bad)
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(context(user=FirebaseUid("someone-else")), LIGHT)
        self.store.data[self.entity_path]["tenant_id"] = OTHER
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, LIGHT)

    def test_invalid_constructed_values_are_rechecked_before_transaction(self) -> None:
        invalid = fact_values().model_copy(update={"value": "both"})
        before, calls = copy.deepcopy(self.store.data), self.store.calls
        with self.assertRaises(ValidationError):
            self.repo.create_fact(self.context, FACT, invalid)
        self.assertEqual(self.store.calls, calls)
        self.assertEqual(self.store.data, before)

    def test_missing_storage_metadata_fails_closed(self) -> None:
        original = copy.deepcopy(self.store.data[self.entity_path])
        for field in ("deletion_epoch", "schema_version", "deleted", "suppress_reuse"):
            self.store.data[self.entity_path] = {
                key: value for key, value in original.items() if key != field
            }
            with self.subTest(field=field), self.assertRaises(PolicyDenied):
                self.repo.get_entity(self.context, LIGHT)
        self.store.data[self.entity_path] = {**original, "schema_version": True}
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, LIGHT)

    def test_duplicate_create_never_discloses_other_users_private_entity(self) -> None:
        with self.assertRaises(AlreadyExists):
            self.repo.create_entity(self.context, LIGHT, entity_values())
        self.store.data[self.entity_path].update(
            visibility="private", display_policy="never", owner_user_id="other-user"
        )
        with self.assertRaises(PolicyDenied):
            self.repo.create_entity(self.context, LIGHT, entity_values())

    def test_retraction_cannot_return_fact_after_source_authority_is_lost(self) -> None:
        self.save()
        self.store.data[self.source_path].update(
            visibility="private", display_policy="never", owner_user_id="other-user"
        )
        with self.assertRaises(PolicyDenied):
            self.repo.update_fact(
                self.context,
                FACT,
                status="retracted",
                expected_version=1,
                expected_deletion_epoch=0,
            )
        self.assertEqual(self.store.data[self.fact_path]["version"], 1)

    def test_save_get_update_and_version_epoch_preconditions(self) -> None:
        saved = self.save()
        self.assertEqual(self.repo.get_fact(self.context, FACT, for_model=True), saved)
        updated = self.repo.update_entity(
            self.context,
            LIGHT,
            EntityPatch(label="Bedroom"),
            expected_version=1,
            expected_deletion_epoch=0,
        )
        self.assertEqual(updated.version, 2)
        for version, epoch in ((1, 0), (2, 1)):
            with self.assertRaises(VersionConflict):
                self.repo.update_entity(
                    self.context,
                    LIGHT,
                    EntityPatch(label="Wrong"),
                    expected_version=version,
                    expected_deletion_epoch=epoch,
                )
        with self.assertRaises(ValidationError):
            self.repo.update_entity(
                self.context,
                LIGHT,
                EntityPatch(label="Wrong"),
                expected_version=True,
                expected_deletion_epoch=0,
            )
        self.assertEqual(self.repo.get_entity(self.context, LIGHT), updated)

    def test_inferred_candidate_cannot_be_model_fact_but_can_be_retracted(self) -> None:
        self.save(origin="inferred", status="candidate")
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        result = self.repo.update_fact(
            self.context, FACT, status="retracted", expected_version=1, expected_deletion_epoch=0
        )
        self.assertEqual(
            (result.origin, result.status, result.version), ("inferred", "retracted", 2)
        )
        with self.assertRaises(VersionConflict):
            self.repo.update_fact(
                self.context,
                FACT,
                status="retracted",
                expected_version=1,
                expected_deletion_epoch=0,
            )

    def test_missing_stale_deleted_or_suppressed_sources_never_commit(self) -> None:
        original = copy.deepcopy(self.store.data[self.source_path])
        for change in (
            {"version": 2},
            {"deleted": True},
            {"suppress_reuse": True},
            {"deletion_epoch": 1},
            {"tenant_id": OTHER},
            {"version": True},
        ):
            self.store.data[self.source_path] = {**original, **change}
            with self.subTest(change=change), self.assertRaises(PolicyDenied):
                self.save()
            self.assertNotIn(self.fact_path, self.store.data)
        del self.store.data[self.source_path]
        with self.assertRaises(PolicyDenied):
            self.save()

    def test_shared_fact_rejects_private_subject_object_or_source(self) -> None:
        for path in (self.entity_path, f"tenants/{TENANT}/entities/{PERSON}", self.source_path):
            original = copy.deepcopy(self.store.data[path])
            self.store.data[path].update(visibility="private", display_policy="never")
            with self.subTest(path=path), self.assertRaises(PolicyDenied):
                self.save()
            private = self.save(visibility="private", display_policy="never")
            self.assertEqual(private.owner_user_id, USER)
            del self.store.data[self.fact_path]
            self.store.data[path] = original

    def test_admin_cannot_read_other_private_memory(self) -> None:
        self.save(visibility="private", display_policy="never")
        admin = FirebaseUid("fixture-admin")
        self.store.data[f"tenants/{TENANT}/memberships/admin"] = {
            "tenant_id": TENANT,
            "user_id": admin,
            "role": "admin",
            "status": "active",
            "authorization_version": 1,
        }
        self.caller.subject = UserSubject(user_id=admin)
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(user=admin), FACT)

    def test_fact_cannot_widen_source_or_entity_display_policy(self) -> None:
        for path in (self.source_path, self.entity_path):
            original = copy.deepcopy(self.store.data[path])
            for display in ("never", "authenticated_session"):
                self.store.data[path] = {**original, "display_policy": display}
                with self.subTest(path=path, display=display), self.assertRaises(PolicyDenied):
                    self.save()
                saved = self.save(visibility="private", display_policy="never")
                self.assertEqual(saved.display_policy, "never")
                del self.store.data[self.fact_path]
            self.store.data[path] = {**original, "display_policy": "never"}
            with self.assertRaises(PolicyDenied):
                self.save(visibility="private", display_policy="authenticated_session")
            self.store.data[path] = original

    def test_source_reuse_is_distinct_from_owner_history_read(self) -> None:
        self.save(visibility="private", display_policy="never")
        self.store.data[self.source_path]["suppress_reuse"] = True
        self.assertEqual(self.repo.get_fact(self.context, FACT).id, FACT)
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        # Withdrawal remains possible after suppression; no model reuse is involved.
        self.repo.update_fact(
            self.context, FACT, status="retracted", expected_version=1, expected_deletion_epoch=0
        )

    def test_every_read_checks_credentials_membership_account_and_ledger(self) -> None:
        self.save()
        self.caller.active = False
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT)
        self.caller.active = True
        self.store.data[self.member_path]["status"] = "revoked"
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, LIGHT)
        self.store.data[self.member_path]["status"] = "active"
        self.store.data["users/safe-key"] = {
            "user_id": USER,
            "display_name": "Fixture",
            "timezone": "Asia/Tokyo",
            "status": "deleting",
            "version": 2,
        }
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, LIGHT)
        del self.store.data["users/safe-key"]
        ledger = ControlLedger(MemoryObjects())
        ledger.prepare(
            Change(
                operation_id=FACT,
                tenant_id=TENANT,
                expected_version=1,
                authorization_version=2,
                effect="deny",
                target=MemberTarget(user_id=USER, role=Role.OWNER, status="revoked"),
            )
        )
        repo = MemoryRepository(
            self.store,
            authorizer=self.caller,
            sources=CanonicalSources(),
            ledger=ledger,
            now=lambda: NOW,
            allow_untracked_bootstrap=True,
        )
        with self.assertRaises(PolicyDenied):
            repo.get_entity(self.context, LIGHT)

    def test_retry_rechecks_source_and_failure_does_not_commit(self) -> None:
        self.store.retry = lambda: self.store.data[self.source_path].update(suppress_reuse=True)
        with self.assertRaises(PolicyDenied):
            self.save()
        self.assertNotIn(self.fact_path, self.store.data)
        self.store.data[self.source_path]["suppress_reuse"] = False
        self.store.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.save()
        self.assertNotIn(self.fact_path, self.store.data)

    def test_entity_update_conflicts_with_concurrent_version_and_revocation(self) -> None:
        self.store.retry = lambda: self.store.data[self.entity_path].update(version=2)
        with self.assertRaises(VersionConflict):
            self.repo.update_entity(
                self.context,
                LIGHT,
                EntityPatch(label="Wrong"),
                expected_version=1,
                expected_deletion_epoch=0,
            )
        self.store.retry = lambda: self.store.data[self.member_path].update(status="revoked")
        with self.assertRaises(PolicyDenied):
            self.repo.update_entity(
                self.context,
                LIGHT,
                EntityPatch(label="Wrong"),
                expected_version=2,
                expected_deletion_epoch=0,
            )
        self.assertEqual(self.store.data[self.entity_path]["label"], "照明")

    def test_tombstones_and_epoch_cannot_be_reset_by_save_or_update(self) -> None:
        self.store.data[self.entity_path].update(deleted=True, deletion_epoch=1)
        with self.assertRaises(PolicyDenied):
            self.repo.create_entity(self.context, LIGHT, entity_values())
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, LIGHT)
        with self.assertRaises(PolicyDenied):
            self.repo.update_entity(
                self.context,
                LIGHT,
                EntityPatch(label="Wrong"),
                expected_version=1,
                expected_deletion_epoch=0,
            )

    def test_message_parent_is_server_resolved_and_import_fails_closed(self) -> None:
        from brainoid_contracts import ResourceId
        from brainoid_contracts.transactions import Transaction

        class Locator:
            def conversation_id(
                self, tx: Transaction, tenant_id: ResourceId, message_id: ResourceId
            ) -> ResourceId:
                return CONVERSATION

        parent = f"tenants/{TENANT}/conversations/{CONVERSATION}"
        self.store.data[parent] = {**self.store.data[self.source_path], "id": CONVERSATION}
        path = parent + f"/messages/{SOURCE}"
        self.store.data[path] = {
            **self.store.data[self.source_path],
            "conversation_id": CONVERSATION,
        }
        self.repo = MemoryRepository(
            self.store,
            authorizer=self.caller,
            sources=CanonicalSources(Locator()),
            ledger=ControlLedger(MemoryObjects()),
            now=lambda: NOW,
            allow_untracked_bootstrap=True,
        )
        self.save(source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}])
        del self.store.data[parent]["suppress_reuse"]
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        self.store.data[parent]["suppress_reuse"] = True
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        del self.store.data[self.fact_path]
        with self.assertRaises(PolicyDenied):
            self.save(source_refs=[{"kind": "import", "resource_id": SOURCE, "version": 1}])
        self.store.data[path]["conversation_id"] = OTHER
        with self.assertRaises(PolicyDenied):
            self.save(source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}])

    def test_resolver_cannot_authorize_cross_tenant_or_private_sources(self) -> None:
        from brainoid_contracts.transactions import Transaction
        from brainoid_memory import SourceRef

        class OtherTenant:
            def resolve(
                self, tx: Transaction, context: TenantContext, source: SourceRef
            ) -> ResolvedSource:
                return ResolvedSource(
                    source=source,
                    authority=ResourceRef(tenant_id=OTHER, kind=ResourceKind.NOTE, id=SOURCE),
                    deletion_epoch=0,
                    deleted=False,
                    suppress_reuse=False,
                )

        repo = MemoryRepository(
            self.store,
            authorizer=self.caller,
            sources=OtherTenant(),
            ledger=ControlLedger(MemoryObjects()),
            now=lambda: NOW,
            allow_untracked_bootstrap=True,
        )
        with self.assertRaises(PolicyDenied):
            repo.create_fact(self.context, FACT, fact_values())

    def test_device_never_and_entity_allowlists_are_enforced(self) -> None:
        self.save(visibility="private", display_policy="never")
        device = LIGHT
        self.store.data[f"tenants/{TENANT}/devices/{device}"] = {
            "id": device,
            "tenant_id": TENANT,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "allowed_scopes": ["shared", "memory.read"],
            "allowed_entity_ids": [LIGHT],
            "allowed_room_ids": [],
            "capabilities": ["display"],
        }
        self.caller.subject = DeviceSubject(device_id=device, connection_generation=1)
        device_context = TenantContext(
            tenant_id=TENANT,
            actor=DeviceActorContext(
                actor_id=device,
                allowed_scopes=frozenset({"memory.read", "memory.write"}),
                authorization_version=1,
            ),
        )
        self.assertEqual(self.repo.get_entity(device_context, LIGHT).id, LIGHT)
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(device_context, PERSON)
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(device_context, FACT)
