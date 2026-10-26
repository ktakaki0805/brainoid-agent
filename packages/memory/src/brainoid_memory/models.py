"""Canonical graph records. These are storage values, not HTTP request/response DTOs."""

from typing import Annotated, Literal, Self

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version, Visibility
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

Epoch = Annotated[int, Field(strict=True, ge=0)]
Label = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Predicate = Literal["located_in", "prefers_setting", "prefers", "related_to", "has_attribute"]
FactStatus = Literal["candidate", "confirmed", "superseded", "retracted"]
Value = Annotated[str, Field(strict=True, max_length=4000)] | StrictInt | StrictFloat | StrictBool


class MemoryValue(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class SourceRef(MemoryValue):
    kind: Literal["message", "note", "import"]
    resource_id: ResourceId
    version: Version


class ExternalRef(MemoryValue):
    provider: Literal["home_assistant"]
    integration_id: ResourceId
    external_id: Annotated[str, Field(strict=True, min_length=1, max_length=255)]


class Qualifiers(MemoryValue):
    context: Annotated[str, Field(strict=True, max_length=200)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    brightness_pct: Annotated[int, Field(strict=True, ge=0, le=100)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    temperature_c: Annotated[float, Field(strict=True, ge=16, le=30)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @field_validator("context", "brightness_pct", "temperature_c", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied qualifier cannot be null")
        return value


class ScopedValue(Scope):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class EntityInput(ScopedValue):
    kind: Literal["person", "room", "appliance", "activity", "topic"]
    label: Label
    aliases: Annotated[tuple[Label, ...], Field(max_length=10)] = ()
    external_refs: Annotated[tuple[ExternalRef, ...], Field(max_length=10)] = ()


class EntityPatch(MemoryValue):
    label: Label | None = None
    aliases: Annotated[tuple[Label, ...], Field(max_length=10)] | None = None

    @field_validator("label", "aliases", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied entity patch field cannot be null")
        return value

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if self.label is None and self.aliases is None:
            raise ValueError("An entity patch must change at least one field")
        return self


class ClaimInput(ScopedValue):
    subject_entity_id: ResourceId
    predicate: Predicate
    object_entity_id: ResourceId | None = None
    value: Value | None = None
    qualifiers: Qualifiers = Field(default_factory=Qualifiers)

    @field_validator("object_entity_id", "value", mode="before")
    @classmethod
    def reject_null_object(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied object or value cannot be null")
        return value

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        if (self.object_entity_id is None) == (self.value is None):
            raise ValueError("A fact requires exactly one object_entity_id or value")
        if self.predicate != "prefers_setting" and (
            self.qualifiers.brightness_pct is not None or self.qualifiers.temperature_c is not None
        ):
            raise ValueError("Setting qualifiers require prefers_setting")
        return self


class FactInput(ClaimInput):
    source_refs: Annotated[tuple[SourceRef, ...], Field(max_length=20)]
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp | None = None
    origin: Literal["user_explicit", "imported", "inferred"] = "user_explicit"
    status: FactStatus = "confirmed"

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must follow valid_from")
        if self.origin == "inferred" and self.status == "confirmed":
            raise ValueError("An inference cannot be a confirmed fact")
        if not self.source_refs and (self.origin != "user_explicit" or self.status == "candidate"):
            raise ValueError("Only explicit confirmed input and its history may lack sources")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate source references")
        return self


class StoredMetadata(MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    owner_user_id: FirebaseUid | None
    created_by: FirebaseUid
    created_via_device_id: ResourceId | None = None
    version: Version
    deletion_epoch: Epoch
    schema_version: Literal[1]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    deleted: StrictBool
    suppress_reuse: StrictBool

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value


class Ownership(MemoryValue):
    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        # Shared base used only by the two complete stored record models below.
        if isinstance(self, (Entity, Fact)):
            if self.visibility == Visibility.PRIVATE and self.owner_user_id is None:
                raise ValueError("Private resources require an owner")
            if self.updated_at < self.created_at:
                raise ValueError("updated_at cannot precede created_at")
        return self


class Entity(EntityInput, StoredMetadata, Ownership):
    pass


class Fact(FactInput, StoredMetadata, Ownership):
    recorded_at: UtcTimestamp
    supersedes_fact_id: ResourceId | None = None
