"""Append-only authorization fences. Records are data, never proof of caller authority."""

import hashlib
import json
from typing import Annotated, Literal, Protocol, Self

from brainoid_contracts import FirebaseUid, ResourceId, Version
from pydantic import Field, ValidationError, model_validator

from brainoid_policy.models import Record, Role

Counter = Annotated[int, Field(strict=True, ge=0)]
OperationScope = Literal[
    "shared",
    "conversation",
    "memory.read",
    "memory.write",
    "notes",
    "tasks",
    "reminders",
    "calendar.read",
    "calendar.propose",
    "home.control",
]


class MemberTarget(Record):
    kind: Literal["membership"] = "membership"
    user_id: FirebaseUid
    role: Role
    status: Literal["active", "revoked"]


class DeviceTarget(Record):
    kind: Literal["device"] = "device"
    device_id: ResourceId
    status: Literal["active", "revoked"]
    allowed_scopes: Annotated[tuple[OperationScope, ...], Field(max_length=10)] = ()
    allowed_entity_ids: Annotated[tuple[ResourceId, ...], Field(max_length=100)] = ()
    allowed_room_ids: Annotated[tuple[ResourceId, ...], Field(max_length=100)] = ()


class Change(Record):
    """Allowlist only: no request bodies, credential, arbitrary patch, or actor token.

    expected_version is the DB integer, never the composite authorization stamp.
    A trusted business transaction must authorize this exact proposed change.
    """

    schema_version: Literal[1] = 1
    operation_id: ResourceId
    tenant_id: ResourceId
    expected_version: Counter
    authorization_version: Version
    effect: Literal["deny", "grant"]
    target: Annotated[MemberTarget | DeviceTarget, Field(discriminator="kind")]

    @model_validator(mode="after")
    def consecutive(self) -> Self:
        if self.authorization_version != self.expected_version + 1:
            raise ValueError("Authorization versions must be consecutive")
        if self.effect == "grant" and self.target.status != "active":
            raise ValueError("Grant requires an active target")
        return self

    @property
    def subject_key(self) -> str:
        identity = (
            self.target.user_id if isinstance(self.target, MemberTarget) else self.target.device_id
        )
        return subject_key(self.tenant_id, self.target.kind, identity)


def subject_key(tenant_id: ResourceId, kind: str, identity: str) -> str:
    """Opaque Firebase UIDs are hashed as values, never concatenated into paths."""
    return hashlib.sha256(json.dumps([tenant_id, kind, identity]).encode()).hexdigest()


class Anchor(Record):
    schema_version: Literal[1] = 1
    subject_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    baseline_version: Counter


class Entry(Record):
    change: Change
    state: Literal["pending", "committed"] = "pending"


class LedgerError(Exception):
    def __init__(self) -> None:
        super().__init__("authorization_ledger_unavailable")


class LedgerConflict(LedgerError):
    pass


class Objects(Protocol):
    """Independent durable storage. create must be atomic and immutable.

    False means the name already exists, including a successful ambiguous retry.
    Transport failures raise LedgerError. Reads/listing must be strongly consistent.
    """

    def read(self, name: str) -> bytes | None: ...
    def create(self, name: str, data: bytes) -> bool: ...
    def names(self, prefix: str) -> list[str]: ...


class ControlLedger:
    def __init__(self, objects: Objects) -> None:
        self._objects = objects

    def _read[T: Record](self, name: str, model: type[T]) -> T | None:
        data = self._objects.read(name)
        if data is None:
            return None
        try:
            return model.model_validate_json(data)
        except ValidationError:
            raise LedgerError() from None

    def _create(self, name: str, record: Record) -> None:
        data = record.model_dump_json().encode()
        if not self._objects.create(name, data):
            existing = self._read(name, type(record))
            if existing != record:
                raise LedgerConflict()

    @staticmethod
    def _root(key: str) -> str:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise LedgerError()
        return f"authz/v1/subjects/{key}"

    def latest(self, key: str) -> Entry | None:
        root = self._root(key)
        anchor = self._read(f"{root}/anchor.json", Anchor)
        names = self._objects.names(f"{root}/intents/")
        if anchor is None:
            if names:
                raise LedgerError()
            return None
        if anchor.subject_key != key:
            raise LedgerError()
        changes = []
        for name in names:
            change = self._read(name, Change)
            if change is None or change.subject_key != key:
                raise LedgerError()
            if name != f"{root}/intents/{change.authorization_version}.json":
                raise LedgerError()
            changes.append(change)
        changes.sort(key=lambda item: item.authorization_version)
        expected = anchor.baseline_version
        last = None
        for change in changes:
            if change.expected_version != expected or (last and last.state != "committed"):
                raise LedgerError()
            committed = self._read(f"{root}/commits/{change.authorization_version}.json", Entry)
            if committed is not None and committed != Entry(change=change, state="committed"):
                raise LedgerError()
            last = committed or Entry(change=change)
            expected = change.authorization_version
        return last

    def prepare(self, change: Change) -> Entry:
        # Global immutable operation binding precedes subject allocation. An interrupted
        # reservation can be resumed only with the identical subject and payload.
        self._create(f"authz/v1/operations/{change.operation_id}.json", change)
        root = self._root(change.subject_key)
        anchor = self._read(f"{root}/anchor.json", Anchor)
        if anchor is None:
            self._create(
                f"{root}/anchor.json",
                Anchor(subject_key=change.subject_key, baseline_version=change.expected_version),
            )
        latest = self.latest(change.subject_key)
        if latest is not None and latest.change == change:
            return latest
        if latest is None:
            anchor = self._read(f"{root}/anchor.json", Anchor)
            if anchor is None or anchor.baseline_version != change.expected_version:
                raise LedgerConflict()
        elif (
            latest.state != "committed"
            or latest.change.authorization_version != change.expected_version
        ):
            raise LedgerConflict()
        self._create(f"{root}/intents/{change.authorization_version}.json", change)
        return Entry(change=change)

    def commit(self, change: Change) -> Entry:
        """Repository only: call after confirming durable DB operation/version/payload.

        The public DTO itself is not a database acknowledgement or authorization.
        """
        current = self.latest(change.subject_key)
        if current is None or current.change != change:
            raise LedgerConflict()
        committed = Entry(change=change, state="committed")
        self._create(
            f"{self._root(change.subject_key)}/commits/{change.authorization_version}.json",
            committed,
        )
        return committed
