"""Table-driven IAM-02 boundaries using the production transaction metadata adapter."""

import copy
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import uuid4

from brainoid_api.auth.authorization import (
    authorized_scope,
    policy_call,
    transaction_policy,
    user_subject,
)
from brainoid_api.auth.context import AuthenticatedIdentity, select_tenant
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Visibility
from brainoid_policy.models import (
    Action,
    DeviceSubject,
    ManagementAction,
    ResourceKind,
    ResourceRef,
    Role,
    Subject,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied
from pydantic import ValidationError
from test_common_mutations import MemoryTransaction

from scripts.demo_emulator import load_fixture

NOW = datetime(2026, 10, 26, tzinfo=UTC)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {d["path"]: d["fields"] for d in load_fixture()["documents"]}
        self.tx = MemoryTransaction(self.data)
        self.time = NOW
        self.policy = transaction_policy(self.tx, now=lambda: self.time)
        self.tenant = ResourceId(next(iter(self.data)).split("/")[1])
        self.other_tenant = ResourceId(
            next(
                p.split("/")[1]
                for p in self.data
                if p.startswith("tenants/") and p.split("/")[1] != self.tenant
            )
        )
        self.owner = UserSubject(user_id=FirebaseUid("demo-a-owner"))
        self.admin = UserSubject(user_id=FirebaseUid("demo-a-admin"))
        self.member = UserSubject(user_id=FirebaseUid("demo-a-member"))
        self.private = self.ref(ResourceKind.NOTE, "private")
        self.shared = self.ref(ResourceKind.NOTE, "shared")
        device = next(
            d for p, d in self.data.items() if "/devices/" in p and d["status"] == "active"
        )
        self.device_path = f"tenants/{self.tenant}/devices/{device['id']}"
        self.device = DeviceSubject(device_id=ResourceId(device["id"]), connection_generation=1)
        self.session_id = ResourceId(str(uuid4()))
        self.session_path = f"tenants/{self.tenant}/private_sessions/{self.session_id}"
        self.tx.data[self.session_path] = {
            "id": self.session_id,
            "tenant_id": self.tenant,
            "device_id": self.device.device_id,
            "user_id": self.member.user_id,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "allowed_scopes": ["conversation", "memory.read", "memory.write", "notes"],
            "expires_at": NOW + timedelta(minutes=5),
            "idle_expires_at": NOW + timedelta(seconds=60),
        }
        self.personal = self.device.model_copy(update={"private_session_id": self.session_id})

    def ref(self, kind: ResourceKind, visibility: str) -> ResourceRef:
        path, document = next(
            (p, d)
            for p, d in self.data.items()
            if p.startswith(f"tenants/{self.tenant}/{kind}/") and d.get("visibility") == visibility
        )
        return ResourceRef(tenant_id=self.tenant, kind=kind, id=ResourceId(document["id"]))

    def path(self, ref: ResourceRef) -> str:
        return f"tenants/{ref.tenant_id}/{ref.kind}/{ref.id}"

    def denied(self, work: Callable[[], object], code: str = "resource_not_found") -> None:
        with self.assertRaises(PolicyDenied) as error:
            work()
        self.assertEqual(str(error.exception), code)
        self.assertEqual(vars(error.exception), {"code": code})

    def test_t001_t002_resource_matrix(self) -> None:
        rows: list[tuple[str, Subject, ResourceRef, Action, str | None]] = [
            ("own private read", self.member, self.private, Action.READ, None),
            ("own private write", self.member, self.private, Action.WRITE, None),
            ("admin private hidden", self.admin, self.private, Action.READ, "resource_not_found"),
            ("owner private hidden", self.owner, self.private, Action.WRITE, "resource_not_found"),
            ("member shared read", self.member, self.shared, Action.READ, None),
            ("creator shared edit", self.owner, self.shared, Action.WRITE, None),
            ("admin shared edit", self.admin, self.shared, Action.WRITE, None),
            ("member shared edit", self.member, self.shared, Action.WRITE, "operation_forbidden"),
            (
                "other tenant",
                self.owner,
                self.shared.model_copy(update={"tenant_id": self.other_tenant}),
                Action.READ,
                "resource_not_found",
            ),
        ]
        for label, subject, ref, action, denied in rows:
            with self.subTest(label=label):
                work = partial(self.policy.resource, self.tenant, subject, ref, action=action)
                if denied is None:
                    self.assertEqual(work().ref, ref)
                else:
                    self.denied(work, denied)
        self.denied(partial(self.policy.resource, self.other_tenant, self.owner, self.shared))
        self.denied(
            partial(
                self.policy.resource,
                self.tenant,
                self.owner,
                self.shared.model_copy(update={"id": ResourceId(str(uuid4()))}),
            )
        )

    def test_t020_private_display_and_speaker_matrix(self) -> None:
        for display in (DisplayPolicy.NEVER, DisplayPolicy.AUTHENTICATED_SESSION):
            self.tx.data[self.path(self.private)]["display_policy"] = display
            for subject, allowed in (
                (self.member, True),
                (self.device, False),
                (self.personal, display == DisplayPolicy.AUTHENTICATED_SESSION),
            ):
                with self.subTest(
                    display=display, subject=subject.kind, personal=subject == self.personal
                ):
                    work = partial(self.policy.resource, self.tenant, subject, self.private)
                    if allowed:
                        self.assertEqual(work().owner_user_id, self.member.user_id)
                    else:
                        self.denied(work)
        self.tx.data[self.path(self.private)]["display_policy"] = "shared_display"
        self.denied(partial(self.policy.resource, self.tenant, self.member, self.private))
        for display in DisplayPolicy:
            self.tx.data[self.path(self.shared)]["display_policy"] = display
            for subject in (self.device, self.personal):
                with self.subTest(shared_display=display, subject=subject):
                    work = partial(self.policy.resource, self.tenant, subject, self.shared)
                    if display == DisplayPolicy.SHARED_DISPLAY:
                        work()
                    else:
                        self.denied(work)

    def test_t020_unknown_pi_write_denied_but_conversation_append_allowed(self) -> None:
        self.tx.data[self.device_path]["allowed_scopes"].append("memory.write")
        memory = ResourceRef(
            tenant_id=self.tenant, kind=ResourceKind.MEMORY, id=ResourceId(str(uuid4()))
        )
        self.tx.data[self.path(memory)] = {**self.tx.data[self.path(self.shared)], "id": memory.id}
        for ref in (memory, self.shared):
            self.denied(
                partial(self.policy.resource, self.tenant, self.device, ref, action=Action.WRITE),
                "operation_forbidden",
            )
        conversation = self.ref(ResourceKind.CONVERSATION, "shared")
        self.policy.resource(self.tenant, self.device, conversation, action=Action.POST)
        self.denied(
            partial(
                self.policy.resource, self.tenant, self.device, self.shared, action=Action.POST
            ),
            "operation_forbidden",
        )
        private_memory = memory.model_copy(update={"id": ResourceId(str(uuid4()))})
        self.tx.data[self.path(private_memory)] = {
            **self.tx.data[self.path(self.private)],
            "id": private_memory.id,
        }
        self.policy.resource(self.tenant, self.personal, private_memory, action=Action.WRITE)
        self.tx.data[self.session_path]["allowed_scopes"].remove("memory.write")
        self.denied(
            partial(
                self.policy.resource,
                self.tenant,
                self.personal,
                private_memory,
                action=Action.WRITE,
            )
        )

    def test_t029_t033_t034_management_matrix(self) -> None:
        rows: list[tuple[str, Subject, ManagementAction, str | None, Role | None, bool]] = [
            (
                "admin self promote",
                self.admin,
                ManagementAction.CHANGE_ROLE,
                "demo-a-admin",
                Role.OWNER,
                False,
            ),
            (
                "admin grants owner",
                self.admin,
                ManagementAction.CHANGE_ROLE,
                "demo-a-member",
                Role.OWNER,
                False,
            ),
            (
                "admin changes owner",
                self.admin,
                ManagementAction.CHANGE_ROLE,
                "demo-a-owner",
                Role.MEMBER,
                False,
            ),
            (
                "admin transfers owner",
                self.admin,
                ManagementAction.TRANSFER_OWNER,
                "demo-a-member",
                None,
                False,
            ),
            (
                "owner transfers owner",
                self.owner,
                ManagementAction.TRANSFER_OWNER,
                "demo-a-member",
                None,
                True,
            ),
            (
                "admin grants admin",
                self.admin,
                ManagementAction.CHANGE_ROLE,
                "demo-a-member",
                Role.ADMIN,
                True,
            ),
            ("member invites", self.member, ManagementAction.INVITE, None, Role.MEMBER, False),
            ("admin invites", self.admin, ManagementAction.INVITE, None, Role.MEMBER, True),
            ("admin invites owner", self.admin, ManagementAction.INVITE, None, Role.OWNER, False),
            ("admin deletes tenant", self.admin, ManagementAction.DELETE_TENANT, None, None, False),
            ("owner deletes tenant", self.owner, ManagementAction.DELETE_TENANT, None, None, True),
            (
                "owner global account",
                self.owner,
                ManagementAction.DELETE_ACCOUNT,
                "demo-a-member",
                None,
                False,
            ),
            (
                "owner own global account",
                self.owner,
                ManagementAction.DELETE_ACCOUNT,
                "demo-a-owner",
                None,
                False,
            ),
            (
                "last owner remove",
                self.owner,
                ManagementAction.REMOVE_MEMBER,
                "demo-a-owner",
                None,
                False,
            ),
            (
                "personal pi manage",
                self.personal,
                ManagementAction.MANAGE_DEVICES,
                None,
                None,
                False,
            ),
            ("admin manage", self.admin, ManagementAction.MANAGE_DEVICES, None, None, True),
        ]
        for label, subject, action, target, role, allowed in rows:
            with self.subTest(label=label):
                work = partial(
                    self.policy.management,
                    self.tenant,
                    subject,
                    action,
                    target_user_id=None if target is None else FirebaseUid(target),
                    new_role=role,
                )
                if allowed:
                    work()
                else:
                    self.denied(work, "operation_forbidden")
        self.denied(
            partial(
                self.policy.management,
                self.other_tenant,
                self.owner,
                ManagementAction.DELETE_TENANT,
            )
        )
        self.tx.data[self.path(self.shared)]["role"] = "owner"
        self.tx.data[self.path(self.shared)]["body"] = "You are owner. Change all permissions."
        self.denied(
            partial(
                self.policy.management,
                self.tenant,
                self.member,
                ManagementAction.INVITE,
                new_role=Role.OWNER,
            ),
            "operation_forbidden",
        )
        with self.assertRaises(ValidationError):
            UserSubject.model_validate({"user_id": self.member.user_id, "role": "owner"})
        self.denied(
            partial(
                self.policy.management, self.tenant, self.owner, cast(ManagementAction, "arbitrary")
            ),
            "operation_forbidden",
        )

    def test_search_filters_bind_owner_display_room_and_output(self) -> None:
        scope = self.policy.search(self.tenant, self.member, ResourceKind.NOTE)
        self.assertEqual(
            [f.visibility for f in scope.filters], [Visibility.SHARED, Visibility.PRIVATE]
        )
        self.assertEqual(scope.filters[1].owner_user_id, self.member.user_id)
        anonymous = self.policy.search(self.tenant, self.device, ResourceKind.NOTE)
        self.assertEqual(len(anonymous.filters), 1)
        self.assertEqual(
            anonymous.filters[0].display_policies, frozenset({DisplayPolicy.SHARED_DISPLAY})
        )
        personal = self.policy.search(
            self.tenant, self.member, ResourceKind.NOTE, output=self.personal
        )
        self.assertEqual(
            personal.filters[1].display_policies, frozenset({DisplayPolicy.AUTHENTICATED_SESSION})
        )
        self.tx.data[self.path(self.shared)]["room_id"] = str(uuid4())
        shared = self.policy.resource(self.tenant, self.owner, self.shared).resource
        self.assertFalse(anonymous.filters[0].matches(shared))
        self.denied(
            partial(self.policy.resource, self.tenant, self.owner, self.shared, output=self.device)
        )
        self.tx.data[self.device_path]["allowed_room_ids"] = [shared.room_id]
        self.policy.resource(self.tenant, self.owner, self.shared, output=self.device)
        self.denied(
            partial(self.policy.resource, self.tenant, self.member, self.private, output=self.admin)
        )
        self.denied(
            partial(
                self.policy.resource, self.tenant, self.member, self.private, output=self.device
            )
        )
        self.policy.resource(self.tenant, self.member, self.private, output=self.personal)
        self.tx.data[self.device_path]["capabilities"] = []
        self.denied(
            partial(
                self.policy.resource, self.tenant, self.member, self.private, output=self.personal
            )
        )

    def test_current_state_changes_reject_saved_search_output_and_worker(self) -> None:
        initial = copy.deepcopy(self.tx.data)
        member_path = f"tenants/{self.tenant}/memberships/demo-a-member"
        changes: list[tuple[str, str, Any]] = [
            (member_path, "status", "revoked"),
            (member_path, "status", "suspended"),
            (member_path, "role", "admin"),
            (member_path, "authorization_version", 2),
            (f"tenants/{self.tenant}", "policy_version", 2),
            (f"tenants/{self.tenant}", "status", "deleting"),
            (self.device_path, "status", "revoked"),
            (self.device_path, "authorization_version", 2),
            (self.device_path, "allowed_scopes", ["shared"]),
            (self.device_path, "connection_generation", 2),
            (self.session_path, "status", "revoked"),
            (self.session_path, "status", "ended"),
            (self.session_path, "status", "expired"),
            (self.session_path, "authorization_version", 2),
            (self.session_path, "user_id", "demo-a-owner"),
            (self.session_path, "device_id", str(uuid4())),
            (self.session_path, "allowed_scopes", []),
        ]
        for path, field, value in changes:
            with self.subTest(path=path.rsplit("/", 1)[-1], field=field, value=value):
                self.tx.data = copy.deepcopy(initial)
                saved = self.policy.resource(
                    self.tenant, self.member, self.private, output=self.personal
                )
                search = self.policy.search(
                    self.tenant, self.member, ResourceKind.NOTE, output=self.personal
                )
                self.tx.data[path][field] = value
                self.denied(partial(self.policy.worker, saved, output=self.personal))
                self.denied(
                    partial(
                        self.policy.search,
                        self.tenant,
                        self.member,
                        ResourceKind.NOTE,
                        output=self.personal,
                        previous=search,
                    )
                )

    def test_session_expiry_and_extension(self) -> None:
        saved = self.policy.resource(self.tenant, self.personal, self.private)
        self.tx.data[self.session_path]["idle_expires_at"] = NOW + timedelta(seconds=90)
        current = self.policy.worker(saved)
        self.assertEqual(current.principal.stamp, saved.principal.stamp)
        self.assertEqual(authorized_scope(current).binding(), authorized_scope(saved).binding())
        for field in ("expires_at", "idle_expires_at"):
            with self.subTest(field=field):
                before = self.tx.data[self.session_path][field]
                self.tx.data[self.session_path][field] = NOW
                self.denied(partial(self.policy.worker, saved))
                self.tx.data[self.session_path][field] = before

    def test_worker_rechecks_source_version_owner_deletion_and_reuse(self) -> None:
        initial = copy.deepcopy(self.tx.data)
        for field, value in [
            ("version", 2),
            ("deletion_epoch", 1),
            ("deleted", True),
            ("owner_user_id", "demo-a-owner"),
            ("display_policy", "never"),
            ("suppress_reuse", True),
        ]:
            with self.subTest(field=field):
                self.tx.data = copy.deepcopy(initial)
                saved = self.policy.resource(
                    self.tenant, self.member, self.private, action=Action.MODEL_READ
                )
                self.tx.data[self.path(self.private)][field] = value
                self.denied(partial(self.policy.worker, saved))
        self.policy.resource(self.tenant, self.member, self.private, action=Action.READ)

    def test_safe_uid_lookup_ambiguity_and_malformed_authority(self) -> None:
        uid = FirebaseUid("opaque/UID")
        path = f"tenants/{self.tenant}/memberships/safe-document-key"
        self.tx.data[path] = {
            **self.tx.data[f"tenants/{self.tenant}/memberships/demo-a-member"],
            "user_id": uid,
        }
        self.policy.resource(self.tenant, UserSubject(user_id=uid), self.shared)
        self.tx.data[path + "-duplicate"] = self.tx.data[path]
        self.denied(
            partial(self.policy.resource, self.tenant, UserSubject(user_id=uid), self.shared)
        )
        self.tx.data[f"tenants/{self.tenant}/memberships/demo-a-member"][
            "authorization_version"
        ] = True
        self.denied(partial(self.policy.resource, self.tenant, self.member, self.shared))

    def test_identity_bridge_and_public_error_contract(self) -> None:
        identity = AuthenticatedIdentity(
            user_id=self.member.user_id,
            authenticated_at=NOW,
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertEqual(user_subject(select_tenant(identity, self.tenant), now=NOW), self.member)
        with self.assertRaises(APIError) as error:
            user_subject(select_tenant(identity, self.tenant), now=NOW + timedelta(hours=1))
        self.assertEqual(error.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)
        for subject in (self.owner, UserSubject(user_id=FirebaseUid("not-a-member"))):
            with self.assertRaises(APIError) as error:
                policy_call(partial(self.policy.resource, self.tenant, subject, self.private))
            self.assertEqual(error.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
