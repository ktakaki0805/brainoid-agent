"""T-004 API revocation, pending denies, replacement and stale command boundaries."""

import copy
from datetime import timedelta
from functools import partial
from typing import Any, cast
from uuid import uuid4

from brainoid_api.auth.account_state import account_stamp
from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import Device, PairingConfirm
from brainoid_api.repositories.devices import DevicePatch, Devices
from brainoid_api.services.device_authorization import DeviceAuthorization, DeviceBinding
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import DeviceSubject, ResourceKind, ResourceRef, UserSubject
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from fastapi import FastAPI
from test_device_pairing import KEY, DevicePairingFixture


class DeviceManagementTests(DevicePairingFixture):
    def setUp(self) -> None:
        super().setUp()
        self.management = Devices(
            self.store,
            self.tenants,
            self.ledger,
            Cursors(b"d" * 32, now=self.clock.now),
            now=self.clock.now,
        )
        cast(FastAPI, self.client.app).dependency_overrides[get_auth_services] = lambda: (
            AuthServices(tenants=self.tenants, devices=self.repo, device_management=self.management)
        )
        self.pairing = self.create("n" * 32)
        self.device = self.confirm(self.pairing)
        self.original = self.exchange(self.pairing)
        self.path = f"tenants/{self.tenant}/devices/{self.device.id}"
        self.boundary = DeviceAuthorization(self.ledger, now=self.clock.now)
        self.binding = DeviceBinding(
            tenant_id=self.tenant,
            device_id=self.device.id,
            authorization_version=1,
            connection_generation=1,
        )

    def update(self, body: DevicePatch, version: int = 1, key: str = KEY) -> Device:
        return self.management.update(
            self.identity(),
            self.tenant,
            self.device.id,
            body,
            key,
            f'"{version}"',
            "request-update",
        )

    def revoke(self, version: int = 1, key: str = KEY) -> None:
        return self.management.revoke(
            self.identity(), self.tenant, self.device.id, key, f'"{version}"', "request-revoke"
        )

    def entity(self, tenant: ResourceId | None = None, *, room: bool = False) -> ResourceId:
        identity = ResourceId(str(uuid4()))
        tenant = tenant or self.tenant
        self.store.data[f"tenants/{tenant}/entities/{identity}"] = {
            "id": identity,
            "tenant_id": tenant,
            "kind": "room" if room else "appliance",
            "deleted": False,
        }
        return identity

    def denied_credentials(self) -> None:
        for call in (
            partial(self.repo.authenticate, self.original.access_token),
            partial(self.refresh, self.refresh_body(self.original)),
            partial(self.exchange, self.pairing),
            partial(self.confirm, self.pairing),
            lambda: self.store.run(lambda tx: self.boundary.current(tx, self.binding)),
        ):
            with self.assertRaises(APIError):
                call()

    def test_http_owner_admin_only_etag_cursor_and_entity_validation(self) -> None:
        url = f"/v1/tenants/{self.tenant}/devices"
        for uid, code in (("demo-a-member", 403), ("demo-b-owner", 404)):
            self.uid = uid
            for method in ("get", "patch", "delete"):
                result = getattr(self.client, method)(
                    url + ("" if method == "get" else "/" + self.device.id),
                    **({"json": {"allowed_scopes": ["home.control"]}} if method == "patch" else {}),
                    headers={"Idempotency-Key": KEY, "If-Match": '"1"'},
                )
                self.assertEqual(result.status_code, code, result.text)
        self.uid = "demo-a-admin"
        self.assertEqual(self.client.get(url).status_code, 200)
        read = self.client.get(url + "/" + self.device.id)
        self.assertEqual(read.headers["etag"], '"1"')
        alien = self.entity(self.other)
        body: dict[str, Any]
        for body in (
            {"allowed_entity_ids": [alien]},
            {"allowed_room_ids": [alien]},
            {"allowed_entity_ids": [str(uuid4())]},
        ):
            result = self.client.patch(
                url + "/" + self.device.id,
                json=body,
                headers={"Idempotency-Key": KEY, "If-Match": '"1"'},
            )
            self.assertEqual(result.status_code, 404, result.text)
        self.fails(
            lambda: self.management.get(self.identity(), self.other, self.device.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        for body in (
            {},
            {"allowed_scopes": None},
            {"status": "active"},
            {"allowed_scopes": ["admin"]},
        ):
            self.assertEqual(
                self.client.patch(
                    url + "/" + self.device.id,
                    json=body,
                    headers={"Idempotency-Key": KEY, "If-Match": '"1"'},
                ).status_code,
                422,
            )

    def test_name_change_preserves_credential_but_permission_changes_fence_it(self) -> None:
        renamed = self.update(DevicePatch(name="Kitchen"))
        self.assertEqual(
            (renamed.version, renamed.authorization_version, renamed.connection_generation),
            (2, 1, 1),
        )
        self.repo.authenticate(self.original.access_token)
        self.assertEqual(renamed, self.update(DevicePatch(name="Kitchen")))
        changed = self.update(DevicePatch(allowed_scopes=["notes", "shared"]), 2, KEY + "scope")
        self.assertEqual(
            (changed.version, changed.authorization_version, changed.connection_generation),
            (3, 2, 2),
        )
        self.assertEqual(
            changed, self.update(DevicePatch(allowed_scopes=["shared", "notes"]), 2, KEY + "scope")
        )
        self.denied_credentials()
        self.fails(lambda: self.update(DevicePatch(name="Kitchen")), ErrorCode.OPERATION_FORBIDDEN)
        audits = [v for p, v in self.store.data.items() if "/audit_events/" in p]
        self.assertEqual(len(audits), 3)
        for event in audits:
            self.assertEqual(
                set(event), {"actor_user_id", "target_device_id", "result", "request_id", "at"}
            )
            self.assertEqual(event["result"], "succeeded")

    def test_allowlist_grants_shrink_and_cursor_reauthorization(self) -> None:
        entity, room = self.entity(), self.entity(room=True)
        page = self.management.list(self.identity(), self.tenant, limit=1)
        assert page.next_cursor is not None
        self.management.list(self.identity(), self.tenant, limit=1, cursor=page.next_cursor)
        result = self.update(DevicePatch(allowed_entity_ids=[entity], allowed_room_ids=[room]))
        operation = next(v for p, v in self.store.data.items() if "/device_operations/" in p)
        self.assertEqual(operation["change"]["effect"], "grant")
        self.assertEqual(result.allowed_entity_ids, [entity])
        with self.assertRaises(APIError):
            self.management.list(self.identity(), self.tenant, limit=1, cursor=page.next_cursor)
        reduced = self.update(DevicePatch(allowed_entity_ids=[]), 2, KEY + "reduce-entity")
        self.assertEqual(reduced.allowed_entity_ids, [])
        self.assertEqual(reduced.connection_generation, 3)
        self.assertEqual(
            [
                v["change"]["effect"]
                for p, v in self.store.data.items()
                if "/device_operations/" in p
            ],
            ["grant", "deny"],
        )
        self.denied_credentials()

    def test_missing_etag_changed_body_and_foreign_device_are_rejected(self) -> None:
        self.fails(
            lambda: self.management.revoke(
                self.identity(), self.tenant, self.device.id, KEY, None, "request"
            ),
            ErrorCode.VERSION_REQUIRED,
        )
        other_device = next(
            ResourceId(d["id"])
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.other}/devices/")
        )
        self.fails(
            lambda: self.management.get(self.identity(), self.tenant, other_device),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.update(DevicePatch(name="Kitchen"))
        self.fails(lambda: self.update(DevicePatch(name="Bedroom")), ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_shrink_pending_retry_ledger_failure_and_current_actor(self) -> None:
        objects = cast(MemoryObjects, self.ledger._objects)

        def fail(name: str) -> None:
            if "/intents/" in name:
                raise LedgerError()

        objects.after = fail
        patch = DevicePatch(allowed_scopes=["shared", "notes"])
        self.fails(lambda: self.update(patch), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.store.data[self.path]["authorization_version"], 1)
        self.denied_credentials()
        self.assertEqual(
            self.management.get(self.identity(), self.tenant, self.device.id).status, "suspended"
        )
        self.fails(lambda: self.update(patch, key=KEY + "other"), ErrorCode.CONFLICT)
        objects.after = lambda name: None
        member = next(
            v
            for p, v in self.store.data.items()
            if "/memberships/" in p and v["user_id"] == self.uid
        )
        member["role"] = "member"
        self.fails(lambda: self.update(patch), ErrorCode.OPERATION_FORBIDDEN)
        member["role"] = "owner"
        result = self.update(patch)
        self.assertEqual((result.authorization_version, result.connection_generation), (2, 2))
        self.assertEqual(result, self.update(patch))
        self.denied_credentials()

    def test_commit_marker_loss_rolls_forward_without_generation_double_increment(self) -> None:
        objects = cast(MemoryObjects, self.ledger._objects)

        def fail(name: str) -> None:
            if "/commits/" in name:
                raise LedgerError()

        objects.after = fail
        self.fails(self.revoke, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.store.data[self.path]["status"], "suspended")
        self.assertEqual(self.store.data[self.path]["connection_generation"], 2)
        self.denied_credentials()
        objects.after = lambda name: None
        self.revoke()
        self.revoke()
        self.assertEqual(self.store.data[self.path]["connection_generation"], 2)
        self.assertEqual(self.store.data[self.path]["status"], "revoked")

    def test_api_revoke_private_session_and_saved_worker_output(self) -> None:
        session = ResourceId(str(uuid4()))
        self.store.data[f"tenants/{self.tenant}/private_sessions/{session}"] = {
            "tenant_id": self.tenant,
            "id": session,
            "device_id": self.device.id,
            "user_id": self.uid,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "membership_authorization_version": 1,
            "device_authorization_version": 1,
            "user_authorization_stamp": self.store.run(
                lambda tx: account_stamp(tx, FirebaseUid(self.uid))
            ),
            "allowed_scopes": ["notes"],
            "expires_at": self.clock.now() + timedelta(minutes=5),
            "idle_expires_at": self.clock.now() + timedelta(seconds=60),
        }
        self.store.data[f"tenants/{self.tenant}/private_session_slots/{self.device.id}"] = {
            "session_id": session
        }
        self.store.run(
            lambda tx: self.boundary.current(
                tx, self.binding, scope="notes", private_session_id=session
            )
        )
        note = ResourceId(str(uuid4()))
        self.store.data[f"tenants/{self.tenant}/notes/{note}"] = {
            "id": note,
            "tenant_id": self.tenant,
            "visibility": "shared",
            "display_policy": "shared_display",
            "owner_user_id": None,
            "created_by": self.uid,
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
        }
        self.store.data[self.path]["capabilities"] = ["display"]
        output = DeviceSubject(device_id=self.device.id, connection_generation=1)

        def policy(tx: Transaction) -> Evaluator:
            return transaction_policy(
                tx, ledger=self.ledger, allow_untracked_bootstrap=True, now=self.clock.now
            )

        saved = self.store.run(
            lambda tx: policy(tx).resource(
                self.tenant,
                UserSubject(user_id=FirebaseUid(self.uid)),
                ResourceRef(tenant_id=self.tenant, kind=ResourceKind.NOTE, id=note),
                output=output,
            )
        )
        response = self.client.delete(
            f"/v1/tenants/{self.tenant}/devices/{self.device.id}",
            headers={"Idempotency-Key": KEY, "If-Match": '"1"'},
        )
        self.assertEqual(response.status_code, 204, response.text)
        self.denied_credentials()
        with self.assertRaises(APIError):
            self.store.run(
                lambda tx: self.boundary.current(tx, self.binding, private_session_id=session)
            )
        with self.assertRaises(PolicyDenied):
            self.store.run(lambda tx: policy(tx).worker(saved, output=output))

    def test_reregister_requires_revoked_same_installation_and_new_secret(self) -> None:
        pairing = self.create("n" * 32, KEY + "next")
        body = PairingConfirm(
            user_code=pairing.user_code,
            device_name="Replacement",
            allowed_entity_ids=[],
            replaces_device_id=self.device.id,
        )

        def replace() -> Device:
            return self.repo.confirm(
                self.identity(), self.tenant, body, KEY + "replacement", "127.0.0.1"
            )

        self.fails(replace, ErrorCode.CONFLICT)
        self.revoke()
        other_tenant = self.tenant
        self.tenant, self.uid = self.other, "demo-b-owner"
        self.fails(replace, ErrorCode.CONFLICT)
        self.tenant, self.uid = other_tenant, "demo-a-owner"
        replacement = replace()
        self.assertNotEqual(replacement.id, self.device.id)
        self.assertEqual(replacement, replace())
        new = self.exchange(pairing, KEY + "new")
        self.repo.authenticate(new.access_token)
        self.denied_credentials()
        self.assertEqual(self.store.data[self.path]["status"], "revoked")
        self.assertEqual(
            len([p for p in self.store.data if p.startswith(f"tenants/{self.tenant}/devices/")]), 4
        )  # includes two seeded Pi

    def test_unexchanged_pairing_cannot_issue_after_authority_change(self) -> None:
        pairing = self.create()
        device = self.confirm(pairing)
        self.management.update(
            self.identity(),
            self.tenant,
            device.id,
            DevicePatch(allowed_scopes=["notes"]),
            KEY + "unexchanged",
            '"1"',
            "request",
        )
        self.fails(lambda: self.exchange(pairing), ErrorCode.AUTHENTICATION_REQUIRED)

    def test_pairing_validates_canonical_entities_and_restore_fences_generation(self) -> None:
        pairing = self.create()
        entity = self.entity(self.other)
        self.fails(
            lambda: self.repo.confirm(
                self.identity(),
                self.tenant,
                PairingConfirm(
                    user_code=pairing.user_code, device_name="Pi", allowed_entity_ids=[entity]
                ),
                KEY,
                "127.0.0.1",
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        before = copy.deepcopy(self.store.data[self.path])
        self.revoke()
        operation = next(v for p, v in self.store.data.items() if "/device_operations/" in p)
        from brainoid_policy.control_ledger import Change

        change = Change.model_validate(operation["change"])
        self.store.data[self.path] = before
        self.management._changes.reapply(change)
        first = copy.deepcopy(self.store.data[self.path])
        self.management._changes.reapply(change)
        self.assertEqual(self.store.data[self.path], first)
        self.assertEqual(first["connection_generation"], 2)
        self.denied_credentials()
