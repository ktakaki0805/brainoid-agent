"""IAM-10 / FR-013 / T-022 T-034 destructive scopes and crash recovery."""

import copy
import json
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid5

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.services.deletion_requests import (
    DeleteAccount,
    DeleteTenant,
    DeletionRequests,
    LeaveTenant,
)
from brainoid_contracts import FirebaseUid, IanaTimezone, ResourceId
from brainoid_contracts.deletion import exportable, subject_id
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import Operation, operation_path
from brainoid_memory.lifecycle import KIND
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import transaction_policy
from brainoid_worker.entry import Execution
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import PurgeResource
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import NOW, GraphStore
from fakes.purge import FakeInvalidation, FakePurgePages

USER = FirebaseUid("demo-a-member")
OWNER = FirebaseUid("demo-a-owner")
KEY = "lifecycle-request-test-0001"


class FakeFinalizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def finalize(self, user: str, *, key: str) -> str:
        self.calls.append((user, key))
        if self.fail:
            raise RuntimeError("synthetic provider failure")
        return "fake-identity-deleted-v1"


class DeletionFixture(unittest.TestCase):
    def setUp(self) -> None:
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        self.store = GraphStore()
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
        self.now = NOW
        self.objects = MemoryObjects()
        self.ledger = DeletionLedger(self.objects)
        self.authz = ControlLedger(MemoryObjects())
        self.repo = DeletionRequests(self.store, self.ledger, self.authz, now=lambda: self.now)
        self.normal = Jobs(
            self.store, self.authz, now=lambda: self.now, allow_untracked_bootstrap=True
        )
        self.jobs = PurgeJobs(self.normal, self.ledger)
        self.cache, self.export, self.finalizer = (
            FakeInvalidation(),
            FakeInvalidation(),
            FakeFinalizer(),
        )
        self.handler = PurgeResource(
            self.jobs,
            FakePurgePages(self.store),
            handlers=LifecycleHandlers(),
            hooks={"cache": self.cache, "export": self.export},
            account_finalizer=self.finalizer,
            page_size=2,
        )

    def identity(self, user: FirebaseUid = USER, age: float = 0) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=user,
            authenticated_at=self.now - timedelta(seconds=age),
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def accept(self, *, user: FirebaseUid = USER, account: bool = False) -> Operation:
        return self.repo.create(
            self.identity(user),
            DeleteAccount(kind="delete_account", confirm=True)
            if account
            else LeaveTenant(kind="leave_tenant"),
            KEY,
            None if account else self.tenant,
        )

    def drain(self, operation: Operation, anchor: ResourceId) -> None:
        identity = job_id(anchor, KIND, operation.operation_id)
        self.assertTrue(self.jobs.activate(anchor, identity))
        delivery = self.jobs.prepare_dispatch(anchor, identity, queue_name=KIND)
        assert delivery is not None
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        assert lease is not None
        execution = Execution(self.jobs, lease)
        for _ in range(5000):
            if self.handler.step(execution):
                self.jobs.complete(lease, result={"purged": True}, commit=self.handler.commit)
                return
        self.fail("purge failed to terminate")


class DeletionTests(DeletionFixture):
    def test_recent_auth_boundary_and_last_owner(self) -> None:
        self.repo.create(self.identity(age=300), LeaveTenant(kind="leave_tenant"), KEY, self.tenant)
        for age in (300.001, 301):
            with self.assertRaises(APIError) as error:
                self.repo.create(
                    self.identity(age=age), LeaveTenant(kind="leave_tenant"), KEY, self.tenant
                )
            self.assertEqual(error.exception.code, ErrorCode.REAUTH_REQUIRED)
        with self.assertRaises(APIError) as error:
            self.repo.create(
                self.identity(OWNER), LeaveTenant(kind="leave_tenant"), KEY, self.tenant
            )
        self.assertEqual(error.exception.code, ErrorCode.OWNERSHIP_TRANSFER_REQUIRED)

    def test_leave_fences_and_same_key_retry_without_membership(self) -> None:
        operation = self.accept()
        self.assertEqual(self.accept(), operation)
        self.assertEqual(
            self.repo.get(self.identity(), operation.operation_id, self.tenant), operation
        )
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: transaction_policy(
                    tx, ledger=self.authz, allow_untracked_bootstrap=True
                ).principal(self.tenant, UserSubject(user_id=USER))
            )
        with self.assertRaises(APIError):
            self.repo.get(self.identity(OWNER), operation.operation_id, self.tenant)
        with self.assertRaises(APIError) as error:
            self.repo.create(self.identity(), LeaveTenant(kind="leave_tenant"), KEY, self.other)
        self.assertEqual(error.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.store.run(lambda tx: account_stamp(tx, USER))

    def test_tenant_requires_current_owner_exact_confirmation(self) -> None:
        name = self.store.data[f"tenants/{self.tenant}"]["name"]
        for user, tenant, confirmation in (
            (USER, self.tenant, name),
            (OWNER, self.other, name),
            (OWNER, self.tenant, "wrong"),
        ):
            with self.assertRaises(APIError):
                self.repo.create(
                    self.identity(user),
                    DeleteTenant(kind="delete_tenant", confirmation_name=confirmation),
                    KEY,
                    tenant,
                )
        operation = self.repo.create(
            self.identity(OWNER),
            DeleteTenant(kind="delete_tenant", confirmation_name=name),
            KEY,
            self.tenant,
        )
        self.assertEqual(operation.kind, "delete_tenant")
        self.store.data[f"tenants/{self.tenant}"]["status"] = "active"
        with self.assertRaises(APIError):
            self.repo.create(
                self.identity(), LeaveTenant(kind="leave_tenant"), KEY + "stale-root", self.tenant
            )
        with self.assertRaises(PolicyDenied):
            self.store.run(
                lambda tx: transaction_policy(tx).principal(self.tenant, UserSubject(user_id=USER))
            )

    def test_unprovisioned_account_with_membership_has_full_deletion_path(self) -> None:
        profile = next(
            p
            for p, d in self.store.data.items()
            if p.startswith("users/") and d.get("user_id") == USER
        )
        del self.store.data[profile]
        operation = self.accept(account=True)
        self.drain(operation, subject_id(USER))
        self.assertTrue(self.finalizer.calls)
        self.assertFalse(
            any(
                "/memberships/" in p and d.get("user_id") == USER
                for p, d in self.store.data.items()
            )
        )
        with self.assertRaises(AccountUnavailable):
            self.store.run(lambda tx: account_stamp(tx, USER))

    def test_unknown_profile_parent_keeps_fixed_reason_but_purges_known_scope(self) -> None:
        from brainoid_jobs import RetryableError

        profile = next(
            p
            for p, d in self.store.data.items()
            if p.startswith("users/") and d.get("user_id") == USER
        )
        original = self.store.data.pop(profile)
        child = profile + "/push_installations/ghost"
        self.store.data[child] = {"token": "synthetic"}
        note = f"tenants/{self.tenant}/notes/private-to-erase"
        self.store.data[note] = {"visibility": "private", "owner_user_id": USER}
        operation = self.accept(account=True)
        anchor = subject_id(USER)
        jid = job_id(anchor, KIND, operation.operation_id)
        self.jobs.activate(anchor, jid)
        delivery = self.jobs.prepare_dispatch(anchor, jid, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        with self.assertRaises(RetryableError):
            for _ in range(5000):
                self.handler.step(execution)
        saved = self.store.data[operation_path(anchor, operation.operation_id)]
        self.assertEqual(saved["operation"]["status"], "running")
        self.assertEqual(saved["purge"]["retry_reason"], "profile_parent_unknown")
        self.assertNotIn(note, self.store.data)
        self.assertIn(child, self.store.data)
        self.assertFalse(self.finalizer.calls)
        # Canonical repair establishes ownership; it is not inferred from the path.
        self.store.data[profile] = original
        for _ in range(5000):
            if self.handler.step(execution):
                self.jobs.complete(lease, result={"purged": True}, commit=self.handler.commit)
                break
        else:
            self.fail("canonical profile repair did not allow completion")
        self.assertNotIn(child, self.store.data)
        self.assertTrue(self.finalizer.calls)

    def test_account_all_memberships_and_global_scope(self) -> None:
        self.store.data[f"tenants/{self.other}/memberships/second"] = {
            "tenant_id": self.other,
            "user_id": USER,
            "role": "member",
            "status": "active",
            "authorization_version": 1,
        }
        operation = self.accept(account=True)
        intent = self.ledger.get(operation.operation_id)
        assert intent is not None
        self.assertEqual({s.tenant_id for s in intent.scopes[1:]}, {self.tenant, self.other})
        self.assertNotIn(USER.encode(), next(iter(self.objects.data.values())))
        self.assertEqual(self.accept(account=True), operation)
        with self.assertRaises(AccountUnavailable):
            self.store.run(lambda tx: account_stamp(tx, USER))
        self.assertEqual(self.repo.get(self.identity(), operation.operation_id), operation)

    def test_last_owner_in_other_membership_rejects_everything(self) -> None:
        for p, doc in list(self.store.data.items()):
            if p.startswith(f"tenants/{self.other}/memberships/"):
                doc["role"] = "member"
        self.store.data[f"tenants/{self.other}/memberships/second"] = {
            "tenant_id": self.other,
            "user_id": USER,
            "role": "owner",
            "status": "active",
            "authorization_version": 1,
        }
        before = copy.deepcopy(self.store.data)
        with self.assertRaises(APIError) as error:
            self.accept(account=True)
        self.assertEqual(error.exception.code, ErrorCode.OWNERSHIP_TRANSFER_REQUIRED)
        self.assertEqual(self.store.data, before)
        self.assertFalse(self.objects.data)

    def test_ledger_failures_and_commit_loss_resume(self) -> None:
        def fail(name: str) -> None:
            raise LedgerError()

        self.objects.before = fail
        with self.assertRaises(APIError) as error:
            self.accept()
        self.assertEqual(error.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertFalse(any("/operations/" in p for p in self.store.data))
        self.objects.before = lambda name: None
        self.objects.after = fail
        with self.assertRaises(APIError):
            self.accept()
        self.objects.after = lambda name: None
        operation = self.accept()
        self.assertEqual(self.accept(), operation)
        self.assertEqual(len(self.objects.data), 1)

    def test_private_subtrees_credentials_shared_and_other_owner_preserved(self) -> None:
        root = f"tenants/{self.tenant}"
        private = root + "/notes/" + str(uuid5(UUID(self.tenant), "private"))
        shared, other = root + "/notes/shared", root + "/notes/other"
        self.store.data.update(
            {
                private: {
                    "tenant_id": self.tenant,
                    "owner_user_id": USER,
                    "visibility": "private",
                    "deleted": False,
                    "deletion_epoch": 0,
                },
                private + "/parts/one/deep/two": {"text": "erase"},
                shared: {"owner_user_id": USER, "visibility": "shared", "text": "retain"},
                other: {"owner_user_id": OWNER, "visibility": "private", "text": "retain"},
                root + "/integrations/personal": {
                    "owner_user_id": USER,
                    "visibility": "private",
                    "secret_ref": "fake",
                },
                root + "/private_sessions/self": {"user_id": USER},
            }
        )
        operation = self.accept()
        self.assertFalse(
            self.store.run(
                lambda tx: exportable(
                    tx,
                    self.tenant,
                    "notes",
                    ResourceId(private.rsplit("/", 1)[-1]),
                    tx.get(private) or {},
                )
            )
        )
        self.drain(operation, self.tenant)
        self.assertFalse(any(p.startswith(private) for p in self.store.data))
        self.assertIn(shared, self.store.data)
        self.assertIn(other, self.store.data)
        self.assertNotIn(root + "/integrations/personal", self.store.data)
        self.assertNotIn(root + "/private_sessions/self", self.store.data)
        self.assertEqual(
            self.repo.get(self.identity(), operation.operation_id, self.tenant).status, "succeeded"
        )

    def test_account_profile_children_and_provider_complete_then_status_gone(self) -> None:
        profile = next(
            p
            for p, d in self.store.data.items()
            if p.startswith("users/") and d.get("user_id") == USER
        )
        self.store.data[profile + "/push_installations/phone"] = {
            "token": "fake-only",
            "tenant_id": self.tenant,
        }
        operation = self.accept(account=True)
        self.drain(operation, subject_id(USER))
        self.assertNotIn(profile, self.store.data)
        self.assertFalse(any(p.startswith(profile + "/") for p in self.store.data))
        self.assertEqual(self.finalizer.calls[0][0], USER)
        with self.assertRaises(AccountUnavailable):
            self.store.run(lambda tx: account_stamp(tx, USER))
        with self.assertRaises(APIError):
            self.repo.get(self.identity(), operation.operation_id)

    def test_tenant_erasure_keeps_authority_and_clears_arbitrary_children(self) -> None:
        root = f"tenants/{self.tenant}"
        self.store.data[root + "/unknown_collection/missing/secret/one"] = {"text": "erase"}
        name = self.store.data[root]["name"]
        operation = self.repo.create(
            self.identity(OWNER),
            DeleteTenant(kind="delete_tenant", confirmation_name=name),
            KEY,
            self.tenant,
        )
        self.drain(operation, self.tenant)
        remaining = [p for p in self.store.data if p.startswith(root + "/")]
        self.assertTrue(remaining)
        self.assertFalse(any("/unknown_collection/" in p for p in remaining))
        self.assertTrue(
            all(
                p.split("/")[2] in {"operations", "jobs", "lifecycle_selections"} for p in remaining
            )
        )
        self.assertNotIn("name", self.store.data[root])
        account = self.repo.create(
            self.identity(OWNER),
            DeleteAccount(kind="delete_account", confirm=True),
            KEY + "-account",
        )
        self.assertEqual(account.kind, "delete_account")

    def test_leave_rejoin_after_completion_and_old_private_restore_fence(self) -> None:
        from brainoid_api.common.pagination import Cursors
        from brainoid_api.repositories.invitations import (
            InvitationAccept,
            InvitationCreate,
            Invitations,
        )
        from brainoid_api.repositories.tenants import Tenants

        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.authz,
            now=lambda: self.now,
        )
        invites = Invitations(
            self.store, tenants, self.authz, code_key=b"k" * 32, now=lambda: self.now
        )
        private = f"tenants/{self.tenant}/notes/{uuid5(UUID(self.tenant), 'old-private')}"
        old = {
            "tenant_id": self.tenant,
            "owner_user_id": USER,
            "visibility": "private",
            "deleted": False,
            "deletion_epoch": 0,
        }
        self.store.data[private] = old
        operation = self.accept()
        invite = invites.create(
            self.identity(OWNER), self.tenant, InvitationCreate(role="member"), KEY + "invite"
        )
        with self.assertRaises(APIError):
            invites.accept(self.identity(), InvitationAccept(code=invite.code), KEY + "accept")
        self.drain(operation, self.tenant)
        result = invites.accept(self.identity(), InvitationAccept(code=invite.code), KEY + "accept")
        self.assertEqual(result.version, 3)
        self.store.run(
            lambda tx: transaction_policy(
                tx, ledger=self.authz, allow_untracked_bootstrap=True
            ).principal(self.tenant, UserSubject(user_id=USER))
        )
        self.store.data[private] = old
        self.assertFalse(
            self.store.run(
                lambda tx: exportable(
                    tx, self.tenant, "notes", ResourceId(private.rsplit("/", 1)[-1]), old
                )
            )
        )
        fresh = ResourceId(str(uuid5(UUID(self.tenant), "new-private")))
        self.assertTrue(self.store.run(lambda tx: exportable(tx, self.tenant, "notes", fresh, old)))
        old_job = self.jobs.get(self.tenant, job_id(self.tenant, KIND, operation.operation_id))
        assert old_job is not None
        with self.assertRaises(ValueError):
            self.store.run(lambda tx: self.jobs.accepted(tx, old_job))
        repeated = self.repo.create(
            self.identity(), LeaveTenant(kind="leave_tenant"), KEY + "-again", self.tenant
        )
        self.assertNotEqual(repeated.operation_id, operation.operation_id)

    def test_leave_then_account_uses_existing_member_fence(self) -> None:
        leave = self.accept()
        self.drain(leave, self.tenant)
        account = self.repo.create(
            self.identity(), DeleteAccount(kind="delete_account", confirm=True), KEY + "account"
        )
        self.drain(account, subject_id(USER))

    def test_account_reservation_blocks_new_tenant_join_and_owner_transfer(self) -> None:
        from brainoid_api.common.pagination import Cursors
        from brainoid_api.repositories.invitations import (
            InvitationAccept,
            InvitationCreate,
            Invitations,
        )
        from brainoid_api.repositories.memberships import Memberships, OwnerTransfer
        from brainoid_api.repositories.tenants import TenantCreate, Tenants

        cursors = Cursors(b"k" * 32, now=lambda: self.now)
        tenants = Tenants(self.store, cursors, ledger=self.authz, now=lambda: self.now)
        members = Memberships(self.store, tenants, self.authz, cursors, now=lambda: self.now)
        invites = Invitations(
            self.store, tenants, self.authz, code_key=b"k" * 32, now=lambda: self.now
        )
        invite = invites.create(
            self.identity(OWNER), self.tenant, InvitationCreate(role="member"), KEY + "invite"
        )

        def concurrent(name: str) -> None:
            for action in (
                lambda: tenants.create(
                    self.identity(),
                    TenantCreate(name="new", timezone=IanaTimezone("Asia/Tokyo")),
                    KEY + "new",
                ),
                lambda: invites.accept(
                    self.identity(), InvitationAccept(code=invite.code), KEY + "accept"
                ),
                lambda: members.transfer(
                    self.identity(OWNER),
                    self.tenant,
                    OwnerTransfer(target_user_id=USER, target_version=1),
                    KEY + "transfer",
                    '"1"',
                    "test",
                ),
            ):
                with self.assertRaises(APIError):
                    action()

        self.objects.before = concurrent
        self.accept(account=True)

    def test_database_before_and_after_commits_resume_same_intent(self) -> None:
        from brainoid_persistence.firestore import PersistenceUnavailable

        for commit_number in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            with self.subTest(commit=commit_number):
                self.setUp()
                count = 0

                def lost(commit_number: int = commit_number) -> None:
                    nonlocal count
                    count += 1
                    if count == commit_number:
                        raise PersistenceUnavailable()

                self.store.after_commit = lost
                try:
                    first = self.accept(account=True)
                except PersistenceUnavailable:
                    first = None
                self.store.after_commit = lambda: None
                second = self.accept(account=True)
                if first is not None:
                    self.assertEqual(first, second)
                self.assertEqual(len(self.objects.data), 1)
                self.assertEqual(second.status, "pending")

    def test_expiry_during_ledger_is_not_accepted(self) -> None:
        self.objects.after = lambda name: setattr(self, "now", self.now + timedelta(seconds=301))
        with self.assertRaises(APIError) as error:
            self.accept()
        self.assertEqual(error.exception.code, ErrorCode.REAUTH_REQUIRED)
        self.assertFalse(any("/operations/" in p for p in self.store.data))
        self.objects.after = lambda name: None
        self.accept()

    def test_normal_jobs_cannot_dispatch_lifecycle_and_scope_cursor_tamper_fails(self) -> None:
        operation = self.accept()
        jid = job_id(self.tenant, KIND, operation.operation_id)
        self.assertIsNone(self.normal.prepare_dispatch(self.tenant, jid))
        self.jobs.activate(self.tenant, jid)
        delivery = self.jobs.prepare_dispatch(self.tenant, jid, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda k: k == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        self.handler.step(execution)
        saved = self.store.data[operation_path(self.tenant, operation.operation_id)]
        saved["purge"]["stack"][-1]["path"] = f"tenants/{self.other}"
        with self.assertRaises(ValueError):
            self.handler.step(execution)

    def test_provider_failure_keeps_operation_running_and_stable_key_retry(self) -> None:
        from brainoid_jobs import RetryableError

        operation = self.accept(account=True)
        anchor = subject_id(USER)
        jid = job_id(anchor, KIND, operation.operation_id)
        self.jobs.activate(anchor, jid)
        delivery = self.jobs.prepare_dispatch(anchor, jid, queue_name=KIND)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda k: k == KIND)
        assert lease is not None
        execution = Execution(self.jobs, lease)
        self.finalizer.fail = True
        with self.assertRaises(RetryableError):
            for _ in range(5000):
                self.handler.step(execution)
        self.assertEqual(self.repo.get(self.identity(), operation.operation_id).status, "running")
        self.finalizer.fail = False
        for _ in range(10):
            if self.handler.step(execution):
                self.jobs.complete(lease, result={"purged": True}, commit=self.handler.commit)
                break
        self.assertEqual(self.finalizer.calls[0], self.finalizer.calls[1])

    def test_http_self_account_input_and_status_exception_are_narrow(self) -> None:
        from brainoid_api.app import create_app
        from brainoid_api.auth.runtime import (
            AuthServices,
            authenticated_identity,
            get_auth_services,
        )
        from brainoid_api.common.pagination import Cursors
        from brainoid_api.repositories.tenants import Tenants
        from brainoid_api.settings import load_settings
        from starlette.testclient import TestClient
        from test_api_foundation import LOCAL_ENV

        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.authz,
            now=lambda: self.now,
        )
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = self.identity
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=tenants, deletion_requests=self.repo
        )
        with TestClient(app) as client:
            headers = {"Idempotency-Key": KEY}
            self.assertEqual(
                client.post(
                    "/v1/operations",
                    headers=headers,
                    json={"kind": "delete_account", "confirm": True, "user_id": OWNER},
                ).status_code,
                422,
            )
            response = client.post(
                "/v1/operations", headers=headers, json={"kind": "delete_account", "confirm": True}
            )
            self.assertEqual(response.status_code, 202, response.text)
            identity = response.json()["operation_id"]
            self.assertEqual(client.get(f"/v1/operations/{identity}").status_code, 200)
            self.assertEqual(
                client.post(
                    "/v1/operations",
                    headers=headers,
                    json={"kind": "delete_account", "confirm": True},
                ).json(),
                response.json(),
            )
            self.assertEqual(client.get("/v1/tenants").status_code, 403)
            self.assertEqual(
                client.post(
                    "/v1/operations",
                    headers={"Idempotency-Key": KEY + "other"},
                    json={"kind": "delete_account", "confirm": True},
                ).status_code,
                403,
            )

    def test_database_failure_before_ledger_and_after_ledger_remains_retryable(self) -> None:
        self.store.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.accept()
        self.assertFalse(self.objects.data)
        self.store.fail_commit = False
        self.objects.after = lambda name: setattr(self.store, "fail_commit", True)
        with self.assertRaises(RuntimeError):
            self.accept()
        self.assertEqual(len(self.objects.data), 1)
        self.assertFalse(any("/operations/" in path for path in self.store.data))
        self.store.fail_commit = False
        self.objects.after = lambda name: None
        operation = self.accept()
        self.assertEqual(operation.status, "pending")

    def test_pending_tenant_deletion_allows_owner_to_request_own_account(self) -> None:
        name = self.store.data[f"tenants/{self.tenant}"]["name"]
        self.repo.create(
            self.identity(OWNER),
            DeleteTenant(kind="delete_tenant", confirmation_name=name),
            KEY,
            self.tenant,
        )
        operation = self.repo.create(
            self.identity(OWNER),
            DeleteAccount(kind="delete_account", confirm=True),
            KEY + "account",
        )
        self.assertEqual(operation.kind, "delete_account")


class LegacyForgetCompatibilityTests(unittest.TestCase):
    def test_old_immutable_json_and_saved_receipt_still_purge_and_replay(self) -> None:
        from brainoid_memory.forget import ForgetCreate
        from test_memory_purge import PurgeFixture

        fixture = PurgeFixture()
        fixture.setUp()
        fixture.accept()
        saved = fixture.store.data[fixture.op_path]
        old = {
            key: value for key, value in saved["intent"].items() if key not in {"kind", "scopes"}
        }
        self.assertEqual(old, saved["intent"])
        fixture.store.data[fixture.op_path]["intent"] = old
        assert isinstance(fixture.ledger.objects, MemoryObjects)
        fixture.ledger.objects.data[fixture.ledger.name(fixture.operation.operation_id)] = (
            json.dumps(old).encode()
        )
        fixture.drain()
        from fakes.memory_repository import FACT, context

        result = fixture.acceptor.forget(
            lambda tx: fixture.repo.facts(tx, context()),
            ForgetCreate(kind="forget_memory", target_id=FACT, expected_version=1),
            "purge-fixture-request-001",
        )
        self.assertEqual(result.operation_id, fixture.operation.operation_id)
        self.assertEqual(result.status, "succeeded")
