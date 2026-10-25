"""IAM-04 real Firestore contention, expiry, durable claims and runtime HTTP wiring."""

import os
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
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.repositories.invitations import (
    Invitation,
    InvitationAccept,
    InvitationCreate,
    Invitations,
    MembershipResult,
)
from brainoid_api.repositories.tenants import Tenants
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    LedgerError,
    MemberTarget,
    subject_key,
)
from brainoid_policy.models import Role
from firebase_admin import auth
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture

KEY = "synthetic-acceptance-key-001"


class InvitationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)
        self.store = FirestoreTransactions(self.db)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = ControlLedger(LocalAuthzObjects(directory))
        self.tenants = Tenants(
            self.store,
            Cursors(b"x" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
        )
        self.repo = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"x" * 32, now=lambda: self.now
        )
        self.tenant = ResourceId(
            next(
                d["fields"]["tenant_id"]
                for d in load_fixture()["documents"]
                if d["path"].startswith("tenants/") and d["path"].count("/") == 1
            )
        )
        self.uid = FirebaseUid("new/opaque invited user")

    def identity(self, uid: str | None = None) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid or self.uid),
            issued_at=self.now,
            authenticated_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def issue(self) -> Invitation:
        return self.repo.create(
            self.identity("demo-a-owner"),
            self.tenant,
            InvitationCreate(role="member"),
            str(uuid4()),
        )

    def accept(self, code: str, key: str = KEY, uid: str | None = None) -> MembershipResult:
        return self.repo.accept(self.identity(uid), InvitationAccept(code=code), key)

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

    def fails(self, call: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as failure:
            call()
        self.assertEqual(failure.exception.code, code)

    def test_concurrent_same_operation_converges_to_single_membership(self) -> None:
        code = self.issue().code
        results = self.concurrent(partial(self.accept, code), partial(self.accept, code))
        self.assertIsInstance(results[0], MembershipResult)
        self.assertEqual(results[0], results[1])
        members = list(self.db.collection(f"tenants/{self.tenant}/memberships").stream())
        self.assertEqual(len(members), 4)
        self.assertEqual(sum(m.get("user_id") == self.uid for m in members), 1)
        self.assertEqual(len(list(self.db.collection_group("invitation_acceptances").stream())), 1)
        self.assertEqual(self.accept(code), results[0])

    def test_concurrent_different_users_can_claim_code_only_once(self) -> None:
        code = self.issue().code
        results = self.concurrent(
            partial(self.accept, code), partial(self.accept, code, uid="other-user")
        )
        self.assertEqual(sum(isinstance(r, MembershipResult) for r in results), 1)
        self.assertIn(ErrorCode.RESOURCE_NOT_FOUND, results)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/memberships").stream())), 4
        )

    def test_concurrent_two_codes_never_duplicate_membership_or_change_role(self) -> None:
        first, second = self.issue(), self.issue()
        results = self.concurrent(
            partial(self.accept, first.code), partial(self.accept, second.code, KEY + "2")
        )
        self.assertEqual(sum(isinstance(r, MembershipResult) for r in results), 1)
        self.assertIn(ErrorCode.OPERATION_FORBIDDEN, results)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/memberships").stream())), 4
        )

    def test_atomic_final_attempt_and_window_and_expiry_boundaries(self) -> None:
        invitation, expired = self.issue(), self.issue()
        for i in range(4):
            self.fails(
                partial(self.accept, str(i) * 32, KEY + str(i)), ErrorCode.RESOURCE_NOT_FOUND
            )
        results = self.concurrent(
            partial(self.accept, "a" * 32), partial(self.accept, "b" * 32, KEY + "b")
        )
        self.assertCountEqual(results, [ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.RATE_LIMITED])
        self.now += timedelta(seconds=899)
        self.fails(partial(self.accept, invitation.code), ErrorCode.RATE_LIMITED)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.accept(invitation.code).version, 1)
        self.now = expired.expires_at
        self.fails(
            partial(self.accept, expired.code, KEY + "expired", "fresh-user"),
            ErrorCode.RESOURCE_EXPIRED,
        )

    def test_revoke_old_code_pending_rejoin_and_replay_use_latest_ledger(self) -> None:
        original, stale = self.issue(), self.issue()
        self.accept(original.code)
        revoke = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=1,
            authorization_version=2,
            effect="deny",
            target=MemberTarget(user_id=self.uid, role=Role.MEMBER, status="revoked"),
        )
        AuthzChanges(self.store, self.ledger).execute(revoke, authorize=lambda tx, change: None)
        self.fails(partial(self.accept, stale.code, KEY + "stale"), ErrorCode.RESOURCE_EXPIRED)
        fresh = self.issue()
        with patch.object(self.ledger, "commit", side_effect=LedgerError()):
            self.fails(
                partial(self.accept, fresh.code, KEY + "fresh"), ErrorCode.TEMPORARILY_UNAVAILABLE
            )
        self.fails(
            lambda: self.tenants.get(self.identity(), self.tenant), ErrorCode.RESOURCE_NOT_FOUND
        )
        entry = self.ledger.latest(subject_key(self.tenant, "membership", self.uid))
        assert entry is not None
        self.assertEqual(entry.state, "pending")
        self.fails(partial(self.accept, original.code), ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.accept(fresh.code, KEY + "fresh").version, 3)
        self.now += timedelta(minutes=15)
        self.fails(partial(self.accept, original.code), ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/memberships").stream())), 4
        )

    def test_lost_claim_commit_response_keeps_one_operation_and_used_invitation(self) -> None:
        code = self.issue().code
        commit = self.db._firestore_api.commit

        def lose_response(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic claim response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose_response):
            self.fails(partial(self.accept, code), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(len(list(self.db.collection_group("invitation_acceptances").stream())), 1)
        self.assertEqual(self.accept(code).version, 1)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/memberships").stream())), 4
        )

    def test_missing_ledger_stops_tracked_member_but_preserves_untracked_owner(self) -> None:
        self.accept(self.issue().code)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        guarded = Tenants(
            self.store,
            Cursors(b"x" * 32, now=lambda: self.now),
            ledger=ControlLedger(LocalAuthzObjects(directory)),
            now=lambda: self.now,
        )
        self.fails(lambda: guarded.get(self.identity(), self.tenant), ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(guarded.list(self.identity()).items, [])
        self.assertEqual(guarded.get(self.identity("demo-a-owner"), self.tenant).my_role, "owner")

    def test_real_firebase_http_runtime_and_invitation_authorization(self) -> None:
        firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT, "httpTimeout": 10}, name="invite-" + str(uuid4())
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
            return {"Authorization": "Bearer " + result["idToken"], "Idempotency-Key": KEY}

        with TestClient(create_app()) as http:
            self.assertEqual(
                http.post("/v1/invitations:accept", json={"code": "x" * 32}).status_code, 401
            )
            owner, member = headers("demo-a-owner"), headers("demo-a-member")
            endpoint = f"/v1/tenants/{self.tenant}/invitations"
            self.assertEqual(
                http.post(endpoint, json={"role": "member"}, headers=member).status_code, 403
            )
            self.assertEqual(
                http.post(endpoint, json={"role": "owner"}, headers=owner).status_code, 422
            )
            issued = http.post(endpoint, json={"role": "member"}, headers=owner)
            self.assertEqual(issued.status_code, 201)
            invitee = headers("invite-http-" + str(uuid4()))
            accepted = http.post(
                "/v1/invitations:accept", json={"code": issued.json()["code"]}, headers=invitee
            )
            self.assertEqual(accepted.status_code, 201)
            self.assertEqual(
                http.get(f"/v1/tenants/{self.tenant}", headers=invitee).status_code, 200
            )
            self.assertEqual(
                http.post(
                    "/v1/invitations:accept", json={"code": issued.json()["code"]}, headers=invitee
                ).json(),
                accepted.json(),
            )
