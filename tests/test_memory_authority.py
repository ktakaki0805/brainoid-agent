"""MEM-01 review regressions: Pi authorship and deadlines after the final I/O."""

import copy
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta

from brainoid_contracts import DeviceActorContext, FirebaseUid, TenantContext
from brainoid_memory import CanonicalSources, EntityPatch, MemoryRepository
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    BEDROOM,
    BEDROOM_LIGHT,
    CONVERSATION,
    FACT,
    LIGHT,
    NOW,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    AuthenticatedCaller,
    GraphStore,
    authority_data,
    context,
    device_session,
    entity_values,
    fact_values,
)


class MemoryAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = GraphStore()
        self.store.data = authority_data()
        self.store.data[f"tenants/{TENANT}/memberships/safe-key"]["role"] = "member"
        self.caller = AuthenticatedCaller()
        self.repo = MemoryRepository(
            self.store,
            authorizer=self.caller,
            sources=CanonicalSources(),
            ledger=ControlLedger(MemoryObjects()),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.repo.create_entity(context(), LIGHT, entity_values())
        self.repo.create_entity(context(), PERSON, entity_values(kind="person"))
        self.subject, self.device_context = device_session(self.store)
        self.entity_path = f"tenants/{TENANT}/entities/{LIGHT}"
        self.fact_path = f"tenants/{TENANT}/facts/{FACT}"
        self.source_path = f"tenants/{TENANT}/notes/{SOURCE}"
        self.session_path = f"tenants/{TENANT}/private_sessions/{BEDROOM}"

    def at_final_read(self, path: str, count: int, at: datetime) -> None:
        reads = 0

        def after_get(current: str) -> None:
            nonlocal reads
            if current == path:
                reads += 1
                if reads == count:
                    self.now = at

        self.store.after_get = after_get

    def test_pi_shared_fact_creator_can_retract_from_session_or_phone(self) -> None:
        self.caller.subject = self.subject
        created = self.repo.create_fact(self.device_context, FACT, fact_values())
        self.assertEqual(created.created_by, USER)
        self.assertEqual(created.created_via_device_id, CONVERSATION)
        original = copy.deepcopy(self.store.data[self.fact_path])
        for subject, ctx in (
            (self.subject, self.device_context),
            (UserSubject(user_id=USER), context()),
        ):
            with self.subTest(subject=subject):
                self.store.data[self.fact_path] = copy.deepcopy(original)
                self.caller.subject = subject
                result = self.repo.update_fact(
                    ctx, FACT, status="retracted", expected_version=1, expected_deletion_epoch=0
                )
                self.assertEqual((result.status, result.version), ("retracted", 2))
                self.assertEqual(result.created_via_device_id, CONVERSATION)

    def test_other_member_cannot_retract_pi_shared_fact(self) -> None:
        self.caller.subject = self.subject
        self.repo.create_fact(self.device_context, FACT, fact_values())
        other = FirebaseUid("unrelated-member")
        self.store.data[f"tenants/{TENANT}/memberships/other"] = {
            "tenant_id": TENANT,
            "user_id": other,
            "role": "member",
            "status": "active",
            "authorization_version": 1,
        }
        self.caller.subject = UserSubject(user_id=other)
        with self.assertRaises(PolicyDenied):
            self.repo.update_fact(
                context(user=other),
                FACT,
                status="retracted",
                expected_version=1,
                expected_deletion_epoch=0,
            )
        self.assertEqual(self.store.data[self.fact_path]["status"], "confirmed")

    def test_pi_entity_keeps_actor_audit_and_responsible_member_can_edit(self) -> None:
        self.caller.subject = self.subject
        entity = self.repo.create_entity(self.device_context, BEDROOM_LIGHT, entity_values())
        self.assertEqual((entity.created_by, entity.created_via_device_id), (USER, CONVERSATION))
        self.repo.update_entity(
            self.device_context,
            BEDROOM_LIGHT,
            EntityPatch(label="From Pi"),
            expected_version=1,
            expected_deletion_epoch=0,
        )
        self.caller.subject = UserSubject(user_id=USER)
        updated = self.repo.update_entity(
            context(),
            BEDROOM_LIGHT,
            EntityPatch(label="From phone"),
            expected_version=2,
            expected_deletion_epoch=0,
        )
        self.assertEqual(updated.version, 3)

    def test_model_fact_valid_to_is_checked_after_last_source_read(self) -> None:
        end = NOW + timedelta(seconds=60)
        self.repo.create_fact(context(), FACT, fact_values(valid_to=end))
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                self.now = NOW
                self.at_final_read(self.source_path, 3, end + timedelta(microseconds=delta))
                if delta < 0:
                    self.assertEqual(self.repo.get_fact(context(), FACT, for_model=True).id, FACT)
                else:
                    with self.assertRaises(PolicyDenied):
                        self.repo.get_fact(context(), FACT, for_model=True)

    def test_canonical_session_deadlines_bound_every_read_and_write(self) -> None:
        self.repo.create_fact(context(), FACT, fact_values())
        self.caller.subject = self.subject
        actor = self.device_context.actor
        assert isinstance(actor, DeviceActorContext) and actor.private_session is not None
        # A forged context claims much later expiry; only the canonical session counts.
        ctx = TenantContext(
            tenant_id=TENANT,
            actor=actor.model_copy(
                update={
                    "private_session": actor.private_session.model_copy(
                        update={
                            "expires_at": NOW + timedelta(days=1),
                            "idle_expires_at": NOW + timedelta(days=1),
                        }
                    ),
                }
            ),
        )
        paths: dict[str, tuple[str, int, Callable[[], object]]] = {
            "get_entity": (self.entity_path, 2, lambda: self.repo.get_entity(ctx, LIGHT)),
            "get_fact": (self.source_path, 3, lambda: self.repo.get_fact(ctx, FACT)),
            "create_entity": (
                f"tenants/{TENANT}/entities/{BEDROOM_LIGHT}",
                1,
                lambda: self.repo.create_entity(ctx, BEDROOM_LIGHT, entity_values()),
            ),
            "create_fact": (
                f"tenants/{TENANT}/facts/{BEDROOM_LIGHT}",
                1,
                lambda: self.repo.create_fact(ctx, BEDROOM_LIGHT, fact_values()),
            ),
            "update_entity": (
                self.entity_path,
                2,
                lambda: self.repo.update_entity(
                    ctx,
                    LIGHT,
                    EntityPatch(label="Updated"),
                    expected_version=1,
                    expected_deletion_epoch=0,
                ),
            ),
            "update_fact": (
                self.source_path,
                3,
                lambda: self.repo.update_fact(
                    ctx, FACT, status="retracted", expected_version=1, expected_deletion_epoch=0
                ),
            ),
        }
        original = copy.deepcopy(self.store.data)
        end = NOW + timedelta(seconds=60)
        for deadline in ("expires_at", "idle_expires_at"):
            for operation, (path, count, call) in paths.items():
                for delta in (-1, 0, 1):
                    with self.subTest(deadline=deadline, operation=operation, delta=delta):
                        self.now = NOW
                        self.store.data = copy.deepcopy(original)
                        self.store.data[self.session_path].update(
                            expires_at=NOW + timedelta(hours=1),
                            idle_expires_at=NOW + timedelta(hours=1),
                        )
                        self.store.data[self.session_path][deadline] = end
                        before = copy.deepcopy(self.store.data)
                        self.at_final_read(path, count, end + timedelta(microseconds=delta))
                        if delta < 0:
                            call()
                        else:
                            with self.assertRaises(PolicyDenied):
                                call()
                            self.assertEqual(self.store.data, before)

    def test_verified_credential_deadline_bounds_reads_and_writes(self) -> None:
        end = NOW + timedelta(seconds=30)
        self.caller.valid_to = end
        original = copy.deepcopy(self.store.data)
        for subject, ctx in (
            (UserSubject(user_id=USER), context()),
            (self.subject, self.device_context),
        ):
            self.caller.subject = subject
            for write in (False, True):
                for delta in (-1, 0, 1):
                    with self.subTest(subject=subject, write=write, delta=delta):
                        self.now = NOW
                        self.store.data = copy.deepcopy(original)
                        self.at_final_read(self.entity_path, 2, end + timedelta(microseconds=delta))

                        def call(write: bool = write, ctx: TenantContext = ctx) -> object:
                            if write:
                                return self.repo.update_entity(
                                    ctx,
                                    LIGHT,
                                    EntityPatch(label="Changed"),
                                    expected_version=1,
                                    expected_deletion_epoch=0,
                                )
                            return self.repo.get_entity(ctx, LIGHT)

                        if delta < 0:
                            call()
                        else:
                            with self.assertRaises(PolicyDenied):
                                call()
                            self.assertEqual(self.store.data, original)

    def test_expiry_while_transaction_response_is_pending_never_releases_content(self) -> None:
        end = NOW + timedelta(seconds=60)
        self.repo.create_fact(context(), FACT, fact_values(valid_to=end))

        def delayed_response() -> None:
            self.now = end

        self.store.after_commit = delayed_response
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(), FACT, for_model=True)
        self.now = NOW
        self.caller.subject = self.subject
        with self.assertRaises(PolicyDenied):
            self.repo.update_entity(
                self.device_context,
                LIGHT,
                EntityPatch(label="Authorized write"),
                expected_version=1,
                expected_deletion_epoch=0,
            )
        # The write was authorized and staged before expiry; a late response denial is
        # not proof of rollback. Subsequent work must reconcile this committed version.
        self.assertEqual(self.store.data[self.entity_path]["version"], 2)

    def test_caller_validity_start_and_expiry_are_exclusive_at_the_upper_bound(self) -> None:
        self.caller.valid_from = NOW + timedelta(seconds=1)
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(context(), LIGHT)
        self.now = self.caller.valid_from
        self.assertEqual(self.repo.get_entity(context(), LIGHT).id, LIGHT)
        self.now = self.caller.valid_to
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(context(), LIGHT)
