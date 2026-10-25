"""Validated policy inputs. Shape validation never proves identity or current authority."""

from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version, Visibility
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class UserSubject(Record):
    """UID supplied by authentication or a trusted persisted job, never model input."""

    kind: Literal["user"] = "user"
    user_id: FirebaseUid


class DeviceSubject(Record):
    """Verified device credential identity; a selected session ID is not authentication."""

    kind: Literal["device"] = "device"
    device_id: ResourceId
    connection_generation: Version
    private_session_id: ResourceId | None = None


type Subject = UserSubject | DeviceSubject
Status = Literal["active", "revoked", "suspended", "deleting", "deleted", "ended", "expired"]
Epoch = Annotated[int, Field(strict=True, ge=0)]


class TenantState(Record):
    tenant_id: ResourceId
    status: Status
    policy_version: Version
    deletion_epoch: Epoch


class Membership(Record):
    tenant_id: ResourceId
    user_id: FirebaseUid
    role: Role
    status: Status
    authorization_version: Version


class DeviceState(Record):
    tenant_id: ResourceId
    id: ResourceId
    status: Status
    authorization_version: Version
    connection_generation: Version
    allowed_scopes: frozenset[str]
    allowed_entity_ids: frozenset[ResourceId] = frozenset()
    allowed_room_ids: frozenset[ResourceId] = frozenset()
    capabilities: frozenset[str]


class SessionState(Record):
    tenant_id: ResourceId
    id: ResourceId
    device_id: ResourceId
    user_id: FirebaseUid
    status: Status
    authorization_version: Version
    connection_generation: Version
    membership_authorization_version: Version
    device_authorization_version: Version
    user_authorization_stamp: str
    allowed_scopes: frozenset[str]
    expires_at: UtcTimestamp
    idle_expires_at: UtcTimestamp


class ResourceKind(StrEnum):
    CONVERSATION = "conversations"
    ENTITY = "entities"
    MEMORY = "facts"
    NOTE = "notes"
    TASK = "tasks"
    REMINDER = "reminders"


class ResourceRef(Record):
    tenant_id: ResourceId
    kind: ResourceKind
    id: ResourceId


class ResourceState(Record):
    """Metadata read from the canonical resource, not client-provided PATCH values."""

    ref: ResourceRef
    scope: Scope
    owner_user_id: FirebaseUid | None
    created_by: str
    version: Version
    deletion_epoch: Epoch
    deleted: StrictBool = False
    suppress_reuse: StrictBool = False
    room_id: ResourceId | None = None
    entity_id: ResourceId | None = None

    @model_validator(mode="after")
    def private_owner(self) -> Self:
        if self.scope.visibility == Visibility.PRIVATE and self.owner_user_id is None:
            raise ValueError("Private resource requires an owner")
        return self


class StateReader(Protocol):
    """Trusted, uncached canonical reads within ONE current DB transaction.

    Implementations must include concurrent authorization writes in the read set.
    Neither caller-supplied contexts nor graph/model text may implement this port.
    IAM-05 will also fence pending deny/committed grants from the control ledger.
    """

    def tenant(self, tenant_id: ResourceId) -> TenantState | None: ...
    def membership(self, tenant_id: ResourceId, user_id: FirebaseUid) -> Membership | None: ...
    def device(self, tenant_id: ResourceId, device_id: ResourceId) -> DeviceState | None: ...
    def session(self, tenant_id: ResourceId, session_id: ResourceId) -> SessionState | None: ...
    def resource(self, ref: ResourceRef) -> ResourceState | None: ...


class Action(StrEnum):
    READ = "read"
    WRITE = "write"
    POST = "post"  # Append a conversation message, never edit existing content.
    MODEL_READ = "model_read"


class ManagementAction(StrEnum):
    UPDATE_TENANT = "update_tenant"
    INVITE = "invite"
    CHANGE_ROLE = "change_role"
    REMOVE_MEMBER = "remove_member"
    TRANSFER_OWNER = "transfer_owner"
    MANAGE_DEVICES = "manage_devices"
    DELETE_TENANT = "delete_tenant"
    DELETE_ACCOUNT = "delete_account"
