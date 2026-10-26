"""Canonical note models and the shared direct-resource transaction service."""

from typing import Annotated, Self

from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Scope, UtcTimestamp, Version
from brainoid_policy.models import ResourceKind
from pydantic import Field, model_validator

from brainoid_memory.direct_resources import DirectRecord, DirectResources
from brainoid_memory.models import MemoryValue

Title = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Body = Annotated[str, Field(strict=True, max_length=8000)]


class NoteCreate(Scope):
    title: Title
    body: Body


class NotePatch(MemoryValue):
    title: Title | None = None
    body: Body | None = None
    display_policy: DisplayPolicy | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Nonempty changes without null required")
        return self


class Note(NoteCreate, DirectRecord):
    source_refs: tuple[()] = ()


class NoteView(NoteCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda value: value is None)


class NotePage(MemoryValue):
    items: Annotated[list[NoteView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None = None


def note_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/notes/{identity}"


def effect_path(tenant: ResourceId, effect: ResourceId) -> str:
    return f"tenants/{tenant}/note_effects/{effect}"


def view(record: Note) -> NoteView:
    return NoteView.model_validate({key: getattr(record, key) for key in NoteView.model_fields})


class Notes(DirectResources[NoteCreate, NotePatch, Note]):
    kind = ResourceKind.NOTE
    create_type = NoteCreate
    patch_type = NotePatch
    record_type = Note

    @staticmethod
    def text(record: Note) -> str:
        return record.title + "\n" + record.body
