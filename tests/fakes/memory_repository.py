"""Synthetic T-009 graph, current authenticated identities and atomic transaction fake."""

import copy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from brainoid_contracts import (
    DeviceActorContext,
    FirebaseUid,
    PrivateSessionContext,
    ResourceId,
    TenantContext,
    UserActorContext,
)
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory import (
    CanonicalSources,
    EntityInput,
    FactInput,
    MemoryRepository,
    VerifiedCaller,
)
from brainoid_policy.accounts import account_stamp
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceSubject, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied

from fakes.authz_ledger import MemoryObjects

TENANT = ResourceId("00000000-0000-0000-0000-000000000001")
OTHER = ResourceId("00000000-0000-0000-0000-000000000002")
LIGHT = ResourceId("00000000-0000-0000-0000-000000000003")
BEDROOM_LIGHT = ResourceId("00000000-0000-0000-0000-000000000004")
PERSON = ResourceId("00000000-0000-0000-0000-000000000005")
OTHER_PERSON = ResourceId("00000000-0000-0000-0000-000000000006")
SOURCE = ResourceId("00000000-0000-0000-0000-000000000007")
FACT = ResourceId("00000000-0000-0000-0000-000000000008")
CONVERSATION = ResourceId("00000000-0000-0000-0000-000000000009")
LIVING_ROOM = ResourceId("00000000-0000-0000-0000-000000000010")
BEDROOM = ResourceId("00000000-0000-0000-0000-000000000011")
LIVING_LOCATION = ResourceId("00000000-0000-0000-0000-000000000012")
BEDROOM_LOCATION = ResourceId("00000000-0000-0000-0000-000000000013")
USER = FirebaseUid("fixture/user/opaque")
NOW = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


class GraphTransaction:
    def __init__(
        self, data: dict[str, Document], after_get: Callable[[str], None] = lambda path: None
    ) -> None:
        self.data = copy.deepcopy(data)
        self.written = False
        self.after_get = after_get

    def _read(self) -> None:
        if self.written:
            raise AssertionError("Firestore cannot read after a write")

    def get(self, path: str) -> Document | None:
        self._read()
        result = copy.deepcopy(self.data.get(path))
        self.after_get(path)
        return result

    def collection(self, path: str) -> list[tuple[str, Document]]:
        self._read()
        return [(p, copy.deepcopy(d)) for p, d in self.data.items() if p.rsplit("/", 1)[0] == path]

    @staticmethod
    def _field(document: Document, name: str) -> Any:
        value: Any = document
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def query(self, path: str, filters: dict[str, str]) -> list[tuple[str, Document]]:
        return [
            (p, d)
            for p, d in self.collection(path)
            if all(self._field(d, k) == v for k, v in filters.items())
        ]

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        return [d for _, d in self.collection(collection) if d.get(field) == value][:2]

    def find_array(self, collection: str, field: str, value: Document) -> list[Document]:
        self._read()
        result = []
        for path, document in self.data.items():
            if path.rsplit("/", 1)[0] == collection and value in document.get(field, []):
                result.append(copy.deepcopy(document))
                if len(result) == 2:
                    break
        return result

    def query_page(
        self, path: str, filters: dict[str, str], *, after: str | None, limit: int
    ) -> list[tuple[str, Document]]:
        self._read()
        result = []
        for key in sorted(self.data):
            if key.rsplit("/", 1)[0] != path or (after and key.rsplit("/", 1)[1] <= after):
                continue
            document = self.data[key]
            if all(self._field(document, k) == v for k, v in filters.items()):
                result.append((key, copy.deepcopy(document)))
                if len(result) == limit:
                    break
        return result

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        self._read()
        return [
            (p, copy.deepcopy(d))
            for p, d in self.data.items()
            if p.split("/")[-2] == "memberships" and d.get("user_id") == user_id
        ]

    def set(self, path: str, document: Document) -> None:
        self.written = True
        self.data[path] = copy.deepcopy(document)

    def create(self, path: str, document: Document) -> None:
        if path in self.data:
            raise AssertionError("Duplicate create")
        self.set(path, document)

    def delete(self, path: str) -> None:
        self.written = True
        del self.data[path]


class GraphStore:
    def __init__(self) -> None:
        self.data: dict[str, Document] = {}
        self.retry: Callable[[], None] | None = None
        self.fail_commit = False
        self.calls = 0
        self.after_get: Callable[[str], None] = lambda path: None
        self.after_commit: Callable[[], None] = lambda: None

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        self.calls += 1
        if self.retry is not None:
            hook, self.retry = self.retry, None
            work(GraphTransaction(self.data, self.after_get))
            hook()
        tx = GraphTransaction(self.data, self.after_get)
        result = work(tx)
        if self.fail_commit:
            raise RuntimeError("Synthetic uncommitted failure")
        self.data = tx.data
        self.after_commit()
        return result


class AuthenticatedCaller:
    """Fixture identity is independent of the caller-provided context."""

    def __init__(self, subject: Subject | None = None) -> None:
        self.subject = subject or UserSubject(user_id=USER)
        self.active = True
        self.valid_from = NOW
        self.valid_to = NOW + timedelta(hours=1)

    def authorize(self, tx: Transaction, context: TenantContext) -> VerifiedCaller:
        if not self.active:
            raise PolicyDenied()
        return VerifiedCaller(
            subject=self.subject, valid_from=self.valid_from, valid_to=self.valid_to
        )


def context(tenant: ResourceId = TENANT, user: FirebaseUid = USER) -> TenantContext:
    return TenantContext(
        tenant_id=tenant,
        actor=UserActorContext(actor_id=user, allowed_scopes=frozenset(), authorization_version=1),
    )


def device_session(
    store: GraphStore, *, user: FirebaseUid = USER
) -> tuple[DeviceSubject, TenantContext]:
    """A member's real canonical Pi session, with a 60-second idle deadline."""
    device, session = CONVERSATION, BEDROOM
    scopes = {"shared", "memory.read", "memory.write", "notes"}
    store.data[f"tenants/{TENANT}/devices/{device}"] = {
        "id": device,
        "tenant_id": TENANT,
        "status": "active",
        "authorization_version": 1,
        "connection_generation": 1,
        "allowed_scopes": sorted(scopes),
        "allowed_entity_ids": [LIGHT, PERSON, BEDROOM_LIGHT],
        "allowed_room_ids": [],
        "capabilities": ["display"],
    }
    expiry = NOW + timedelta(minutes=5)
    idle = NOW + timedelta(seconds=60)
    store.data[f"tenants/{TENANT}/private_sessions/{session}"] = {
        "id": session,
        "tenant_id": TENANT,
        "device_id": device,
        "user_id": user,
        "status": "active",
        "authorization_version": 1,
        "connection_generation": 1,
        "membership_authorization_version": 1,
        "device_authorization_version": 1,
        "user_authorization_stamp": account_stamp(GraphTransaction(store.data), user),
        "allowed_scopes": sorted(scopes - {"shared"}),
        "expires_at": expiry,
        "idle_expires_at": idle,
    }
    store.data[f"tenants/{TENANT}/private_session_slots/{device}"] = {"session_id": session}
    return DeviceSubject(
        device_id=device, connection_generation=1, private_session_id=session
    ), TenantContext(
        tenant_id=TENANT,
        actor=DeviceActorContext(
            actor_id=device,
            allowed_scopes=frozenset(scopes),
            authorization_version=1,
            private_session=PrivateSessionContext(
                private_session_id=session, user_id=user, expires_at=expiry, idle_expires_at=idle
            ),
        ),
    )


def entity_values(**changes: object) -> EntityInput:
    return EntityInput.model_validate(
        {
            "kind": "appliance",
            "label": "照明",
            "visibility": "shared",
            "display_policy": "shared_display",
            **changes,
        }
    )


def fact_values(**changes: object) -> FactInput:
    return FactInput.model_validate(
        {
            "subject_entity_id": PERSON,
            "predicate": "prefers_setting",
            "object_entity_id": LIGHT,
            "qualifiers": {"context": "bedtime", "brightness_pct": 20},
            "source_refs": [{"kind": "note", "resource_id": SOURCE, "version": 1}],
            "valid_from": NOW,
            "visibility": "shared",
            "display_policy": "shared_display",
            **changes,
        }
    )


def authority_data() -> dict[str, Document]:
    data: dict[str, Document] = {}
    for tenant in (TENANT, OTHER):
        data[f"tenants/{tenant}"] = {
            "tenant_id": tenant,
            "status": "active",
            "policy_version": 1,
            "deletion_epoch": 0,
        }
        data[f"tenants/{tenant}/memberships/safe-key"] = {
            "tenant_id": tenant,
            "user_id": USER,
            "status": "active",
            "role": "owner",
            "authorization_version": 1,
        }
        data[f"tenants/{tenant}/notes/{SOURCE}"] = {
            "id": SOURCE,
            "tenant_id": tenant,
            "visibility": "shared",
            "display_policy": "shared_display",
            "owner_user_id": USER,
            "created_by": USER,
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
            "suppress_reuse": False,
            "body": "FIXTURE_SYNTHETIC_SOURCE",
        }
    return data


def repository(store: GraphStore, caller: AuthenticatedCaller | None = None) -> MemoryRepository:
    return MemoryRepository(
        store,
        authorizer=caller or AuthenticatedCaller(),
        sources=CanonicalSources(),
        ledger=ControlLedger(MemoryObjects()),
        now=lambda: NOW,
        allow_untracked_bootstrap=True,
    )
