"""Bounded, content-free manifests and the private temporary-object contract."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Protocol

from brainoid_contracts import ResourceId, UtcTimestamp, Version
from brainoid_contracts.transactions import Transaction
from pydantic import Field

from brainoid_memory.models import MemoryValue
from brainoid_memory.repository import VerifiedCaller

MAX_ITEMS = 200
MAX_BYTES = 4 * 1024 * 1024
MAX_SCAN = 5000
KIND = "export_data"


class ExportCreate(MemoryValue):
    kind: Literal["export"]
    scope: Literal["my_data", "visible_tenant_data"]


class ManifestEntry(MemoryValue):
    kind: Literal[
        "conversations",
        "messages",
        "entities",
        "facts",
        "notes",
        "tasks",
        "reminders",
        "notifications",
    ]
    resource_id: ResourceId
    parent_id: ResourceId | None = None
    version: Version
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)]
    # Binds public projection AND authorization/dependency metadata, never content.
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    def path(self, tenant: ResourceId) -> str:
        if (self.kind == "messages") != (self.parent_id is not None):
            raise ValueError("Invalid export manifest parent")
        prefix = f"tenants/{tenant}"
        if self.parent_id is not None:
            prefix += f"/conversations/{self.parent_id}"
        return f"{prefix}/{self.kind}/{self.resource_id}"


class Artifact(MemoryValue):
    expires_at: UtcTimestamp
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: Annotated[int, Field(strict=True, ge=1, le=MAX_BYTES)]


class ObjectUnavailable(Exception):
    """No provider error strings or object locations cross the public boundary."""


class ObjectRevoked(Exception):
    pass


class ExportObjects(Protocol):
    """Separate encrypted private storage; NEVER the immutable deletion/control ledger.

    Key is an opaque operation UUID. put is immutable/idempotent. revoke durably
    tombstones the key before erasing its bytes, and prevents every concurrent or
    delayed put from resurrecting it, including after process restart. get enforces
    the object's OWN deadline and authenticates ciphertext/metadata. No public URLs.
    """

    def put(self, key: ResourceId, content: bytes, artifact: Artifact) -> None: ...
    def get(self, key: ResourceId) -> tuple[bytes, Artifact]: ...
    def revoke(self, key: ResourceId) -> None: ...


class ExportPages(Protocol):
    def children(
        self, parent: str, *, collections: bool, token: str = "", limit: int = 50
    ) -> tuple[list[str], str]: ...


type Authorize = Callable[[Transaction, ResourceId], VerifiedCaller]
type Clock = Callable[[], datetime]
