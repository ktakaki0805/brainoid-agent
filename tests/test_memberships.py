"""IAM-06: serialized owners, operation-bound recovery, tenant and privacy boundaries."""

import copy
import json
import subprocess
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from pathlib import Path
from threading import Event, RLock
from typing import Any
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.invitations import InvitationAccept, InvitationCreate, Invitations
from brainoid_api.repositories.memberships import MembershipPatch, Memberships, OwnerTransfer
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.settings import load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.models import Role
from fakes.authz_ledger import MemoryObjects
from fakes.runtime import FakeClock
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_authz_ledger import FaultStore

KEY = "membership-synthetic-key-01"
OWNER = "demo-a-owner"
ADMIN = "demo-a-admin"
MEMBER = "demo-a-member"


class LockedStore(FaultStore):
    def __init__(self) -> None:
        super().__init__()
        self.lock = RLock()

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        with self.lock:
            return super().run(work)


class MembershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = LockedStore()
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        self.store.data = {d["path"]: d["fields"] for d in fixture["documents"]}
        self.tenant = ResourceId(
            next(p.split("/")[1] for p in self.store.data if p.startswith("tenants/"))
        )
        self.other = ResourceId(
            next(
                p.split("/")[1]
                for p in self.store.data
                if p.startswith("tenants/") and self.tenant not in p
            )
        )
        self.objects = MemoryObjects()
        self.ledger = ControlLedger(self.objects)
        cursors = Cursors(b"m" * 32, now=self.clock.now)
        self.tenants = Tenants(self.store, cursors, ledger=self.ledger, now=self.clock.now)
        self.repo = Memberships(self.store, self.tenants, self.ledger, cursors, now=self.clock.now)
        self.uid = OWNER
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity(self.uid)
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, memberships=self.repo
        )
        self.client = self.enterContext(TestClient(app))
        self.spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())

    def identity(self, uid: str = OWNER) -> AuthenticatedIdentity:
        now = self.clock.now()
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid),
            issued_at=now,
            authenticated_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def update(
        self,
        uid: str = MEMBER,
        role: Role = Role.ADMIN,
        actor: str = OWNER,
        version: int = 1,
        key: str = KEY,
    ) -> Any:
        return self.repo.update(
            self.identity(actor),
            self.tenant,
            FirebaseUid(uid),
            MembershipPatch(role=role),
            key,
            f'"{version}"',
            str(uuid4()),
        )

    def transfer(
        self,
        uid: str = MEMBER,
        actor: str = OWNER,
        version: int = 1,
        target_version: int = 1,
        key: str = KEY,
    ) -> Any:
        return self.repo.transfer(
            self.identity(actor),
            self.tenant,
            OwnerTransfer(target_user_id=FirebaseUid(uid), target_version=target_version),
            key,
            f'"{version}"',
            str(uuid4()),
        )

    def revoke(
        self, uid: str = MEMBER, actor: str = OWNER, version: int = 1, key: str = KEY
    ) -> None:
        self.repo.revoke(
            self.identity(actor), self.tenant, FirebaseUid(uid), key, f'"{version}"', str(uuid4())
        )

    def fails(self, work: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as error:
            work()
        self.assertEqual(error.exception.code, code)

    def owners(self) -> list[str]:
        return self.store.run(
            lambda tx: [
                m.user_id
                for m in self.repo._members(tx, self.tenant)
                if m.status == "active" and m.role == Role.OWNER
            ]
        )

    def test_t033_owner_admin_member_matrix_and_last_owner(self) -> None:
        forbidden = [
            partial(self.update, actor=ADMIN, role=Role.OWNER),
            partial(self.update, uid=ADMIN, actor=ADMIN, role=Role.OWNER),
            partial(self.update, uid=OWNER, actor=ADMIN, role=Role.MEMBER),
            partial(self.transfer, actor=ADMIN),
            partial(self.revoke, uid=OWNER, actor=ADMIN),
            partial(self.revoke, uid=OWNER),
            partial(self.update, actor=MEMBER),
        ]
        for work in forbidden:
            self.fails(work, ErrorCode.OPERATION_FORBIDDEN)
        self.fails(partial(self.update, uid=OWNER, role=Role.MEMBER), ErrorCode.CONFLICT)
        self.assertEqual(self.objects.data, {})
        self.assertEqual(self.update(actor=ADMIN).role, Role.ADMIN)

    def test_transfer_uses_two_versions_then_self_downgrade_replays(self) -> None:
        result = self.transfer()
        self.assertEqual((result.user_id, result.role, result.version), (MEMBER, Role.OWNER, 2))
        self.assertEqual(self.owners(), [MEMBER])
        self.assertEqual(self.transfer(), result)
        self.fails(partial(self.transfer, uid=ADMIN), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(self.tenants.get(self.identity(), self.tenant).my_role, Role.MEMBER)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 1)
        audits = [d for p, d in self.store.data.items() if "/audit_events/" in p]
        self.assertEqual(len(audits), 1)
        self.assertEqual(
            set(audits[0]), {"actor_user_id", "target_user_ids", "result", "request_id", "at"}
        )
        self.assertEqual(audits[0]["result"], "succeeded")
        self.fails(partial(self.update, uid=ADMIN, key=KEY + "new"), ErrorCode.OPERATION_FORBIDDEN)

    def test_owner_grant_then_other_owner_remove_and_self_demotion(self) -> None:
        self.update(role=Role.OWNER)
        self.update(uid=OWNER, role=Role.ADMIN, key=KEY + "self")
        self.assertEqual(self.owners(), [MEMBER])
        self.update(uid=OWNER, role=Role.OWNER, actor=MEMBER, version=2, key=KEY + "restore")
        self.revoke(uid=OWNER, actor=MEMBER, version=3, key=KEY + "revoke")
        self.assertEqual(self.owners(), [MEMBER])

    def test_transfer_http_contract_requires_both_versions_and_replays(self) -> None:
        endpoint = f"/v1/tenants/{self.tenant}/memberships:transfer-owner"
        headers = {"Idempotency-Key": KEY, "If-Match": '"1"'}
        body = {"target_user_id": MEMBER, "target_version": 1}
        stale = self.client.post(endpoint, json={**body, "target_version": 9}, headers=headers)
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(
            self.client.post(endpoint, json={**body, "role": "owner"}, headers=headers).status_code,
            422,
        )
        response = self.client.post(endpoint, json=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"2"')
        self.assertEqual(
            response.json(), {"user_id": MEMBER, "role": "owner", "status": "active", "version": 2}
        )
        self.assertEqual(
            self.client.post(endpoint, json=body, headers=headers).json(), response.json()
        )

    def test_t034_revocation_old_identity_other_tenant_and_private_unchanged(self) -> None:
        old_identity = self.identity(MEMBER)
        source = self.store.data[f"tenants/{self.tenant}/memberships/{MEMBER}"]
        self.store.data[f"tenants/{self.other}/memberships/shared-user"] = {
            **source,
            "tenant_id": self.other,
        }
        snapshot = copy.deepcopy(self.store.data)
        self.revoke()
        self.revoke()
        self.fails(
            lambda: self.tenants.get(old_identity, self.tenant), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.assertEqual(self.tenants.get(old_identity, self.other).my_role, Role.MEMBER)
        for path, document in snapshot.items():
            if "/memberships/" not in path and path != f"tenants/{self.tenant}":
                self.assertEqual(self.store.data[path], document)
        self.fails(
            lambda: self.repo.revoke(
                self.identity(), self.other, FirebaseUid("demo-b-member"), KEY, '"1"', str(uuid4())
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )

    def test_recent_auth_versions_and_strict_http_opaque_uid_contract(self) -> None:
        path = f"tenants/{self.tenant}/memberships/opaque-key"
        self.store.data[path] = {
            **self.store.data[f"tenants/{self.tenant}/memberships/{MEMBER}"],
            "user_id": "opaque / uid",
        }
        endpoint = f"/v1/tenants/{self.tenant}/memberships/opaque%20%2F%20uid"
        headers = {"Idempotency-Key": KEY, "If-Match": '"1"'}
        self.assertEqual(
            self.client.patch(
                endpoint, json={"role": "admin"}, headers={"Idempotency-Key": KEY}
            ).status_code,
            428,
        )
        self.assertEqual(
            self.client.patch(
                endpoint, json={"role": "admin", "private": "secret"}, headers=headers
            ).status_code,
            422,
        )
        with self.assertLogs("brainoid_api", level="INFO") as logs:
            response = self.client.patch(endpoint, json={"role": "admin"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["etag"], '"2"')
        self.assertEqual(response.json()["user_id"], "opaque / uid")
        self.assertNotIn("opaque", "".join(logs.output))
        schema = {
            **self.spec["components"]["schemas"]["Membership"],
            "components": self.spec["components"],
        }
        Draft202012Validator(schema).validate(response.json())
        self.fails(partial(self.update, version=9, key=KEY + "version"), ErrorCode.VERSION_CONFLICT)
        identity = self.identity().model_copy(
            update={"authenticated_at": self.clock.now() - timedelta(seconds=301)}
        )
        self.fails(
            lambda: self.repo.revoke(
                identity, self.tenant, FirebaseUid(MEMBER), KEY + "reauth", '"1"', str(uuid4())
            ),
            ErrorCode.REAUTH_REQUIRED,
        )
        self.assertEqual(
            self.client.delete(f"/v1/users/{MEMBER}", headers=headers).status_code, 404
        )

    def test_sdk_requests_resolve_opaque_query_uids_and_preserve_legacy_replays(self) -> None:
        probe = subprocess.run(
            ["node", "tests/fixtures/memberships/sdk_requests.mjs", self.tenant],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        requests = json.loads(probe.stdout)
        source = self.store.data[f"tenants/{self.tenant}/memberships/{MEMBER}"]
        paths = {}
        for index, uid in enumerate(dict.fromkeys(r["user_id"] for r in requests)):
            path = f"tenants/{self.tenant}/memberships/sdk-{index}"
            paths[uid] = path
            self.store.data[path] = {**source, "user_id": uid}
        original_members = {
            path: copy.deepcopy(doc)
            for path, doc in self.store.data.items()
            if "/memberships/" in path and path not in paths.values()
        }
        for request in requests:
            with self.subTest(uid=request["user_id"], method=request["method"]):
                response = self.client.request(
                    request["method"],
                    request["url"],
                    headers=request["headers"],
                    content=request["body"],
                )
                status = 200 if request["method"] == "PATCH" else 204
                self.assertEqual(response.status_code, status, response.text)
                if status == 200:
                    self.assertEqual(response.json()["user_id"], request["user_id"])
                    self.assertEqual(response.headers["etag"], '"2"')
                document = self.store.data[paths[request["user_id"]]]
                self.assertEqual(document["role"], "admin")
                self.assertEqual(document["status"], "active" if status == 200 else "revoked")
                self.assertEqual(document["authorization_version"], 2 if status == 200 else 3)
                if request["user_id"] == "opaque / uid":
                    # Both entry points share the operation receipt, so changing only
                    # the transport route neither repeats a mutation nor changes its UID.
                    legacy = f"/v1/tenants/{self.tenant}/memberships/{quote(request['user_id'], safe='')}"
                    replay = self.client.request(
                        request["method"],
                        legacy,
                        headers=request["headers"],
                        content=request["body"],
                    )
                    self.assertEqual(replay.status_code, status)
                    self.assertEqual(replay.content, response.content)
        for path, document in original_members.items():
            self.assertEqual(self.store.data[path], document)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 5)

    def test_query_uid_is_required_and_unambiguous(self) -> None:
        endpoint = f"/v1/tenants/{self.tenant}/memberships"
        headers = {"Idempotency-Key": KEY, "If-Match": '"1"'}
        for method in ("PATCH", "DELETE"):
            body = {"role": "admin"} if method == "PATCH" else None
            missing = self.client.request(method, endpoint, json=body, headers=headers)
            duplicate = self.client.request(
                method, endpoint + f"?user_id={MEMBER}&user_id={ADMIN}", json=body, headers=headers
            )
            self.assertEqual(missing.status_code, 422)
            self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(self.objects.data, {})

    def test_membership_pagination_is_current_and_metadata_only(self) -> None:
        first = self.repo.list(self.identity(), self.tenant, limit=1)
        self.assertIsNotNone(first.next_cursor)
        second = self.repo.list(self.identity(), self.tenant, limit=1, cursor=first.next_cursor)
        self.assertNotEqual(first.items, second.items)
        self.fails(
            lambda: self.repo.list(self.identity(MEMBER), self.tenant),
            ErrorCode.OPERATION_FORBIDDEN,
        )
        self.update()
        self.fails(
            lambda: self.repo.list(self.identity(), self.tenant, limit=1, cursor=first.next_cursor),
            ErrorCode.INVALID_REQUEST,
        )

    def test_all_transfer_ledger_failures_keep_owner_and_same_operation_resumes(self) -> None:
        initial = copy.deepcopy(self.store.data)
        for subject in (MEMBER, OWNER):
            for suffix in ("operations", "anchor.json", "intents/2.json", "commits/2.json"):
                for when in ("before", "after"):
                    with self.subTest(subject=subject, suffix=suffix, when=when):
                        self.store.data = copy.deepcopy(initial)
                        self.objects.data.clear()
                        self.objects.before = self.objects.after = lambda name: None
                        # First four writes are grant, next three or four are deny.
                        seen = 0

                        def fail(name: str, subject: str = subject, suffix: str = suffix) -> None:
                            nonlocal seen
                            seen += 1
                            phase = 0 if subject == MEMBER else 4
                            step = (
                                "operations",
                                "anchor.json",
                                "intents/2.json",
                                "commits/2.json",
                            ).index(suffix) + 1
                            if seen == phase + step:
                                raise LedgerError()

                        setattr(self.objects, when, fail)
                        self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
                        self.assertTrue(self.owners())
                        self.fails(
                            partial(self.update, uid=ADMIN, key=KEY + "other"), ErrorCode.CONFLICT
                        )
                        self.objects.before = self.objects.after = lambda name: None
                        self.assertEqual(self.transfer().role, Role.OWNER)
                        self.assertEqual(self.owners(), [MEMBER])
                        self.assertEqual(
                            self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 1
                        )

    def test_all_transfer_database_failure_boundaries_are_resumable(self) -> None:
        initial = copy.deepcopy(self.store.data)
        self.transfer()
        calls = self.store.calls
        for when in ("before", "after"):
            for call in range(1, calls + 1):
                with self.subTest(when=when, call=call):
                    self.store.data = copy.deepcopy(initial)
                    self.objects.data.clear()
                    self.store.calls = 0
                    self.store.before = self.store.after = lambda count: None

                    def fail(count: int, call: int = call) -> None:
                        if count == call:
                            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

                    setattr(self.store, when, fail)
                    self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
                    self.store.before = self.store.after = lambda count: None
                    self.assertTrue(self.owners())
                    self.assertEqual(self.transfer().role, Role.OWNER)
                    self.assertEqual(self.owners(), [MEMBER])
                    self.assertEqual(
                        self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 1
                    )

    def test_reserved_first_deny_blocks_second_owner_deny_before_ledger(self) -> None:
        self.update(role=Role.OWNER)
        entered, release = Event(), Event()

        def block(name: str) -> None:
            if "/operations/" in name:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("release not signaled")

        self.objects.before = block
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.update, OWNER, Role.MEMBER, OWNER, 1, KEY + "one")
            try:
                self.assertTrue(entered.wait(5))
                writes = self.objects.writes
                self.fails(
                    partial(
                        self.update,
                        uid=MEMBER,
                        role=Role.MEMBER,
                        actor=MEMBER,
                        version=2,
                        key=KEY + "two",
                    ),
                    ErrorCode.CONFLICT,
                )
                self.assertEqual(self.objects.writes, writes)
            finally:
                release.set()
            pending.result()
        self.objects.before = lambda name: None
        self.fails(
            partial(
                self.update, uid=MEMBER, role=Role.MEMBER, actor=MEMBER, version=2, key=KEY + "two"
            ),
            ErrorCode.CONFLICT,
        )
        self.assertEqual(self.owners(), [MEMBER])

    def test_retry_requires_current_user_tenant_and_exact_latest_ledger(self) -> None:
        with patch.object(self.ledger, "commit", side_effect=LedgerError()):
            self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.store.data[f"users/{OWNER}"]["status"] = "deleting"
        self.fails(self.transfer, ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.owners(), [OWNER])
        self.store.data[f"users/{OWNER}"]["status"] = "active"
        self.transfer()
        self.update(uid=ADMIN, role=Role.OWNER, actor=MEMBER, key=KEY + "later")
        self.revoke(uid=MEMBER, actor=ADMIN, version=2, key=KEY + "revoke")
        self.fails(self.transfer, ErrorCode.OPERATION_FORBIDDEN)

    def test_rejoin_waits_for_revoke_release_without_consuming_invitation(self) -> None:
        def stop_before_finish(count: int) -> None:
            if count == 6:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.store.before = stop_before_finish
        self.fails(self.revoke, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.store.before = lambda count: None
        invitations = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"i" * 32, now=self.clock.now
        )
        invitation = invitations.create(
            self.identity(), self.tenant, InvitationCreate(role="member"), KEY + "issue"
        )
        accept = partial(
            invitations.accept,
            self.identity(MEMBER),
            InvitationAccept(code=invitation.code),
            KEY + "accept",
        )
        self.fails(accept, ErrorCode.CONFLICT)
        self.assertIsNone(
            self.store.data[f"tenants/{self.tenant}/invitations/{invitation.id}"][
                "used_operation_id"
            ]
        )
        self.assertFalse(any("/invitation_acceptances/" in path for path in self.store.data))
        self.revoke()
        self.assertEqual(accept().version, 3)
        self.assertEqual(self.owners(), [OWNER])
        self.fails(self.revoke, ErrorCode.OPERATION_FORBIDDEN)

    def test_claimed_invitation_resumes_after_membership_reservation_releases(self) -> None:
        invitations = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"i" * 32, now=self.clock.now
        )
        invitation = invitations.create(
            self.identity(), self.tenant, InvitationCreate(role="member"), KEY + "issue"
        )
        accept = partial(
            invitations.accept,
            self.identity("new / invitee"),
            InvitationAccept(code=invitation.code),
            KEY + "accept",
        )
        with patch.object(self.ledger, "commit", side_effect=LedgerError()):
            self.fails(accept, ErrorCode.TEMPORARILY_UNAVAILABLE)
        with patch.object(self.ledger, "prepare", side_effect=LedgerError()):
            self.fails(self.update, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.fails(accept, ErrorCode.CONFLICT)
        self.update()
        self.assertEqual(accept().version, 1)
        self.assertEqual(accept().version, 1)
