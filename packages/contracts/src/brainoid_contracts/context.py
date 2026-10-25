"""Immutable carriers created by IAM after authorization, never from request/tool input.

Construction validates shape only. It does not prove membership, active credentials,
scope grants, authorization versions or session expiry; IAM must check these on use.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from brainoid_contracts.identifiers import FirebaseUid, ProviderId, ResourceId
from brainoid_contracts.scope import Version
from brainoid_contracts.time import UtcTimestamp

PermissionScope = Annotated[str, Field(strict=True, min_length=1, max_length=80)]


class _Context(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PrivateSessionContext(_Context):
    """IAM-resolved user on the enclosing device; expiry is not checked by this model."""

    private_session_id: ResourceId
    user_id: FirebaseUid
    expires_at: UtcTimestamp
    idle_expires_at: UtcTimestamp


class _Actor(_Context):
    allowed_scopes: frozenset[PermissionScope]
    authorization_version: Version


class UserActorContext(_Actor):
    actor_type: Literal["user"] = "user"
    actor_id: FirebaseUid
    membership_status: Literal["active"] = "active"


class DeviceActorContext(_Actor):
    actor_type: Literal["device"] = "device"
    actor_id: ResourceId
    device_status: Literal["active"] = "active"
    private_session: PrivateSessionContext | None = None


class WorkerActorContext(_Actor):
    actor_type: Literal["worker"] = "worker"
    actor_id: ProviderId


ActorContext = Annotated[
    UserActorContext | DeviceActorContext | WorkerActorContext, Field(discriminator="actor_type")
]


class TenantContext(_Context):
    """One server-selected tenant and its authorized actor, bound in one value."""

    tenant_id: ResourceId
    actor: ActorContext
