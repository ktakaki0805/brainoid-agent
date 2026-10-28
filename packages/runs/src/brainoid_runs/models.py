"""Public views are separate from server-owned history and execution receipts."""

from typing import Annotated, Literal, Self

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_policy.devices import CredentialBinding as CredentialBinding
from brainoid_policy.models import Record
from pydantic import Field, model_validator

Title = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Text = Annotated[str, Field(strict=True, max_length=8000)]


class ConversationCreate(Record):
    agent_id: ResourceId
    title: Title = "Conversation"
    visibility: Visibility
    display_policy: DisplayPolicy

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        Scope(visibility=self.visibility, display_policy=self.display_policy)
        return self


class Conversation(ConversationCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda value: value is None)


class ConversationPatch(Record):
    title: Title | None = None
    display_policy: DisplayPolicy | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, f) is None for f in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null setting")
        return self


class ConversationPage(Record):
    items: Annotated[list[Conversation], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class SourceRef(Record):
    kind: Literal["message", "note", "import"]
    resource_id: ResourceId
    version: Version


class Message(Record):
    id: ResourceId
    role: Literal["user", "assistant", "tool"]
    text: Text = Field(repr=False)
    run_id: ResourceId
    created_at: UtcTimestamp
    source_refs: Annotated[list[SourceRef], Field(max_length=20)] = Field(default_factory=list)


class MessagePage(Record):
    items: Annotated[list[Message], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class TurnCreate(Record):
    # Audio and speech are deliberately not admitted before their implementation.
    text: Annotated[str, Field(strict=True, min_length=1, max_length=8000, repr=False)]
    output_mode: Literal["text"]
    output_device_id: ResourceId | None = None
    # A user-to-Pi output precondition, never a grant of session authority.
    expected_output_private_session_id: ResourceId | None = None

    @model_validator(mode="after")
    def nonnull_destination(self) -> Self:
        if "output_device_id" in self.model_fields_set and self.output_device_id is None:
            raise ValueError("Destination must be omitted or a resource ID")
        if "expected_output_private_session_id" in self.model_fields_set and (
            self.expected_output_private_session_id is None or self.output_device_id is None
        ):
            raise ValueError("Session precondition requires an explicit destination")
        return self


class RunAccepted(Record):
    run_id: ResourceId
    status: Literal["queued"] = "queued"
    events_path: str
    status_path: str


class Run(Record):
    run_id: ResourceId
    conversation_id: ResourceId
    status: Literal["queued", "running", "waiting_confirmation", "completed", "failed", "cancelled"]
    version: Version
    last_event_seq: Annotated[int, Field(strict=True, ge=0)] = 0
    created_at: UtcTimestamp
    final_message_id: ResourceId | None = None
    error_code: str | None = None
    waiting_proposal_id: ResourceId | None = None
    checkpoint_version: Version | None = None
