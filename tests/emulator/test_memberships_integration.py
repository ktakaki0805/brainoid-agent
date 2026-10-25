"""T-033/T-034: real Firestore owner contention, recovery, and unchanged Firebase tokens."""

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.invitations import InvitationAccept, InvitationCreate, Invitations
from brainoid_api.repositories.memberships import (
    MembershipPatch,
    Memberships,
    MembershipView,
    OwnerTransfer,
)
from brainoid_api.repositories.tenants import Tenants
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.models import Role
from firebase_admin import auth
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture

OWNER, ADMIN, MEMBER = "demo-a-owner", "demo-a-admin", "demo-a-member"
KEY = "emulator-membership-key-01"


class MembershipIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)
        self.store = FirestoreTransactions(self.db)
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = ControlLedger(LocalAuthzObjects(self.directory))
        cursors = Cursors(b"m" * 32, now=lambda: self.now)
        self.tenants = Tenants(self.store, cursors, ledger=self.ledger, now=lambda: self.now)
        self.repo = Memberships(
            self.store, self.tenants, self.ledger, cursors, now=lambda: self.now
        )
        self.tenant, self.other = [
            ResourceId(d["fields"]["tenant_id"])
            for d in load_fixture()["documents"]
            if d["path"].startswith("tenants/") and d["path"].count("/") == 1
        ]

    def identity(self, uid: str = OWNER) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid),
            issued_at=self.now,
            authenticated_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def update(
        self, uid: str, role: Role, actor: str = OWNER, version: int = 1, key: str = KEY
    ) -> MembershipView:
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
    ) -> MembershipView:
        return self.repo.transfer(
            self.identity(actor),
            self.tenant,
            OwnerTransfer(target_user_id=FirebaseUid(uid), target_version=target_version),
            key,
            f'"{version}"',
            str(uuid4()),
        )

    def owners(self) -> list[str]:
        return self.store.run(
            lambda tx: [
                m.user_id
                for m in self.repo._members(tx, self.tenant)
                if m.status == "active" and m.role == Role.OWNER
            ]
        )

    def fails(self, call: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as failure:
            call()
        self.assertEqual(failure.exception.code, code)

    def concurrent(self, *calls: Callable[[], object]) -> list[object]:
        barrier = Barrier(len(calls))

        def run(call: Callable[[], object]) -> object:
            barrier.wait(timeout=10)
            try:
                return call()
            except APIError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(run, call) for call in calls]
            return [future.result(timeout=45) for future in futures]

    def test_last_owner_and_concurrent_two_self_demotions_keep_effective_owner(self) -> None:
        self.fails(partial(self.update, OWNER, Role.MEMBER), ErrorCode.CONFLICT)
        self.update(MEMBER, Role.OWNER)
        results = self.concurrent(
            partial(self.update, OWNER, Role.MEMBER, key=KEY + "old"),
            partial(self.update, MEMBER, Role.MEMBER, actor=MEMBER, version=2, key=KEY + "new"),
        )
        self.assertEqual(sum(isinstance(r, MembershipView) for r in results), 1)
        self.assertEqual(len(self.owners()), 1)
        self.assertTrue(
            all(
                isinstance(r, MembershipView)
                or r in (ErrorCode.CONFLICT, ErrorCode.TEMPORARILY_UNAVAILABLE)
                for r in results
            )
        )

    def test_concurrent_transfers_from_distinct_owners_and_same_operation_retry(self) -> None:
        self.update(MEMBER, Role.OWNER)
        source = self.db.document(f"tenants/{self.tenant}/memberships/{ADMIN}").get().to_dict()
        assert source is not None
        self.db.document(f"tenants/{self.tenant}/memberships/opaque-target").set(
            {**source, "user_id": "transfer / target", "role": "member"}
        )
        one = partial(self.transfer, ADMIN, key=KEY + "one")
        two = partial(self.transfer, "transfer / target", actor=MEMBER, version=2, key=KEY + "two")
        results = self.concurrent(one, two)
        self.assertTrue(any(isinstance(r, MembershipView) for r in results))
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual(one().role, Role.OWNER)
        self.assertEqual(two().role, Role.OWNER)
        self.assertCountEqual(self.owners(), [ADMIN, "transfer / target"])
        same = self.concurrent(one, one)
        self.assertTrue(all(isinstance(r, MembershipView) for r in same))

    def test_transfer_failure_in_each_subject_keeps_owner_and_reserved_retry(self) -> None:
        commit = self.ledger.commit
        seen = 0

        def interrupted(change: Any) -> Any:
            nonlocal seen
            seen += 1
            if seen == 2:
                raise LedgerError()
            return commit(change)

        with patch.object(self.ledger, "commit", side_effect=interrupted):
            self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.owners(), [MEMBER])
        self.fails(
            partial(self.update, ADMIN, Role.OWNER, actor=MEMBER, key=KEY + "other"),
            ErrorCode.CONFLICT,
        )
        self.assertEqual(self.transfer().role, Role.OWNER)
        self.assertEqual(self.owners(), [MEMBER])
        document = self.db.document(f"tenants/{self.tenant}").get().to_dict()
        assert document is not None
        self.assertEqual(document["invitation_epoch"], 1)

    def test_lost_reservation_and_completion_commit_responses_roll_forward_once(self) -> None:
        commit = self.db._firestore_api.commit

        def lose_response(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose_response):
            self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.owners(), [OWNER])
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/membership_operations").stream())),
            1,
        )
        run = self.store.run
        count = 0

        def interrupted(work: Any) -> Any:
            nonlocal count
            count += 1
            if count == 10:
                with patch.object(self.db._firestore_api, "commit", side_effect=lose_response):
                    return run(work)
            return run(work)

        with patch.object(self.store, "run", side_effect=interrupted):
            self.fails(self.transfer, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.owners(), [MEMBER])
        self.assertEqual(self.transfer().role, Role.OWNER)
        audit = list(self.db.collection(f"tenants/{self.tenant}/audit_events").stream())
        self.assertEqual(len(audit), 1)
        document = audit[0].to_dict()
        assert document is not None
        self.assertEqual(document["result"], "succeeded")

    def test_t033_t034_runtime_http_old_firebase_token_denied_only_in_revoked_tenant(self) -> None:
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT, "httpTimeout": 10}, name="membership-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, firebase)

        def headers(uid: str) -> dict[str, str]:
            custom = auth.create_custom_token(uid, app=firebase).decode("ascii")
            result = self.emulator.request(
                "auth",
                "POST",
                "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
                {"token": custom, "returnSecureToken": True},
            )
            return {
                "Authorization": "Bearer " + result["idToken"],
                "Idempotency-Key": KEY,
                "If-Match": '"1"',
            }

        original = self.db.document(f"tenants/{self.tenant}/memberships/{MEMBER}").get().to_dict()
        assert original is not None
        self.db.document(f"tenants/{self.other}/memberships/shared-user").set(
            {**original, "tenant_id": self.other}
        )
        owner, admin, member = headers(OWNER), headers(ADMIN), headers(MEMBER)
        with (
            patch.dict(os.environ, {"AUTHZ_LEDGER_DIRECTORY": self.directory}),
            TestClient(create_app()) as http,
        ):
            endpoint = f"/v1/tenants/{self.tenant}/memberships"
            self.assertEqual(
                http.patch(
                    endpoint + "/" + ADMIN, json={"role": "owner"}, headers=admin
                ).status_code,
                403,
            )
            self.assertEqual(
                http.post(
                    endpoint + ":transfer-owner",
                    json={"target_user_id": MEMBER, "target_version": 1},
                    headers=admin,
                ).status_code,
                403,
            )
            self.assertEqual(
                http.patch(
                    endpoint + "/" + OWNER, json={"role": "member"}, headers=owner
                ).status_code,
                409,
            )
            self.assertEqual(
                http.get(f"/v1/tenants/{self.tenant}", headers=member).status_code, 200
            )
            self.assertEqual(http.delete(endpoint + "/" + MEMBER, headers=owner).status_code, 204)
            self.assertEqual(
                http.get(f"/v1/tenants/{self.tenant}", headers=member).status_code, 404
            )
            self.assertEqual(http.get(f"/v1/tenants/{self.other}", headers=member).status_code, 200)
            self.assertEqual(http.delete(f"/v1/users/{MEMBER}", headers=owner).status_code, 404)
            self.assertEqual(
                http.delete(
                    f"/v1/tenants/{self.other}/memberships/demo-b-member", headers=owner
                ).status_code,
                404,
            )
            self.assertIsNotNone(auth.get_user(MEMBER, app=firebase))
            self.assertEqual(http.get(endpoint, headers=member).status_code, 404)

    def test_rejoin_cannot_advance_revoked_subject_until_reservation_releases(self) -> None:
        revoke = partial(
            self.repo.revoke,
            self.identity(),
            self.tenant,
            FirebaseUid(MEMBER),
            KEY,
            '"1"',
            str(uuid4()),
        )
        run = self.store.run
        count = 0

        def stop_before_finish(work: Any) -> Any:
            nonlocal count
            count += 1
            if count == 6:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            return run(work)

        with patch.object(self.store, "run", side_effect=stop_before_finish):
            self.fails(revoke, ErrorCode.TEMPORARILY_UNAVAILABLE)
        invitations = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"i" * 32, now=lambda: self.now
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
        saved = (
            self.db.document(f"tenants/{self.tenant}/invitations/{invitation.id}").get().to_dict()
        )
        assert saved is not None
        self.assertIsNone(saved["used_operation_id"])
        self.assertEqual(list(self.db.collection_group("invitation_acceptances").stream()), [])
        revoke()
        self.assertEqual(accept().version, 3)
        self.assertEqual(self.owners(), [OWNER])

    def test_generated_sdk_query_requests_resolve_dot_and_reserved_character_uids(self) -> None:
        probe = subprocess.run(
            ["node", "tests/fixtures/memberships/sdk_requests.mjs", self.tenant],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        requests = json.loads(probe.stdout)
        source = self.db.document(f"tenants/{self.tenant}/memberships/{MEMBER}").get().to_dict()
        assert source is not None
        paths = {}
        for index, uid in enumerate(dict.fromkeys(r["user_id"] for r in requests)):
            path = f"tenants/{self.tenant}/memberships/sdk-{index}"
            paths[uid] = path
            self.db.document(path).set({**source, "user_id": uid})
        app = create_app()
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, memberships=self.repo
        )
        with (
            patch.dict(os.environ, {"AUTHZ_LEDGER_DIRECTORY": self.directory}),
            TestClient(app) as http,
        ):
            for request in requests:
                with self.subTest(uid=request["user_id"], method=request["method"]):
                    response = http.request(
                        request["method"],
                        request["url"],
                        headers=request["headers"],
                        content=request["body"],
                    )
                    expected = 200 if request["method"] == "PATCH" else 204
                    self.assertEqual(response.status_code, expected, response.text)
                    document = self.db.document(paths[request["user_id"]]).get().to_dict()
                    assert document is not None
                    self.assertEqual(document["user_id"], request["user_id"])
                    self.assertEqual(document["status"], "active" if expected == 200 else "revoked")
                    self.assertEqual(document["authorization_version"], 2 if expected == 200 else 3)
        self.assertEqual(self.owners(), [OWNER])
        unchanged = self.db.document(f"tenants/{self.tenant}/memberships/{MEMBER}").get().to_dict()
        self.assertEqual(unchanged, source)
