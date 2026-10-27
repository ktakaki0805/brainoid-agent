"""Calendar authorization contracts. Secrets never belong to canonical documents."""

import base64
import hashlib
from datetime import datetime
from typing import Literal, Protocol

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp, Version
from brainoid_contracts.transactions import Document
from pydantic import SecretStr

from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.models import MemoryValue

READ = "https://www.googleapis.com/auth/calendar.events.readonly"
WRITE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = {"read": (READ,), "read_write": (READ, WRITE)}


class OAuthStart(MemoryValue):
    scope_profile: Literal["read", "read_write"]
    return_target: Literal["mobile", "web"]


class OAuthStartResponse(MemoryValue):
    authorization_url: str
    expires_at: UtcTimestamp


class Grant(MemoryValue):
    access_token: SecretStr
    refresh_token: SecretStr
    scopes: frozenset[str]


class IntegrationView(MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    provider: Literal["google_calendar"] = "google_calendar"
    status: Literal["active", "reauth_required", "revoked"]
    owner_user_id: FirebaseUid
    allowed_calendar_ids: list[str] = []


class IntegrationPage(MemoryValue):
    items: list[IntegrationView]
    next_cursor: str | None


def integration_view(record: Document) -> IntegrationView:
    return IntegrationView.model_validate(
        {key: value for key, value in record.items() if key in IntegrationView.model_fields}
    )


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class OAuthProvider(Protocol):
    def authorization_url(self, *, state: str, verifier: str, scopes: tuple[str, ...]) -> str: ...
    def exchange(self, *, code: str, verifier: str) -> Grant: ...


class SecretUnavailable(Exception):
    """Fixed error without provider messages, request payloads or tracebacks."""


class SecretStore(Protocol):
    """Immutable numeric-version slots with durable per-slot AND lifecycle fences.

    put returns the first stored value's exact reference, even after response loss.
    Ownership/binding mismatch fails. revoke and invalidate must serialize against
    ALL puts, including a key absent at deletion time, across process restarts.
    A Google Secret Manager implementation needs an independent fencing registry;
    eventual parent deletion or an alias such as 'latest' does not satisfy this port.
    """

    def put(
        self,
        slot: ResourceId,
        *,
        tenant: ResourceId,
        user: FirebaseUid,
        binding: str,
        content: bytes,
        expires_at: datetime | None,
        generation: int = 1,
    ) -> str: ...
    def get(self, reference: str) -> bytes: ...
    def revoke(self, slot: ResourceId) -> None: ...
    def invalidate(self, intent: DeletionIntent, *, key: str) -> str: ...
