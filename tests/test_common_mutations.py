"""Boundary tests for authorized replay, strict versions and scope-bound cursors."""

import copy
import unittest
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Document, Transaction
from brainoid_api.common.versioning import compare_and_write, etag, require_if_match
from brainoid_api.errors import ERRORS, APIError, ErrorCode
from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    PrivateSessionContext,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    Visibility,
)
from fakes.runtime import FakeClock
from pydantic import JsonValue, ValidationError

TENANT = ResourceId("00000000-0000-0000-0000-000000000001")
RESOURCE = ResourceId("00000000-0000-0000-0000-000000000002")
OTHER = ResourceId("00000000-0000-0000-0000-000000000003")
KEY = "fixed-test-key-001"


def scope(
    *, tenant: ResourceId = TENANT, actor: str = "alice", version: int = 1
) -> AuthorizedScope:
    return AuthorizedScope(
        context=TenantContext(
            tenant_id=tenant,
            actor=UserActorContext(
                actor_id=FirebaseUid(actor),
                allowed_scopes=frozenset({"notes:read", "notes:write"}),
                authorization_version=version,
            ),
        ),
        scope=Scope(visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.NEVER),
        owner_user_id=FirebaseUid(actor),
    )


class MemoryTransaction:
    """Copy-on-commit fake: failures and simulated retries discard all pending writes."""

    def __init__(self, data: dict[str, Document]) -> None:
        self.data = copy.deepcopy(data)

    def get(self, path: str) -> Document | None:
        return copy.deepcopy(self.data.get(path))

    def collection(self, path: str) -> list[tuple[str, Document]]:
        return [
            (key, copy.deepcopy(document))
            for key, document in self.data.items()
            if key.rsplit("/", 1)[0] == path
        ]

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        return [
            copy.deepcopy(document)
            for path, document in self.data.items()
            if path.rsplit("/", 1)[0] == collection and document.get(field) == value
        ][:2]

    def create(self, path: str, document: Document) -> None:
        if path in self.data:
            raise AssertionError("Duplicate creation")
        self.set(path, document)

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        return [
            (path, copy.deepcopy(document))
            for path, document in self.data.items()
            if path.split("/")[-2] == "memberships" and document.get("user_id") == user_id
        ]

    def set(self, path: str, document: Document) -> None:
        self.data[path] = copy.deepcopy(document)

    def delete(self, path: str) -> None:
        del self.data[path]


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, Document] = {}
        self.retry_once = False

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        if self.retry_once:
            self.retry_once = False
            work(MemoryTransaction(self.data))
        transaction = MemoryTransaction(self.data)
        result = work(transaction)
        self.data = transaction.data
        return result


class CommonMutationsTests(unittest.TestCase):
    def test_tenant_root_compare_and_write_preserves_tenant_boundary(self) -> None:
        store = MemoryStore()
        store.data[f"tenants/{TENANT}"] = {"version": 1, "name": "original"}
        result = store.run(
            lambda tx: compare_and_write(
                tx,
                authorized_scope=scope(),
                path=f"tenants/{TENANT}",
                if_match='"1"',
                replacement={"name": "updated"},
            )
        )
        self.assertEqual(result, 2)
        with self.assertRaises(APIError) as denied:
            store.run(
                lambda tx: compare_and_write(
                    tx,
                    authorized_scope=scope(),
                    path=f"tenants/{OTHER}",
                    if_match='"1"',
                    replacement={"name": "forbidden"},
                )
            )
        self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = MemoryStore()
        self.idempotency = Idempotency(self.store, now=self.clock.now)
        self.current = scope()
        self.allowed = True
        self.authorizations = 0
        self.mutations = 0
        self.path = f"tenants/{TENANT}/notes/{RESOURCE}"

    def authorize(self, transaction: Transaction, replay: MutationResult | None) -> AuthorizedScope:
        self.authorizations += 1
        if not self.allowed:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return self.current

    def mutate(self, transaction: Transaction, current: AuthorizedScope) -> MutationResult:
        self.mutations += 1
        transaction.set(self.path, {"version": self.mutations})
        return MutationResult(status_code=201, resource_id=RESOURCE, version=self.mutations)

    def execute(self, **overrides: Any) -> MutationResult:
        args: dict[str, Any] = {
            "operation": "notes.create",
            "key": KEY,
            "body": {"title": "sample", "nested": {"b": 2, "a": 1}},
            "authorize": self.authorize,
            "mutate": self.mutate,
            **overrides,
        }
        return self.idempotency.execute(**args)

    def assert_error(self, code: ErrorCode, work: Callable[[], object]) -> None:
        with self.assertRaises(APIError) as error:
            work()
        self.assertEqual(error.exception.code, code)

    def test_normalized_body_replay_and_conflict(self) -> None:
        first = self.execute()
        replay = self.execute(body={"nested": {"a": 1, "b": 2}, "title": "sample"})
        self.assertEqual(first, replay)
        self.assertEqual(self.mutations, 1)
        self.assertEqual(self.authorizations, 3)
        self.assert_error(
            ErrorCode.IDEMPOTENCY_CONFLICT, lambda: self.execute(body={"title": "new"})
        )
        self.assertEqual(ERRORS[ErrorCode.IDEMPOTENCY_CONFLICT].status, 409)
        self.assertNotEqual(fingerprint([1, 2]), fingerprint([2, 1]))

    def test_key_length_and_24_hour_boundary(self) -> None:
        for key in (None, "x" * 15, "x" * 129):
            self.assert_error(ErrorCode.INVALID_REQUEST, partial(self.execute, key=key))
        for key in ("x" * 16, "x" * 128):
            self.execute(key=key)
        first = self.execute()
        self.clock.advance(24 * 60 * 60 - 1)
        self.assertEqual(self.execute(), first)
        self.clock.advance(1)
        self.assertNotEqual(self.execute(body={"new": True}), first)
        records = [doc for path, doc in self.store.data.items() if "idempotency_records" in path]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(KEY not in path for path in self.store.data))

    def test_tenant_actor_operation_and_key_partition_records(self) -> None:
        self.execute()
        self.current = scope(tenant=OTHER)
        self.execute()
        self.current = scope(actor="bob")
        self.execute()
        self.current = scope()
        self.execute(operation="notes.update:" + str(RESOURCE))
        self.execute(key="another-test-key-001")
        self.assertEqual(self.mutations, 5)

    def test_revocation_and_changed_authorization_never_replay(self) -> None:
        self.execute()
        self.allowed = False
        self.assert_error(ErrorCode.OPERATION_FORBIDDEN, self.execute)
        self.allowed = True
        for current in (
            scope(version=2),
            self.current.model_copy(update={"owner_user_id": FirebaseUid("bob")}),
            self.current.model_copy(
                update={
                    "scope": Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER)
                }
            ),
        ):
            self.current = current
            self.assert_error(ErrorCode.OPERATION_FORBIDDEN, self.execute)
        self.assertEqual(self.mutations, 1)

    def test_retried_transaction_reauthorizes_and_failed_write_rolls_back(self) -> None:
        self.store.retry_once = True
        result = self.execute()
        self.assertEqual(self.authorizations, 2)
        self.assertEqual(result.version, 2)
        self.assertEqual(len(self.store.data), 2)

        def fail(transaction: Transaction, current: AuthorizedScope) -> MutationResult:
            transaction.set(self.path, {"version": 999})
            raise RuntimeError("synthetic failure")

        before = copy.deepcopy(self.store.data)
        with self.assertRaises(RuntimeError):
            self.execute(key="failed-test-key-001", mutate=fail)
        self.assertEqual(before, self.store.data)

    def test_cache_type_does_not_accept_credentials_or_extra_response(self) -> None:
        for field in ("token", "access_token", "body", "headers"):
            with self.assertRaises(ValidationError):
                MutationResult.model_validate(
                    {"status_code": 201, "resource_id": RESOURCE, field: "secret"}
                )
        self.execute()
        record = next(
            value for key, value in self.store.data.items() if "idempotency_records" in key
        )
        self.assertEqual(
            set(record), {"body_hash", "scope_binding", "created_at", "expires_at", "result"}
        )
        self.assertNotIn("sample", str(record))

    def device_scope(
        self, *, session: ResourceId = RESOURCE, owner: str = "alice", idle: int = 60
    ) -> AuthorizedScope:
        return AuthorizedScope(
            context=TenantContext(
                tenant_id=TENANT,
                actor=DeviceActorContext(
                    actor_id=OTHER,
                    allowed_scopes=frozenset({"notes:read"}),
                    authorization_version=1,
                    private_session=PrivateSessionContext(
                        private_session_id=session,
                        user_id=FirebaseUid(owner),
                        expires_at=self.clock.now() + timedelta(minutes=5),
                        idle_expires_at=self.clock.now() + timedelta(seconds=idle),
                    ),
                ),
            ),
            scope=Scope(
                visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.AUTHENTICATED_SESSION
            ),
            owner_user_id=FirebaseUid(owner),
        )

    def test_device_session_identity_is_bound_but_extended_idle_time_is_not(self) -> None:
        self.current = self.device_scope()
        first = self.execute()
        self.clock.advance(20)
        self.current = self.device_scope(idle=60)
        self.assertEqual(self.execute(), first)
        valid = self.current
        for changed in (self.device_scope(session=OTHER), self.device_scope(owner="bob")):
            self.current = changed
            self.assert_error(ErrorCode.OPERATION_FORBIDDEN, self.execute)
        self.current = valid
        self.clock.advance(60)
        self.assert_error(ErrorCode.OPERATION_FORBIDDEN, self.execute)

    def test_strong_etag_contract_and_atomic_compare_write(self) -> None:
        self.assertEqual(etag(3), '"3"')
        self.assertEqual(require_if_match('"3"'), 3)
        self.assert_error(ErrorCode.VERSION_REQUIRED, lambda: require_if_match(None))
        for invalid in ('W/"3"', "*", '"0"', '"03"', '"1","2"', "3", '"1"\n'):
            self.assert_error(ErrorCode.INVALID_REQUEST, partial(require_if_match, invalid))
        self.store.data[self.path] = {"version": 1, "title": "before"}

        def write(header: str | None, replacement: Document | None) -> int:
            return self.store.run(
                lambda tx: compare_and_write(
                    tx,
                    authorized_scope=self.current,
                    path=self.path,
                    if_match=header,
                    replacement=replacement,
                )
            )

        self.assert_error(ErrorCode.VERSION_REQUIRED, lambda: write(None, {}))
        self.assertEqual(write('"1"', {"title": "after", "version": 999}), 2)
        self.assert_error(ErrorCode.VERSION_CONFLICT, lambda: write('"1"', {"title": "lost"}))
        self.assertEqual(self.store.data[self.path], {"title": "after", "version": 2})
        self.assertEqual(write('"2"', None), 3)
        self.assertNotIn(self.path, self.store.data)
        self.assert_error(ErrorCode.RESOURCE_NOT_FOUND, lambda: write('"3"', {}))
        self.current = scope(tenant=OTHER)
        self.assert_error(ErrorCode.OPERATION_FORBIDDEN, lambda: write('"3"', {}))
        self.assertEqual(ERRORS[ErrorCode.VERSION_REQUIRED].status, 428)
        self.assertEqual(ERRORS[ErrorCode.VERSION_CONFLICT].status, 412)

    def test_cursor_bindings_tampering_and_expiry(self) -> None:
        codec = Cursors(b"synthetic-unit-test-signing-key-001", now=self.clock.now)
        query: dict[str, JsonValue] = {
            "operation": "notes.list",
            "limit": 20,
            "order": "created_at,id",
        }
        authorize = partial(self.authorize, MemoryTransaction({}), None)
        token = codec.issue(position=str(RESOURCE), query=query, authorize=authorize)
        self.assertEqual(codec.read(token, query=query, authorize=authorize), RESOURCE)
        for changed in (
            scope(tenant=OTHER),
            scope(actor="bob"),
            scope(version=2),
            scope().model_copy(update={"owner_user_id": FirebaseUid("bob")}),
            scope().model_copy(
                update={
                    "scope": Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER)
                }
            ),
        ):
            self.current = changed
            self.assert_error(
                ErrorCode.INVALID_REQUEST,
                lambda: codec.read(token, query=query, authorize=authorize),
            )
        self.current = scope()
        self.assert_error(
            ErrorCode.INVALID_REQUEST,
            lambda: codec.read(token, query={**query, "limit": 21}, authorize=authorize),
        )
        for invalid in (
            "",
            "x" * 2049,
            token + "x",
            "é." + token.split(".")[1],
            token.split(".")[0] + ".☃",
        ):
            self.assert_error(
                ErrorCode.INVALID_REQUEST,
                partial(codec.read, invalid, query=query, authorize=authorize),
            )
        self.allowed = False
        self.assert_error(
            ErrorCode.OPERATION_FORBIDDEN,
            lambda: codec.read(token, query=query, authorize=authorize),
        )
        self.allowed = True
        self.clock.advance(3600)
        self.assert_error(
            ErrorCode.INVALID_REQUEST, lambda: codec.read(token, query=query, authorize=authorize)
        )
        self.assertEqual(page_limit(), 20)
        for limit in (0, 101, True):
            self.assert_error(ErrorCode.INVALID_REQUEST, partial(page_limit, limit))

    def test_cursor_device_session_change_and_idle_extension(self) -> None:
        codec = Cursors(b"synthetic-unit-test-signing-key-001", now=self.clock.now)
        self.current = self.device_scope()
        authorize = partial(self.authorize, MemoryTransaction({}), None)
        token = codec.issue(position=str(RESOURCE), query={}, authorize=authorize)
        self.clock.advance(20)
        self.current = self.device_scope()
        self.assertEqual(codec.read(token, query={}, authorize=authorize), RESOURCE)
        valid = self.current
        self.current = self.device_scope(session=OTHER)
        self.assert_error(
            ErrorCode.INVALID_REQUEST, lambda: codec.read(token, query={}, authorize=authorize)
        )
        self.current = valid
        self.clock.advance(60)
        self.assert_error(
            ErrorCode.OPERATION_FORBIDDEN, lambda: codec.read(token, query={}, authorize=authorize)
        )


if __name__ == "__main__":
    unittest.main()
