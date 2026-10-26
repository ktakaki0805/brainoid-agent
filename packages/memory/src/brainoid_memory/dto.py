"""Public explicit-memory inputs and views, shared by HTTP and typed tools."""

from datetime import datetime
from typing import Annotated, Self

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp, Version
from pydantic import Field, field_validator, model_validator

from brainoid_memory.models import ClaimInput, FactInput, MemoryValue, SourceRef, Value


class MemoryCreate(ClaimInput):
    source_refs: Annotated[tuple[SourceRef, ...], Field(max_length=20)] = ()
    valid_from: UtcTimestamp | None = None
    valid_to: UtcTimestamp | None = None

    @field_validator("valid_from", mode="before")
    @classmethod
    def nonnull_start(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied valid_from cannot be null")
        return value

    @model_validator(mode="after")
    def interval_and_sources(self) -> Self:
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_to <= self.valid_from:
                raise ValueError("valid_to must follow valid_from")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate sources")
        return self

    def fact(self, now: datetime) -> FactInput:
        return FactInput.model_validate(
            {
                **self.model_dump(exclude_none=True),
                "valid_from": self.valid_from or now,
                "origin": "user_explicit",
                "status": "confirmed",
            }
        )


class MemoryRevision(MemoryValue):
    replacement: MemoryCreate
    reason: Annotated[str, Field(strict=True, max_length=500)] | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def nonnull_reason(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied reason cannot be null")
        return value


class MemoryView(FactInput):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    recorded_at: UtcTimestamp
    created_by: FirebaseUid
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)]
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)
    supersedes_fact_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    object_entity_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    value: Value | None = Field(default=None, exclude_if=lambda v: v is None)
    valid_to: UtcTimestamp | None = Field(default=None, exclude_if=lambda v: v is None)


class MemoryPage(MemoryValue):
    items: Annotated[list[MemoryView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None
