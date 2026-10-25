"""Authorization results are server-side carriers, not proof of authorization."""

import hashlib
import json
from datetime import datetime
from typing import Protocol, Self

from brainoid_contracts import (
    DeviceActorContext,
    FirebaseUid,
    Scope,
    TenantContext,
    UtcTimestamp,
    Visibility,
    normalize_utc,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from brainoid_api.errors import APIError, ErrorCode


def canonical_json(value: JsonValue) -> str:
    """Serialize schema-normalized JSON, preserving array order and scalar types.

    Callers first validate/default/normalize their request model (including dates).
    This is deterministic JSON, not a cross-language RFC 8785 implementation.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def fingerprint(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


class BoundScope(Protocol):
    def ensure_live(self, now: datetime) -> None: ...
    def binding(self) -> str: ...


class AuthorizedUserScope(BaseModel):
    """Current global user authority for bootstrap/listing, never tenant authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: FirebaseUid
    authorization_stamp: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp

    def ensure_live(self, now: datetime) -> None:
        if not self.issued_at <= normalize_utc(now) < self.expires_at:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

    def binding(self) -> str:
        # Token refresh does not change account authority.
        return fingerprint(["user", self.user_id, self.authorization_stamp])


class AuthorizedScope(BaseModel):
    """IAM must revalidate membership, grants, resource ownership and deletion on use.

    Include the effective private owner even on a shared device. The entire actor
    context (including private session identity and authorization version)
    participates in replay/cursor binding. Construction checks shape only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    context: TenantContext
    scope: Scope
    owner_user_id: FirebaseUid | None
    authorization_stamp: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def private_owner_required(self) -> Self:
        if self.scope.visibility == Visibility.PRIVATE and self.owner_user_id is None:
            raise ValueError("Private scope requires its authorized owner")
        return self

    def ensure_live(self, now: datetime) -> None:
        actor = self.context.actor
        if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
            session = actor.private_session
            if normalize_utc(now) >= min(session.expires_at, session.idle_expires_at):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)

    def binding(self) -> str:
        data = self.model_dump(mode="json")
        if self.authorization_stamp is None:
            # Preserve existing FND-08 bindings for callers without the IAM-02 stamp.
            del data["authorization_stamp"]
        # A frozenset's serialization order is not stable across processes.
        data["context"]["actor"]["allowed_scopes"] = sorted(self.context.actor.allowed_scopes)
        session = data["context"]["actor"].get("private_session")
        if session is not None:
            # Activity extends idle expiry without changing identity or grants. Expiry
            # is checked against the current context on every use, not fingerprinted.
            del session["expires_at"]
            del session["idle_expires_at"]
        return fingerprint(data)

    def require_tenant_path(self, path: str) -> None:
        parts = path.split("/")
        if (
            len(parts) < 2
            or len(parts) % 2
            or any(not part or part in (".", "..") for part in parts)
            or parts[:2] != ["tenants", str(self.context.tenant_id)]
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
