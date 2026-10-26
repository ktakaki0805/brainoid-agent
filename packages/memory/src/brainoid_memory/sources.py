"""Trusted source lookup contracts; paths never come from SourceRef or model output."""

from typing import Protocol

from brainoid_contracts import ResourceId, TenantContext, Version
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.transactions import Transaction
from brainoid_policy.models import ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied
from pydantic import StrictBool, ValidationError

from brainoid_memory.models import Epoch, MemoryValue, SourceRef


class ResolvedSource(MemoryValue):
    """Current source metadata and the canonical resource governing its access."""

    source: SourceRef
    authority: ResourceRef
    deletion_epoch: Epoch
    deleted: StrictBool
    suppress_reuse: StrictBool


class SourceResolver(Protocol):
    """Trusted server adapter, never supplied by a request or by the model.

    Resolve only canonical tenant paths, and read current source identity, version,
    deletion epoch and reuse suppression in ``tx``. A nested source's authority must
    be its real parent. Missing, ambiguous, unsupported or malformed sources fail
    closed. Import support must meet this contract before it can be enabled.
    """

    def resolve(
        self, tx: Transaction, context: TenantContext, source: SourceRef
    ) -> ResolvedSource: ...


class MessageLocator(Protocol):
    """Resolve a message UUID to its one parent using trusted transaction reads.

    No arbitrary path is returned. A missing/ambiguous mapping raises PolicyDenied.
    MEM-03 supplies this adapter from the server's persisted source information.
    """

    def conversation_id(
        self, tx: Transaction, tenant_id: ResourceId, message_id: ResourceId
    ) -> ResourceId: ...


class _SourceMetadata(MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    deletion_epoch: Epoch
    deleted: StrictBool
    suppress_reuse: StrictBool


class CanonicalSources:
    """Notes and optionally nested messages; imports are deliberately unsupported."""

    def __init__(self, messages: MessageLocator | None = None) -> None:
        self._messages = messages

    def resolve(self, tx: Transaction, context: TenantContext, source: SourceRef) -> ResolvedSource:
        if source.kind == "note":
            authority = ResourceRef(
                tenant_id=context.tenant_id, kind=ResourceKind.NOTE, id=source.resource_id
            )
            path = f"tenants/{context.tenant_id}/notes/{source.resource_id}"
        elif source.kind == "message" and self._messages is not None:
            # ResourceRef validates UUIDs again before any path is composed.
            authority = ResourceRef(
                tenant_id=context.tenant_id,
                kind=ResourceKind.CONVERSATION,
                id=self._messages.conversation_id(tx, context.tenant_id, source.resource_id),
            )
            path = (
                f"tenants/{context.tenant_id}/conversations/{authority.id}"
                f"/messages/{source.resource_id}"
            )
        else:
            raise PolicyDenied()
        document = tx.get(path)
        if document is None or (
            source.kind == "message" and document.get("conversation_id") != authority.id
        ):
            raise PolicyDenied()
        document = fenced_document(
            tx,
            context.tenant_id,
            "messages" if source.kind == "message" else "notes",
            source.resource_id,
            document,
            parent=authority.id if source.kind == "message" else None,
        )
        if source.kind == "message" and (
            # Assistant/tool text can derive from earlier history even with empty
            # local dependency arrays. Until recursive proof is supported, accept
            # only direct user messages through every canonical locator adapter.
            document.get("role") != "user"
            or document.get("source_refs", []) != []
            or any(
                key in document
                for key in ("memory_records", "tool_bindings", "model_message", "model_messages")
            )
        ):
            raise PolicyDenied()
        try:
            metadata = _SourceMetadata.model_validate(
                {key: document.get(key) for key in _SourceMetadata.model_fields}
            )
        except ValidationError:
            raise PolicyDenied() from None
        if metadata.id != source.resource_id or metadata.tenant_id != context.tenant_id:
            raise PolicyDenied()
        return ResolvedSource(
            source=SourceRef(kind=source.kind, resource_id=metadata.id, version=metadata.version),
            authority=authority,
            deletion_epoch=metadata.deletion_epoch,
            deleted=metadata.deleted,
            # Fact SourceRef has no span identity. Any span suppression (including
            # malformed markers) makes model reuse unprovable; ordinary UI READ
            # still follows its separate policy. Never erase provenance to allow it.
            suppress_reuse=metadata.suppress_reuse or document.get("suppressed_spans", []) != [],
        )
