"""IAM-04 HTTP, atomic claims, latest-ledger fencing, and interrupted grant retries."""

import copy
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.firebase_identity import validate_sdk_environment
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.repositories.invitations import InvitationAccept, InvitationCreate, Invitations
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    LedgerError,
    MemberTarget,
    subject_key,
)
from brainoid_policy.models import Role
from fakes.authz_ledger import MemoryObjects
from fakes.runtime import FakeClock
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient
from test_api_foundation import CLOUD_ENV, EMULATOR_ENV, LOCAL_ENV
from test_authz_ledger import FaultStore

ISSUE_KEY = "synthetic-issuance-key-001"
ACCEPT_KEY = "synthetic-acceptance-key-001"


class InvitationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = FaultStore()
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        self.store.data = {doc["path"]: doc["fields"] for doc in fixture["documents"]}
        self.tenant = ResourceId(
            next(
                p.split("/")[1]
                for p in self.store.data
                if p.startswith("tenants/") and p.count("/") == 1
            )
        )
        self.objects = MemoryObjects()
        self.ledger = ControlLedger(self.objects)
        self.tenants = Tenants(
            self.store,
            Cursors(b"x" * 32, now=self.clock.now),
            ledger=self.ledger,
            now=self.clock.now,
        )
        self.repo = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"x" * 32, now=self.clock.now
        )
        self.uid = "new/user with spaces"
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity(self.uid)
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, invitations=self.repo
        )
        self.client = self.enterContext(TestClient(app))
        self.spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())

    def identity(self, uid: str) -> AuthenticatedIdentity:
        now = self.clock.now()
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid),
            issued_at=now,
            authenticated_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def issue(self, key: str = ISSUE_KEY, role: str = "member", uid: str = "demo-a-owner") -> Any:
        return self.repo.create(
            self.identity(uid), self.tenant, InvitationCreate.model_validate({"role": role}), key
        )

    def accept(self, code: str, key: str = ACCEPT_KEY) -> Any:
        return self.repo.accept(self.identity(self.uid), InvitationAccept(code=code), key)

    def fails(self, work: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as error:
            work()
        self.assertEqual(error.exception.code, code)

    def latest(self) -> Any:
        return self.ledger.latest(subject_key(self.tenant, "membership", self.uid))

    def revoke(self) -> Change:
        latest = self.latest()
        change = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=latest.change.authorization_version,
            authorization_version=latest.change.authorization_version + 1,
            effect="deny",
            target=MemberTarget(user_id=FirebaseUid(self.uid), role=Role.MEMBER, status="revoked"),
        )
        AuthzChanges(self.store, self.ledger).execute(change, authorize=lambda tx, change: None)
        return change

    def test_http_contract_code_hash_privacy_and_exact_replay(self) -> None:
        self.uid = "demo-a-owner"
        with self.assertLogs("brainoid_api", level="INFO") as logs:
            response = self.client.post(
                f"/v1/tenants/{self.tenant}/invitations",
                json={"role": "admin"},
                headers={"Idempotency-Key": ISSUE_KEY},
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.headers["cache-control"], "no-store")
            invitation = response.json()
            replay = self.client.post(
                f"/v1/tenants/{self.tenant}/invitations",
                json={"role": "admin"},
                headers={"Idempotency-Key": ISSUE_KEY},
            )
            self.assertEqual(replay.json(), invitation)
            self.uid = "opaque / invited user"
            accepted = self.client.post(
                "/v1/invitations:accept",
                json={"code": invitation["code"]},
                headers={"Idempotency-Key": ACCEPT_KEY},
            )
            self.assertEqual(accepted.status_code, 201)
            self.assertEqual(
                accepted.json(), self.accept(invitation["code"]).model_dump(mode="json")
            )
        for schema, value in (("Invitation", invitation), ("Membership", accepted.json())):
            Draft202012Validator(
                {"$ref": "#/components/schemas/" + schema, "components": self.spec["components"]}
            ).validate(value)
        persisted = (
            json.dumps(self.store.data, default=str) + repr(self.objects.data) + str(logs.output)
        )
        self.assertNotIn(invitation["code"], persisted)
        paths = [
            path
            for path, doc in self.store.data.items()
            if "/memberships/" in path and doc["user_id"] == self.uid
        ]
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(paths[0].split("/")), 4)
        self.assertEqual(self.tenants.get(self.identity(self.uid), self.tenant).my_role, "admin")
        self.assertEqual(self.latest().state, "committed")

    def test_permission_headers_and_owner_injection(self) -> None:
        for uid, expected in (("demo-a-member", 403), ("demo-b-owner", 404)):
            self.uid = uid
            response = self.client.post(
                f"/v1/tenants/{self.tenant}/invitations",
                json={"role": "member"},
                headers={"Idempotency-Key": ISSUE_KEY},
            )
            self.assertEqual(response.status_code, expected)
        self.uid = "demo-a-owner"
        for body in ({"role": "owner"}, {"role": "member", "user_id": "other"}):
            self.assertEqual(
                self.client.post(
                    f"/v1/tenants/{self.tenant}/invitations",
                    json=body,
                    headers={"Idempotency-Key": ISSUE_KEY},
                ).status_code,
                422,
            )
        self.assertEqual(
            self.client.post(
                f"/v1/tenants/{self.tenant}/invitations", json={"role": "member"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/v1/invitations:accept",
                json={"code": "x" * 32, "user_id": "other"},
                headers={"Idempotency-Key": ACCEPT_KEY},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/v1/invitations:accept",
                json={"code": "x" * 32},
                headers=[("Idempotency-Key", ACCEPT_KEY), ("Idempotency-Key", ACCEPT_KEY)],
            ).status_code,
            400,
        )
        self.issue(uid="demo-a-admin")

    def test_expiry_boundary_wrong_code_and_used_code(self) -> None:
        first, second = self.issue(), self.issue(ISSUE_KEY + "2")
        self.clock.advance(86399)
        self.accept(first.code)
        self.clock.advance(1)
        self.fails(lambda: self.accept(second.code, ACCEPT_KEY + "2"), ErrorCode.RESOURCE_EXPIRED)
        self.assertEqual(self.accept(first.code).version, 1)  # Durable claim replay.
        self.uid = "other-user"
        self.fails(lambda: self.accept(first.code), ErrorCode.RESOURCE_NOT_FOUND)
        self.fails(lambda: self.accept("wrong" * 8), ErrorCode.RESOURCE_NOT_FOUND)

    def test_failed_attempts_survive_rollback_keys_and_code_changes(self) -> None:
        code = self.issue().code
        self.store.retry_once = True
        for index in range(5):
            self.fails(
                partial(self.accept, str(index) * 32, ACCEPT_KEY + str(index)),
                ErrorCode.RESOURCE_NOT_FOUND,
            )
        self.fails(lambda: self.accept(code), ErrorCode.RATE_LIMITED)
        self.clock.advance(899)
        self.fails(lambda: self.accept(code), ErrorCode.RATE_LIMITED)
        self.clock.advance(1)
        self.assertEqual(self.accept(code).version, 1)
        self.assertEqual(
            next(
                doc["count"]
                for path, doc in self.store.data.items()
                if path.startswith("invitation_attempts/")
            ),
            1,
        )

    def test_active_member_cannot_change_role_and_replay_body_is_bound(self) -> None:
        first, second = self.issue(), self.issue(ISSUE_KEY + "2", "admin")
        self.accept(first.code)
        self.fails(lambda: self.accept(second.code), ErrorCode.IDEMPOTENCY_CONFLICT)
        self.fails(
            lambda: self.accept(second.code, ACCEPT_KEY + "2"), ErrorCode.OPERATION_FORBIDDEN
        )
        self.assertEqual(self.tenants.get(self.identity(self.uid), self.tenant).my_role, "member")

    def test_revoke_invalidates_unused_invites_and_old_acceptance_then_new_grant_joins(
        self,
    ) -> None:
        original, stale = self.issue(), self.issue(ISSUE_KEY + "stale")
        self.accept(original.code)
        revoked = self.revoke()
        epoch = self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"]
        AuthzChanges(self.store, self.ledger).execute(revoked, authorize=lambda tx, change: None)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], epoch)
        self.fails(lambda: self.accept(original.code), ErrorCode.OPERATION_FORBIDDEN)
        self.fails(
            lambda: self.accept(stale.code, ACCEPT_KEY + "stale"), ErrorCode.RESOURCE_EXPIRED
        )
        self.fails(
            lambda: self.tenants.get(self.identity(self.uid), self.tenant),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.clock.advance(900)
        fresh = self.issue(ISSUE_KEY + "fresh")
        joined = self.accept(fresh.code, ACCEPT_KEY + "fresh")
        self.assertEqual(joined.version, 3)
        self.fails(lambda: self.accept(original.code), ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.accept(fresh.code, ACCEPT_KEY + "fresh"), joined)

    def test_pending_deny_fences_old_active_database_before_epoch_changes(self) -> None:
        original, stale = self.issue(), self.issue(ISSUE_KEY + "stale")
        self.accept(original.code)
        deny = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=1,
            authorization_version=2,
            effect="deny",
            target=MemberTarget(user_id=FirebaseUid(self.uid), role=Role.MEMBER, status="revoked"),
        )
        self.ledger.prepare(deny)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"].get("invitation_epoch", 0), 0)
        self.fails(
            lambda: self.accept(stale.code, ACCEPT_KEY + "stale"), ErrorCode.OPERATION_FORBIDDEN
        )
        self.fails(
            lambda: self.tenants.get(self.identity(self.uid), self.tenant),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.fails(lambda: self.accept(original.code), ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.latest().state, "pending")
        AuthzChanges(self.store, self.ledger).execute(deny, authorize=lambda tx, change: None)
        self.assertEqual(
            self.accept(self.issue(ISSUE_KEY + "fresh").code, ACCEPT_KEY + "fresh").version, 3
        )

    def test_restore_latest_grant_invalidates_old_invites_once(self) -> None:
        first, stale = self.issue(), self.issue(ISSUE_KEY + "stale")
        self.accept(first.code)
        backup = copy.deepcopy(self.store.data)
        self.revoke()
        self.accept(self.issue(ISSUE_KEY + "new").code, ACCEPT_KEY + "new")
        latest = self.latest().change
        self.store.data = backup
        service = AuthzChanges(self.store, self.ledger)
        service.reapply(latest)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 1)
        service.reapply(latest)
        self.assertEqual(self.store.data[f"tenants/{self.tenant}"]["invitation_epoch"], 1)
        self.uid = "different-new-user"
        self.fails(lambda: self.accept(stale.code), ErrorCode.RESOURCE_EXPIRED)

    def test_pending_grant_never_authorizes_and_retry_rechecks_issuer(self) -> None:
        code = self.issue().code

        def fail(name: str) -> None:
            if "/commits/" in name:
                raise LedgerError()

        self.objects.before = fail
        self.fails(lambda: self.accept(code), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(self.latest().state, "pending")

        self.fails(
            lambda: self.tenants.get(self.identity(self.uid), self.tenant),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.tenants.list(self.identity(self.uid)).items, [])
        self.store.data[f"tenants/{self.tenant}/memberships/demo-a-owner"]["status"] = "revoked"
        self.objects.before = lambda name: None
        self.fails(lambda: self.accept(code), ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(self.latest().state, "pending")

    def test_missing_tracked_ledger_never_falls_back_to_bootstrap_authority(self) -> None:
        self.accept(self.issue(role="admin").code)
        self.objects.data.clear()
        self.fails(
            lambda: self.tenants.get(self.identity(self.uid), self.tenant),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.tenants.list(self.identity(self.uid)).items, [])
        self.fails(lambda: self.issue(uid=self.uid), ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(
            self.tenants.get(self.identity("demo-a-owner"), self.tenant).my_role, "owner"
        )

    def test_fault_matrix_preserves_operation_and_never_writes_ledger_inside_transaction(
        self,
    ) -> None:
        for phase in ("operations", "anchor", "intents", "commits"):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    self.uid = f"failure-user-{phase}-{after}"
                    invitation = self.issue(ISSUE_KEY + self.uid)

                    def failure(name: str, phase: str = phase) -> None:
                        self.assertFalse(self.store.in_transaction)
                        if phase in name:
                            raise LedgerError()

                    if after:
                        self.objects.after = failure
                    else:
                        self.objects.before = failure
                    self.fails(
                        partial(self.accept, invitation.code), ErrorCode.TEMPORARILY_UNAVAILABLE
                    )
                    receipt = next(
                        doc
                        for path, doc in self.store.data.items()
                        if "/invitation_acceptances/" in path
                        and doc["change"]["target"]["user_id"] == self.uid
                    )
                    operation = receipt["change"]["operation_id"]
                    self.fails(
                        lambda: self.tenants.get(self.identity(self.uid), self.tenant),
                        ErrorCode.RESOURCE_NOT_FOUND,
                    )
                    self.objects.before = self.objects.after = lambda name: None
                    self.assertEqual(self.accept(invitation.code).version, 1)
                    self.assertEqual(self.latest().change.operation_id, operation)

    def test_db_failures_and_lost_responses_resume_same_claim(self) -> None:
        for phase in (1, 2, 3, 4, 5):
            for after in (False, True):
                with self.subTest(phase=phase, after=after):
                    self.uid = f"db-failure-{phase}-{after}"
                    invitation = self.issue(ISSUE_KEY + self.uid)
                    baseline = self.store.calls

                    def fail(call: int, target: int = baseline + phase) -> None:
                        if call == target:
                            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

                    if after:
                        self.store.after = fail
                    else:
                        self.store.before = fail
                    self.fails(
                        partial(self.accept, invitation.code), ErrorCode.TEMPORARILY_UNAVAILABLE
                    )
                    self.store.before = self.store.after = lambda call: None
                    self.assertEqual(self.accept(invitation.code).version, 1)
                    members = [
                        doc
                        for path, doc in self.store.data.items()
                        if "/memberships/" in path and doc["user_id"] == self.uid
                    ]
                    self.assertEqual(len(members), 1)

    def test_rotation_keeps_existing_code_acceptable_but_reissue_requires_original_key(
        self,
    ) -> None:
        invitation = self.issue()
        self.repo = Invitations(
            self.store, self.tenants, self.ledger, code_key=b"y" * 32, now=self.clock.now
        )
        self.fails(self.issue, ErrorCode.RESOURCE_EXPIRED)
        self.assertEqual(self.accept(invitation.code).version, 1)

    def test_profiles_and_tenant_deletion_block_claim_and_replay(self) -> None:
        code = self.issue().code
        self.store.data["users/new"] = {
            "user_id": self.uid,
            "display_name": "Synthetic",
            "timezone": "UTC",
            "status": "deleting",
            "version": 1,
        }
        self.fails(lambda: self.accept(code), ErrorCode.OPERATION_FORBIDDEN)
        del self.store.data["users/new"]
        self.accept(code)
        self.store.data[f"tenants/{self.tenant}"]["status"] = "deleting"
        self.fails(lambda: self.accept(code), ErrorCode.RESOURCE_NOT_FOUND)


class LocalLedgerTests(unittest.TestCase):
    def test_atomic_create_persists_pending_deny_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = LocalAuthzObjects(directory)
            change = Change(
                operation_id=ResourceId(str(uuid4())),
                tenant_id=ResourceId(str(uuid4())),
                expected_version=1,
                authorization_version=2,
                effect="deny",
                target=MemberTarget(
                    user_id=FirebaseUid("opaque/uid"), role=Role.MEMBER, status="revoked"
                ),
            )
            ControlLedger(first).prepare(change)
            second = ControlLedger(LocalAuthzObjects(directory))
            self.assertEqual(second.latest(change.subject_key).state, "pending")  # type: ignore[union-attr]
            self.assertFalse(
                first.create(f"authz/v1/operations/{change.operation_id}.json", b"different")
            )
            self.assertEqual(second.latest(change.subject_key).change, change)  # type: ignore[union-attr]
            with self.assertRaises(LedgerError):
                first.read("authz/v1/../../escape")

    def test_unreadable_local_listing_and_storage_override_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            objects = LocalAuthzObjects(directory)
            with (
                patch.object(Path, "iterdir", side_effect=PermissionError()),
                self.assertRaises(LedgerError),
            ):
                objects.names("authz/v1/subjects/test/intents/")
        with (
            patch.dict(os.environ, {"STORAGE_EMULATOR_HOST": "http://127.0.0.1:9999"}),
            self.assertRaises(ConfigurationError),
        ):
            validate_sdk_environment(load_settings(CLOUD_ENV))

    def test_cloud_and_local_configuration_have_no_storage_fallback(self) -> None:
        for env in (
            {k: v for k, v in CLOUD_ENV.items() if k != "AUTHZ_LEDGER_BUCKET"},
            {**CLOUD_ENV, "AUTHZ_LEDGER_DIRECTORY": "/tmp/forbidden"},
            {k: v for k, v in EMULATOR_ENV.items() if k != "AUTHZ_LEDGER_DIRECTORY"},
            {**EMULATOR_ENV, "AUTHZ_LEDGER_BUCKET": "forbidden-cloud"},
            {**LOCAL_ENV, "AUTHZ_LEDGER_DIRECTORY": "/tmp/forbidden"},
        ):
            with self.assertRaises(ConfigurationError):
                load_settings(env)
