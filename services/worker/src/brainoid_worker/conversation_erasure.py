"""Bounded-walker provenance for conversation erasure, never a read grant."""

from collections.abc import Callable

from brainoid_contracts import ResourceId, Version
from brainoid_contracts.deletion import tombstone_path
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionTarget
from brainoid_memory.forget import operation_path
from brainoid_memory.models import MemoryValue
from pydantic import TypeAdapter, ValidationError


class SourceProvenanceMissing(Exception):
    """A copy cannot be classified safely; continue known cleanup without completion."""


class MessageOrigin(MemoryValue):
    resource_id: ResourceId
    parent_id: ResourceId
    version: Version


class ConversationSources:
    def __init__(self, accepted: Callable[[Transaction, str], DeletionIntent | None]) -> None:
        self.accepted = accepted

    @staticmethod
    def enabled(intent: DeletionIntent) -> bool:
        return (
            intent.kind == "delete_resource"
            and intent.targets[0].kind == "conversation"
            and intent.include_derived_memories
        )

    @staticmethod
    def path(intent: DeletionIntent, identity: str) -> str:
        return operation_path(intent.tenant_id, intent.operation_id) + "/purge_messages/" + identity

    @staticmethod
    def proof(intent: DeletionIntent, target: MessageOrigin) -> Document:
        return {
            "tenant_id": intent.tenant_id,
            "request_hash": intent.request_hash,
            "conversation": intent.targets[0].model_dump(mode="json"),
            "message": target.model_dump(mode="json"),
        }

    def inventory(
        self, tx: Transaction, path: str, document: Document, intent: DeletionIntent
    ) -> tuple[tuple[str, Document], ...]:
        if not self.enabled(intent):
            return ()
        parts = path.split("/")
        target = None
        if len(parts) == 6 and parts[2] == "conversations" and parts[4] == "messages":
            parent = tx.get("/".join(parts[:4]))
            # Deleted/suppressed messages are instead proved by their accepted marker.
            if document.get("deleted") is True or document.get("suppress_reuse") is True:
                return ()
            if (
                parent is None
                or parent.get("id") != parts[3]
                or parent.get("tenant_id") != intent.tenant_id
                or document.get("id") != parts[5]
                or document.get("tenant_id") != intent.tenant_id
                or document.get("conversation_id") != parts[3]
            ):
                raise SourceProvenanceMissing()
            try:
                target = MessageOrigin.model_validate(
                    {
                        "resource_id": parts[5],
                        "parent_id": parts[3],
                        "version": document.get("version"),
                    }
                )
            except ValidationError:
                raise SourceProvenanceMissing() from None
        elif len(parts) == 4 and parts[2] == "deletion_tombstones":
            if document.get("target", {}).get("kind") != "message":
                return ()
            try:
                candidate = DeletionTarget.model_validate(document["target"])
                operation = TypeAdapter(ResourceId).validate_python(document.get("operation_id"))
            except ValidationError:
                return ()
            if path != tombstone_path(
                intent.tenant_id, "messages", candidate.resource_id, candidate.parent_id
            ):
                return ()
            accepted = self.accepted(tx, operation)
            if (
                accepted is None
                or accepted.tenant_id != intent.tenant_id
                or candidate not in accepted.targets
                or document.get("mode") != accepted.source_policy.replace("_source", "")
            ):
                return ()
            target = MessageOrigin.model_validate(
                candidate.model_dump(include={"resource_id", "parent_id", "version"})
            )
        if target is None:
            return ()
        # Epoch is not needed to classify a historical source. A later accepted
        # forget may increase it without changing the canonical parent/version.
        location = self.path(intent, target.resource_id)
        proof = self.proof(intent, target)
        previous = tx.get(location)
        if previous is not None and previous != proof:
            old = self.checked(previous, intent, target.resource_id)
            if old.parent_id != target.parent_id:
                raise SourceProvenanceMissing()
            if old.version >= target.version:
                return ()
        return ((location, proof),)

    def checked(self, proof: Document, intent: DeletionIntent, identity: str) -> MessageOrigin:
        try:
            target = MessageOrigin.model_validate(proof.get("message"))
        except ValidationError:
            raise SourceProvenanceMissing() from None
        if target.resource_id != identity or proof != self.proof(intent, target):
            raise SourceProvenanceMissing()
        return target

    def matches(self, tx: Transaction, ref: Document, intent: DeletionIntent) -> bool:
        if not self.enabled(intent):
            return False
        try:
            identity = TypeAdapter(ResourceId).validate_python(ref.get("resource_id"))
        except ValidationError:
            raise SourceProvenanceMissing() from None
        saved = tx.get(self.path(intent, identity))
        if saved is None:
            raise SourceProvenanceMissing()
        target = self.checked(saved, intent, identity)
        if type(ref.get("version")) is not int or not 1 <= ref["version"] <= target.version:
            raise SourceProvenanceMissing()
        return target.parent_id == intent.targets[0].resource_id
