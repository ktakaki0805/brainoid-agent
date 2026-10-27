"""IAM-10 real Firestore acceptance contention and official bounded purge pages."""

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.memberships import MembershipPatch, Memberships
from brainoid_api.repositories.tenants import TenantPatch, Tenants
from brainoid_api.services.deletion_requests import (
    DeleteAccount,
    DeleteTenant,
    DeletionRequests,
    LeaveTenant,
)
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import operation_path
from brainoid_memory.lifecycle import KIND
from brainoid_persistence.firestore import FirestoreTransactions, PersistenceUnavailable
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.models import Role
from brainoid_worker.entry import Worker
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.local import LocalCaller, LocalQueue
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeDispatcher, PurgeResource
from fakes.authz_ledger import MemoryObjects
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture


class DeletionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)
        self.store = FirestoreTransactions(self.db)
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.ledger = ControlLedger(LocalAuthzObjects(self.directory))
        self.tenant, self.other = [
            ResourceId(d["fields"]["tenant_id"])
            for d in load_fixture()["documents"]
            if d["path"].startswith("tenants/") and d["path"].count("/") == 1
        ]
        cursors = Cursors(b"k" * 32, now=lambda: self.now)
        tenants = Tenants(self.store, cursors, ledger=self.ledger, now=lambda: self.now)
        self.members = Memberships(self.store, tenants, self.ledger, cursors, now=lambda: self.now)
        self.deletions = DeletionLedger(LocalAuthzObjects(self.directory))
        self.requests = DeletionRequests(
            self.store, self.deletions, self.ledger, now=lambda: self.now
        )

    def identity(self, user: str = "demo-a-owner") -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=FirebaseUid(user),
            issued_at=self.now,
            authenticated_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def _settings_reservation(self, kind: str) -> None:
        objects = MemoryObjects()
        requests = DeletionRequests(
            self.store, DeletionLedger(objects), self.ledger, now=lambda: self.now
        )
        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
        )
        document = self.db.document(f"tenants/{self.tenant}").get().to_dict()
        assert document is not None
        body = (
            DeleteTenant(kind="delete_tenant", confirmation_name=document["name"])
            if kind == "tenant"
            else DeleteAccount(kind="delete_account", confirm=True)
            if kind == "account"
            else LeaveTenant(kind="leave_tenant")
        )
        identity = self.identity("demo-a-owner" if kind == "tenant" else "demo-a-member")
        endpoint = None if kind == "account" else self.tenant

        def patch_settings() -> None:
            with self.assertRaises(APIError) as caught:
                tenants.update(
                    self.identity(),
                    self.tenant,
                    TenantPatch(name="Concurrent rename"),
                    "settings-during-reservation",
                    '"1"',
                )
            self.assertEqual(caught.exception.code, ErrorCode.CONFLICT)

        def after_ledger(name: str) -> None:
            # A separate Firestore transaction during the independent I/O gap.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(patch_settings).result(timeout=30)
            raise LedgerError()

        objects.after = after_ledger
        with self.assertRaises(APIError) as caught:
            requests.create(identity, body, "real-reserved-delete-key", endpoint)
        self.assertEqual(caught.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        patch_settings()
        objects.after = lambda name: None
        operation = requests.create(identity, body, "real-reserved-delete-key", endpoint)
        self.assertEqual(
            requests.create(identity, body, "real-reserved-delete-key", endpoint), operation
        )
        current = self.db.document(f"tenants/{self.tenant}").get().to_dict()
        assert current is not None
        self.assertEqual(current["name"], document["name"])
        self.assertEqual(current["version"], document["version"])
        self.assertEqual(len(objects.data), 1)
        self.assertIsNone(current.get("membership_mutation"))

    def test_settings_leave_reservation_is_serialized(self) -> None:
        self._settings_reservation("leave")

    def test_settings_account_reservation_is_serialized(self) -> None:
        self._settings_reservation("account")

    def test_settings_tenant_reservation_is_serialized(self) -> None:
        self._settings_reservation("tenant")

    def test_push_global_account_erasure_uses_bounded_pages_and_keeps_new_token_owner(self) -> None:
        from brainoid_api.repositories.push_installations import PushInstallations
        from brainoid_memory.push_installations import PushCreate, claim_path, installation_path
        from test_deletion_requests import FakeFinalizer

        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
        )
        registrations = PushInstallations(tenants)
        personal = registrations.mutate(
            self.identity("demo-a-member"),
            PushCreate(fcm_token="SYNTHETIC_MEMBER_TOKEN", platform="ios", app_version="1"),
            "member-push-registration",
        )
        transferred = registrations.mutate(
            self.identity("demo-a-member"),
            PushCreate(fcm_token="SYNTHETIC_TRANSFER_TOKEN", platform="ios", app_version="1"),
            "before-push-transfer",
        )
        current = registrations.mutate(
            self.identity(),
            PushCreate(fcm_token="SYNTHETIC_TRANSFER_TOKEN", platform="ios", app_version="1"),
            "after-push-transfer",
        )
        assert personal is not None and transferred is not None and current is not None
        operation = self.requests.create(
            self.identity("demo-a-member"),
            DeleteAccount(kind="delete_account", confirm=True),
            "real-push-account-delete",
        )
        jobs = PurgeJobs(Jobs(self.store, self.ledger, now=lambda: self.now), self.deletions)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            account_finalizer=FakeFinalizer(),
            page_size=1,
            step_budget=5000,
        )
        queue = LocalQueue()
        account = subject_id("demo-a-member")
        PurgeDispatcher(handler, self.store, queue).recover(account)
        queue.drain(Worker(jobs, LocalCaller(), {KIND: handler}))
        saved = self.db.document(operation_path(account, operation.operation_id)).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded", saved)
        for path in (
            installation_path(personal.id),
            installation_path(transferred.id),
            claim_path("SYNTHETIC_MEMBER_TOKEN"),
        ):
            self.assertFalse(self.db.document(path).get().exists)
        self.assertTrue(self.db.document(installation_path(current.id)).get().exists)
        retained = self.db.document(claim_path("SYNTHETIC_TRANSFER_TOKEN")).get().to_dict()
        assert retained is not None
        self.assertEqual(retained["user_id"], "demo-a-owner")
        with self.assertRaises(APIError):
            registrations.mutate(
                self.identity("demo-a-member"),
                PushCreate(fcm_token="SYNTHETIC_MEMBER_TOKEN", platform="ios", app_version="1"),
                "late-push-registration",
            )

    def test_global_device_credentials_use_official_bounded_pages(self) -> None:
        from brainoid_api.auth.device_tokens import DeviceTokens
        from brainoid_api.repositories.device_credentials import (
            DeviceCredentialRepository,
            DeviceRefresh,
            PairingConfirm,
            PairingCreate,
            PairingExchange,
        )
        from test_device_pairing import EPOCH, KEYRING

        tenants = Tenants(
            self.store,
            Cursors(b"k" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
        )
        tokens = DeviceTokens(
            KEYRING, project=PROJECT, credential_epoch=EPOCH, test_only=True, now=lambda: self.now
        )
        credentials = DeviceCredentialRepository(
            self.store, tenants, self.ledger, tokens, now=lambda: self.now
        )
        create_body = PairingCreate(device_name="Synthetic Pi", installation_nonce="z" * 32)
        pair = credentials.create(
            create_body,
            "real-create-device-key",
            "127.0.0.1",
        )
        credentials.confirm(
            self.identity(),
            self.tenant,
            PairingConfirm(
                user_code=pair.user_code, device_name="Synthetic Pi", allowed_entity_ids=[]
            ),
            "real-confirm-device-key",
            "127.0.0.1",
        )
        issued = credentials.exchange(
            pair.pairing_id,
            PairingExchange(poll_secret=pair.poll_secret),
            "real-exchange-device-key",
            "127.0.0.1",
        )
        credentials.refresh(
            DeviceRefresh(
                device_id=issued.device_id,
                refresh_token=issued.refresh_token,
                refresh_request_id=ResourceId(str(uuid4())),
            ),
            "real-refresh-device-key",
            "127.0.0.1",
        )
        family = issued.refresh_token.split(".")[0]
        pending = credentials.create(create_body, "real-pending-repair-key", "127.0.0.1")
        ghost = f"device_pairings/{pair.pairing_id}/legacy/missing/payloads/secret"
        self.db.document(ghost).set({"receipt": "synthetic-encrypted"})
        tenant = self.db.document(f"tenants/{self.tenant}").get().to_dict()
        assert tenant is not None
        operation = self.requests.create(
            self.identity(),
            DeleteTenant(kind="delete_tenant", confirmation_name=tenant["name"]),
            "real-device-delete-key",
            self.tenant,
        )
        for key in ("real-pending-repair-key", "real-fresh-repair-key"):
            with self.assertRaises(APIError) as caught:
                credentials.create(create_body, key, "127.0.0.1")
            self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_EXPIRED)
        jobs = PurgeJobs(Jobs(self.store, self.ledger, now=lambda: self.now), self.deletions)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=1,
            step_budget=5000,
        )
        queue = LocalQueue()
        PurgeDispatcher(handler, self.store, queue).recover(self.tenant)
        queue.drain(Worker(jobs, LocalCaller(), {KIND: handler}))
        saved = (
            self.db.document(operation_path(self.tenant, operation.operation_id)).get().to_dict()
        )
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded", saved)
        self.assertFalse(self.db.document(ghost).get().exists)
        self.assertEqual(
            self.db.document(f"device_credential_families/{family}").get().to_dict(),
            {
                "tenant_id": self.tenant,
                "device_id": issued.device_id,
                "status": "revoked",
            },
        )
        self.assertEqual(
            self.db.document(f"device_pairings/{pair.pairing_id}").get().to_dict(),
            {
                "tenant_id": self.tenant,
                "device_id": issued.device_id,
                "status": "expired",
            },
        )
        with self.assertRaises(APIError):
            credentials.authenticate(issued.access_token)
        self.assertEqual(
            self.db.document(f"device_pairings/{pending.pairing_id}").get().to_dict(),
            {
                "tenant_id": self.tenant,
                "status": "expired",
            },
        )

    def test_concurrent_owners_cannot_both_leave(self) -> None:
        self.members.update(
            self.identity(),
            self.tenant,
            FirebaseUid("demo-a-admin"),
            MembershipPatch(role=Role.OWNER),
            "promote-owner-key-01",
            '"1"',
            "test",
        )
        barrier = Barrier(2)

        def leave(user: str) -> object:
            barrier.wait(timeout=10)
            try:
                return self.requests.create(
                    self.identity(user),
                    LeaveTenant(kind="leave_tenant"),
                    "concurrent-leave-001",
                    self.tenant,
                )
            except APIError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(leave, ["demo-a-owner", "demo-a-admin"]))
        self.assertEqual(sum(not isinstance(result, APIError) for result in results), 1)
        owners = self.store.run(
            lambda tx: [
                d
                for _, d in tx.collection(f"tenants/{self.tenant}/memberships")
                if d["role"] == "owner" and d["status"] == "active"
            ]
        )
        self.assertEqual(len(owners), 1)

    def test_opaque_uid_multiple_memberships_and_lost_acceptance_response(self) -> None:
        uid = FirebaseUid("synthetic/user/../opaque")
        for tenant in (self.tenant, self.other):
            self.db.document(f"tenants/{tenant}/memberships/opaque-safe").set(
                {
                    "tenant_id": tenant,
                    "user_id": uid,
                    "role": "member",
                    "status": "active",
                    "authorization_version": 1,
                }
            )
        self.db.document("users/opaque-safe").set(
            {
                "user_id": uid,
                "display_name": "synthetic",
                "timezone": "Asia/Tokyo",
                "status": "active",
                "version": 1,
            }
        )
        original = self.db._firestore_api.commit
        lost = False

        def commit(**kwargs: Any) -> Any:
            nonlocal lost
            result = original(**kwargs)
            if not lost and any(
                "/operations/" in str(write) for write in kwargs["request"].get("writes", [])
            ):
                lost = True
                raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]
            return result

        with patch.object(self.db._firestore_api, "commit", side_effect=commit):
            with self.assertRaises(PersistenceUnavailable):
                self.requests.create(
                    self.identity(uid),
                    DeleteAccount(kind="delete_account", confirm=True),
                    "opaque-account-key-01",
                )
        self.assertTrue(lost)
        operation = self.requests.create(
            self.identity(uid),
            DeleteAccount(kind="delete_account", confirm=True),
            "opaque-account-key-01",
        )
        intent = self.deletions.get(operation.operation_id)
        assert intent is not None
        self.assertEqual(intent.tenant_id, subject_id(uid))
        self.assertEqual({s.tenant_id for s in intent.scopes[1:]}, {self.tenant, self.other})
        self.assertEqual(self.requests.get(self.identity(uid), operation.operation_id), operation)

    def test_official_paged_private_children_and_shared_retention(self) -> None:
        uid = "demo-a-member"
        note = ResourceId(str(uuid4()))
        path = f"tenants/{self.tenant}/notes/{note}"
        self.db.document(path).set(
            {"tenant_id": self.tenant, "owner_user_id": uid, "visibility": "private"}
        )
        children = [path + f"/parts/{i}/nested/ghost/deep/data" for i in range(4)]
        for child in children:
            self.db.document(child).set({"text": "synthetic private"})
        shared = f"tenants/{self.tenant}/notes/shared-retained"
        self.db.document(shared).set(
            {"tenant_id": self.tenant, "owner_user_id": uid, "visibility": "shared"}
        )
        operation = self.requests.create(
            self.identity(uid),
            LeaveTenant(kind="leave_tenant"),
            "paged-private-key-01",
            self.tenant,
        )
        normal = Jobs(self.store, self.ledger, now=lambda: self.now, allow_untracked_bootstrap=True)
        jobs = PurgeJobs(normal, self.deletions)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=2,
            step_budget=3000,
        )
        queue = LocalQueue()
        dispatcher = PurgeDispatcher(handler, self.store, queue)
        worker = Worker(jobs, LocalCaller(), {KIND: handler})
        self.assertIsNone(
            normal.prepare_dispatch(self.tenant, job_id(self.tenant, KIND, operation.operation_id))
        )
        dispatcher.recover(self.tenant)
        queue.drain(worker)
        saved = (
            self.db.document(operation_path(self.tenant, operation.operation_id)).get().to_dict()
        )
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded", saved)
        self.assertTrue(self.db.document(shared).get().exists)
        self.assertFalse(self.db.document(path).get().exists)
        for child in children:
            self.assertFalse(self.db.document(child).get().exists)

    def test_unprovisioned_account_completes_with_membership(self) -> None:
        from test_deletion_requests import FakeFinalizer

        uid = FirebaseUid("demo-a-member")
        profile = self.store.run(lambda tx: tx.query("users", {"user_id": uid}))[0][0]
        self.db.document(profile).delete()
        operation = self.requests.create(
            self.identity(uid),
            DeleteAccount(kind="delete_account", confirm=True),
            "unprovisioned-real-key",
        )
        anchor = subject_id(uid)
        finalizer = FakeFinalizer()
        jobs = PurgeJobs(Jobs(self.store, self.ledger, now=lambda: self.now), self.deletions)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            account_finalizer=finalizer,
            page_size=2,
            step_budget=3000,
        )
        queue = LocalQueue()
        PurgeDispatcher(handler, self.store, queue).recover(anchor)
        queue.drain(Worker(jobs, LocalCaller(), {KIND: handler}))
        saved = self.db.document(operation_path(anchor, operation.operation_id)).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded", saved)
        self.assertTrue(finalizer.calls)
        self.assertFalse(self.store.run(lambda tx: tx.memberships(uid)))

    def test_global_show_missing_parent_is_pending_without_scope_guess(self) -> None:
        from brainoid_jobs import RetryableError
        from brainoid_worker.entry import Execution
        from test_deletion_requests import FakeFinalizer

        uid = FirebaseUid("demo-a-member")
        profile, original = self.store.run(lambda tx: tx.query("users", {"user_id": uid}))[0]
        child = profile + "/push_installations/ghost"
        self.db.document(child).set({"token": "synthetic-only"})
        self.db.document(profile).delete()
        note = f"tenants/{self.tenant}/notes/known-private"
        self.db.document(note).set({"visibility": "private", "owner_user_id": uid})
        operation = self.requests.create(
            self.identity(uid),
            DeleteAccount(kind="delete_account", confirm=True),
            "unknown-parent-real-key",
        )
        anchor = subject_id(uid)
        finalizer = FakeFinalizer()
        jobs = PurgeJobs(Jobs(self.store, self.ledger, now=lambda: self.now), self.deletions)
        handler = PurgeResource(
            jobs,
            FirestorePurgePages(self.db),
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            account_finalizer=finalizer,
            page_size=2,
        )
        jid = job_id(anchor, KIND, operation.operation_id)
        jobs.activate(anchor, jid)
        delivery = jobs.prepare_dispatch(anchor, jid, queue_name=KIND)
        assert delivery is not None
        lease = jobs.acquire(delivery, known_handler=lambda kind: kind == KIND)
        assert lease is not None
        execution = Execution(jobs, lease)
        with self.assertRaises(RetryableError):
            for _ in range(3000):
                handler.step(execution)
        saved = self.db.document(operation_path(anchor, operation.operation_id)).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["purge"]["retry_reason"], "profile_parent_unknown")
        self.assertEqual(saved["operation"]["status"], "running")
        self.assertFalse(self.db.document(note).get().exists)
        self.assertTrue(self.db.document(child).get().exists)
        self.assertFalse(finalizer.calls)
        self.db.document(profile).set(original)
        for _ in range(3000):
            if handler.step(execution):
                jobs.complete(lease, result={"purged": True}, commit=handler.commit)
                break
        else:
            self.fail("canonical mapping did not unblock purge")
        self.assertFalse(self.db.document(child).get().exists)
        self.assertTrue(finalizer.calls)
