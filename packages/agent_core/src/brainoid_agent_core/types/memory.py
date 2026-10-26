"""Memory boundary values; authorization and persistence belong to the store adapter."""

from typing import Annotated, Literal, Self

from brainoid_contracts import FirebaseUid, ResourceId, Scope, TenantContext, UtcTimestamp, Version
from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from brainoid_agent_core.types.common import (
    CoreValue,
    JsonObject,
    NonEmptyText,
    NonNegativeInt,
    Stopped,
)


class SourceRef(CoreValue):
    kind: Literal["message", "note", "import"]
    resource_id: ResourceId
    version: Version


class MemoryContent(CoreValue):
    subject_entity_id: ResourceId
    predicate: Literal["located_in", "prefers_setting", "prefers", "related_to", "has_attribute"]
    object_entity_id: ResourceId | None = None
    value: StrictStr | StrictInt | StrictFloat | StrictBool | None = None
    qualifiers: JsonObject = Field(default_factory=dict)
    scope: Scope
    source_refs: tuple[SourceRef, ...]
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp | None = None

    @model_validator(mode="after")
    def validate_object_and_interval(self) -> Self:
        if (self.object_entity_id is None) == (self.value is None):
            raise ValueError("Memory requires exactly one object_entity_id or value")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("Memory valid_to must follow valid_from")
        return self


class MemoryRecord(CoreValue):
    memory_id: ResourceId
    tenant_id: ResourceId
    owner_user_id: FirebaseUid | None = None
    content: MemoryContent
    version: Version
    recorded_at: UtcTimestamp
    status: Literal["candidate", "confirmed", "superseded", "retracted"]
    deletion_epoch: NonNegativeInt = 0
    origin: Literal["user_explicit", "imported", "inferred"] | None = None
    created_by: FirebaseUid | None = None


class MemoryAccess(CoreValue):
    """Server-resolved context and explicit viewing scope; not a proof of permission."""

    context: TenantContext
    scopes: Annotated[tuple[Scope, ...], Field(min_length=1)]


class MemorySearch(MemoryAccess):
    query: NonEmptyText
    limit: Annotated[int, Field(strict=True, ge=1, le=40)] = 10


class MemoryRemember(MemoryAccess):
    content: MemoryContent


class MemoryRevise(MemoryAccess):
    memory_id: ResourceId
    expected_version: Version
    replacement: MemoryContent


class MemoryForget(MemoryAccess):
    memory_id: ResourceId
    expected_version: Version
    source_policy: Literal["suppress_reuse", "delete_source"] = "suppress_reuse"


class MemoryMatches(CoreValue):
    """Real stores return currently authorized records only, with source/version evidence."""

    type: Literal["memory_matches"] = "memory_matches"
    records: tuple[MemoryRecord, ...]


class MemorySaved(CoreValue):
    type: Literal["memory_saved"] = "memory_saved"
    record: MemoryRecord


class MemoryForgetAccepted(CoreValue):
    """Durable deletion acceptance, not proof that all physical deletion has finished."""

    type: Literal["memory_forget_accepted"] = "memory_forget_accepted"
    operation_id: ResourceId


type MemorySearchResult = MemoryMatches | Stopped
type MemoryWriteResult = MemorySaved | Stopped
type MemoryForgetResult = MemoryForgetAccepted | Stopped
