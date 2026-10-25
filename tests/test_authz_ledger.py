"""T-028: interrupted authorization changes, immutable retries, and restore fences."""

import copy
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier
from typing import Literal
from uuid import uuid4

from brainoid_api.auth.authorization import TransactionPolicyState
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    DeviceTarget,
    LedgerConflict,
    LedgerError,
    MemberTarget,
)
from brainoid_policy.models import Role
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from pydantic import ValidationError
from test_common_mutations import MemoryStore

TENANT = ResourceId("00000000-0000-0000-0000-000000000001")
UID = FirebaseUid("synthetic/user/opaque")


def change(
    *,
    version: int = 2,
    status: Literal["active", "revoked"] = "revoked",
    role: Role = Role.MEMBER,
) -> Change:
    return Change(
        operation_id=ResourceId(str(uuid4())),
        tenant_id=TENANT,
        expected_version=version - 1,
        authorization_version=version,
        effect="deny" if status == "revoked" else "grant",
        target=MemberTarget(user_id=UID, role=role, status=status),
    )


class FaultStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.before: Callable[[int], None] = lambda call: None
        self.after: Callable[[int], None] = lambda call: None
        self.in_transaction = False

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        self.calls += 1
        call = self.calls
        self.before(call)
        self.in_transaction = True
        try:
            result = super().run(work)
        finally:
            self.in_transaction = False
        self.after(call)
        return result


class AuthzLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.objects = MemoryObjects()
        self.ledger = ControlLedger(self.objects)
        self.store = FaultStore()
        self.service = AuthzChanges(self.store, self.ledger)
        self.path = f"tenants/{TENANT}/memberships/safe-key"
        self.store.data[self.path] = {
            "tenant_id": TENANT,
            "user_id": UID,
            "status": "active",
            "role": "admin",
            "authorization_version": 1,
            "unrelated": "preserved",
        }
        self.backup = copy.deepcopy(self.store.data)
        self.authorizations = 0

    def authorize(self, tx: Transaction, proposed: Change) -> None:
        self.authorizations += 1
        self.assertEqual(proposed.tenant_id, TENANT)
        self.assertTrue(self.store.in_transaction)

    def execute(self, proposed: Change) -> None:
        self.service.execute(proposed, authorize=self.authorize)

    def status(self, *, bootstrap: bool = False) -> str:
        state = self.store.run(
            lambda tx: TransactionPolicyState(
                tx, ledger=self.ledger, allow_untracked_bootstrap=bootstrap
            ).membership(TENANT, UID)
        )
        assert state is not None
        return state.status

    def fails(self, work: Callable[[], object], code: ErrorCode) -> None:
        with self.assertRaises(APIError) as failure:
            work()
        self.assertEqual(failure.exception.code, code)

    def test_allowlist_and_strict_db_integer_not_authorization_stamp(self) -> None:
        data = change().model_dump(mode="json")
        for field, value in (("authorization_version", True), ("expected_version", "a" * 64)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Change.model_validate({**data, field: value})
        with self.assertRaises(ValidationError):
            Change.model_validate({**data, "token": "synthetic-secret"})
        with self.assertRaises(ValidationError):
            MemberTarget(user_id=UID, role=Role.MEMBER, status="active", body="content")  # type: ignore[call-arg]
        device = {"device_id": str(uuid4()), "status": "active"}
        for fields in (
            {"allowed_scopes": ["synthetic-token"]},
            {"allowed_entity_ids": [str(uuid4())] * 101},
        ):
            with self.assertRaises(ValidationError):
                DeviceTarget.model_validate({**device, **fields})

    def test_authorization_is_required_before_any_external_record(self) -> None:
        def deny(tx: Transaction, proposed: Change) -> None:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)

        self.fails(
            lambda: self.service.execute(change(), authorize=deny), ErrorCode.OPERATION_FORBIDDEN
        )
        self.assertFalse(self.objects.data)

    def test_no_external_writes_in_retried_database_callback(self) -> None:
        self.objects.before = lambda name: self.assertFalse(self.store.in_transaction)
        self.store.retry_once = True
        proposed = change()
        self.execute(proposed)
        self.assertEqual(self.objects.writes, 4)
        self.assertEqual(self.store.data[self.path]["unrelated"], "preserved")
        self.assertGreaterEqual(self.authorizations, 3)
        self.assertTrue(all(UID not in key for key in self.objects.data))
        self.assertNotIn(b"unrelated", b"".join(self.objects.data.values()))

    def test_failure_before_and_after_each_immutable_write_retries_same_operation(self) -> None:
        for suffix in ("operations/", "anchor.json", "intents/", "commits/"):
            for after in (False, True):
                with self.subTest(suffix=suffix, after=after):
                    self.setUp()
                    proposed = change(status="active", role=Role.MEMBER)

                    def fail(name: str, suffix: str = suffix) -> None:
                        if suffix in name:
                            raise LedgerError()

                    if after:
                        self.objects.after = fail
                    else:
                        self.objects.before = fail
                    self.fails(partial(self.execute, proposed), ErrorCode.TEMPORARILY_UNAVAILABLE)
                    self.assertNotEqual(self.status(), "active")
                    self.objects.before = self.objects.after = lambda name: None
                    self.execute(proposed)
                    self.assertEqual(self.status(), "active")
                    count = self.objects.writes
                    self.execute(proposed)
                    self.assertEqual(self.objects.writes, count)

    def test_db_failures_and_ambiguous_commits_preserve_deny_and_retry(self) -> None:
        for call in (2, 3, 4):
            for after in (False, True):
                with self.subTest(call=call, after=after):
                    self.setUp()
                    proposed = change()

                    def fail(index: int, call: int = call) -> None:
                        if index == call:
                            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

                    if after:
                        self.store.after = fail
                    else:
                        self.store.before = fail
                    self.fails(partial(self.execute, proposed), ErrorCode.TEMPORARILY_UNAVAILABLE)
                    self.store.before = self.store.after = lambda index: None
                    self.assertNotEqual(self.status(bootstrap=True), "active")
                    self.execute(proposed)
                    self.assertEqual(self.status(), "revoked")

    def test_unconfirmed_grant_restore_stays_suspended_without_creating_commit(self) -> None:
        proposed = change(status="active", role=Role.OWNER)
        self.ledger.prepare(proposed)
        before = copy.deepcopy(self.objects.data)
        for _ in range(2):
            result = self.service.reapply(proposed)
            self.assertEqual(result.state, "suspended")
            self.assertEqual(self.status(), "suspended")
        self.assertEqual(before, self.objects.data)
        self.assertFalse(any("commits/" in name for name in self.objects.data))

    def test_latest_deny_reapplied_over_old_grant_and_stale_accept_replay_rejected(self) -> None:
        grant = change(status="active", role=Role.ADMIN)
        self.execute(grant)
        revoke = change(version=3)
        self.execute(revoke)
        self.store.data = copy.deepcopy(self.backup)
        self.assertEqual(self.status(), "suspended")
        result = self.service.reapply(grant)
        self.assertEqual(result.operation_id, revoke.operation_id)
        state = copy.deepcopy(self.store.data)
        self.service.reapply(grant)
        self.assertEqual(self.store.data, state)
        self.assertEqual(self.status(), "revoked")
        self.fails(lambda: self.execute(grant), ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.status(), "revoked")

    def test_partial_reduction_commits_remaining_membership_and_device_permissions(self) -> None:
        reduction = change(status="active").model_copy(update={"effect": "deny"})
        self.execute(reduction)
        state = self.store.run(
            lambda tx: TransactionPolicyState(tx, ledger=self.ledger).membership(TENANT, UID)
        )
        assert state is not None
        self.assertEqual((state.status, state.role), ("active", Role.MEMBER))
        device_id = ResourceId(str(uuid4()))
        path = f"tenants/{TENANT}/devices/{device_id}"
        self.store.data[path] = {
            "tenant_id": TENANT,
            "id": device_id,
            "status": "active",
            "authorization_version": 1,
            "connection_generation": 1,
            "allowed_scopes": ["shared", "notes", "home.control"],
            "capabilities": ["display"],
        }
        device = Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=TENANT,
            expected_version=1,
            authorization_version=2,
            effect="deny",
            target=DeviceTarget(
                device_id=device_id, status="active", allowed_scopes=("shared", "notes")
            ),
        )
        self.execute(device)
        restored = self.store.run(
            lambda tx: TransactionPolicyState(tx, ledger=self.ledger).device(TENANT, device_id)
        )
        assert restored is not None
        self.assertEqual(restored.status, "active")
        self.assertEqual(restored.allowed_scopes, {"shared", "notes"})
        self.store.data[path]["allowed_scopes"].append("home.control")
        fenced = self.store.run(
            lambda tx: TransactionPolicyState(tx, ledger=self.ledger).device(TENANT, device_id)
        )
        assert fenced is not None
        self.assertEqual(fenced.status, "suspended")

    def test_operation_reuse_rejects_different_subject_or_payload(self) -> None:
        proposed = change()
        self.ledger.prepare(proposed)
        for target in (
            MemberTarget(user_id=UID, role=Role.OWNER, status="active"),
            MemberTarget(user_id=FirebaseUid("other"), role=Role.MEMBER, status="revoked"),
        ):
            with self.assertRaises(LedgerConflict):
                self.ledger.prepare(proposed.model_copy(update={"target": target}))

    def test_simultaneous_same_version_slot_only_one_wins(self) -> None:
        first, second = change(), change(status="active")
        barrier = Barrier(2)

        def synchronize(name: str) -> None:
            if "/intents/" in name:
                barrier.wait(timeout=5)

        self.objects.before = synchronize

        def reserve(proposed: Change) -> str:
            try:
                self.ledger.prepare(proposed)
                return "pending"
            except LedgerConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (first, second)))
        self.assertCountEqual(results, ["pending", "conflict"])
        self.assertEqual(self.status(bootstrap=True), "suspended")

    def test_new_deny_at_grant_activation_boundaries_never_restores_authority(self) -> None:
        for boundary in ("commit", "before_activation", "after_activation"):
            with self.subTest(boundary=boundary):
                self.setUp()
                grant, revoke = change(status="active"), change(version=3)

                def reserve(revoke: Change = revoke) -> None:
                    self.ledger.prepare(revoke)

                if boundary == "commit":
                    self.objects.after = lambda name: reserve() if "/commits/" in name else None
                elif boundary == "before_activation":
                    self.store.before = lambda call: reserve() if call == 4 else None
                else:
                    self.store.after = lambda call: reserve() if call == 4 else None
                self.fails(partial(self.execute, grant), ErrorCode.OPERATION_FORBIDDEN)
                self.objects.after = lambda name: None
                self.store.before = self.store.after = lambda call: None
                self.assertEqual(self.status(), "suspended")
                self.fails(partial(self.execute, grant), ErrorCode.OPERATION_FORBIDDEN)
                self.service.reapply(grant)
                self.assertEqual(self.store.data[self.path]["authorization_version"], 3)
                self.assertEqual(self.status(), "suspended")
                self.execute(revoke)
                self.assertEqual(self.status(), "revoked")

    def test_commit_requires_db_application_confirmation(self) -> None:
        proposed = change(status="active")

        def tamper(call: int) -> None:
            if call == 2:
                self.store.data[self.path]["authorization_operation_id"] = str(uuid4())

        self.store.after = tamper
        self.fails(lambda: self.execute(proposed), ErrorCode.OPERATION_FORBIDDEN)
        self.assertFalse(any("commits/" in name for name in self.objects.data))
        self.assertEqual(self.status(), "suspended")

    def test_bootstrap_missing_ledger_and_database_ahead_restore_are_not_grants(self) -> None:
        proposed = change()
        self.assertEqual(self.status(bootstrap=True), "active")
        self.assertEqual(self.status(), "suspended")
        self.assertEqual(self.service.reapply(proposed).state, "missing")
        self.execute(proposed)
        self.store.data[self.path].update(status="active", authorization_version=9)
        self.assertEqual(self.service.reapply(proposed).state, "suspended")
        self.assertEqual(self.store.data[self.path]["authorization_version"], 9)

    def test_corrupt_or_unavailable_ledger_fails_closed(self) -> None:
        proposed = change()
        self.ledger.prepare(proposed)
        name = next(name for name in self.objects.data if "/intents/" in name)
        self.objects.data[name] = b'{"token":"synthetic"}'
        with self.assertRaises(PolicyDenied):
            self.status(bootstrap=True)
        self.fails(lambda: self.service.reapply(proposed), ErrorCode.TEMPORARILY_UNAVAILABLE)

    def test_absent_membership_grant_uses_hashed_path_and_explicit_version_zero(self) -> None:
        self.store.data = {}
        proposed = change(version=1, status="active")
        self.execute(proposed)
        self.assertEqual(self.status(), "active")
        self.assertEqual(
            list(self.store.data), [f"tenants/{TENANT}/memberships/{proposed.subject_key}"]
        )
