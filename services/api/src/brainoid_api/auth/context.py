"""Verified identity and untrusted tenant selection, never authorization grants."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp
from pydantic import BaseModel, ConfigDict

from brainoid_api.errors import APIError, ErrorCode


class AuthenticatedIdentity(BaseModel):
    """Created from verified Firebase claims. Contains no membership, role or scopes."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    actor_type: Literal["user"] = "user"
    user_id: FirebaseUid
    authenticated_at: UtcTimestamp
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp


class SelectedTenantIdentity(BaseModel):
    """Input to the future membership authorizer, NOT a trusted TenantContext."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    identity: AuthenticatedIdentity
    selected_tenant_id: ResourceId


def select_tenant(identity: AuthenticatedIdentity, tenant_id: ResourceId) -> SelectedTenantIdentity:
    """Keep the request's selection separate from the verified user identity."""
    return SelectedTenantIdentity(identity=identity, selected_tenant_id=tenant_id)


def require_recent_authentication(
    identity: AuthenticatedIdentity, *, now: datetime | None = None
) -> None:
    """Destructive actions require authentication within five minutes, not token refresh."""
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("An aware current time is required")
    age = current - identity.authenticated_at
    if age < timedelta(0) or current >= identity.expires_at:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    if age > timedelta(minutes=5):
        raise APIError(ErrorCode.REAUTH_REQUIRED)
