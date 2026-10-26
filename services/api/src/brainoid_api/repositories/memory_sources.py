"""Resolve message provenance from canonical saved conversation children only."""

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.models import Message
from brainoid_runs.state import view
from pydantic import TypeAdapter, ValidationError


class SavedMessages:
    """Transaction-scoped, tenant-bounded lookup; never uses request source text/paths.

    The current persistence port has no generic collection-group source resolver.
    Scan canonical conversation IDs and probe the exact child UUID. A future server
    index can optimize this, but must still prove the unique current real parent.
    """

    def conversation_id(
        self, tx: Transaction, tenant_id: ResourceId, message_id: ResourceId
    ) -> ResourceId:
        matches = []
        base = f"tenants/{tenant_id}/conversations"
        for path, parent in tx.collection(base):
            try:
                identity = TypeAdapter(ResourceId).validate_python(parent.get("id"))
            except ValidationError:
                continue
            if path != f"{base}/{identity}" or parent.get("tenant_id") != tenant_id:
                continue
            child = tx.get(f"{path}/messages/{message_id}")
            if child is None:
                continue
            try:
                saved = view(Message, child)
            except ValidationError:
                raise PolicyDenied() from None
            if (
                saved.id != message_id
                or child.get("tenant_id") != tenant_id
                or child.get("conversation_id") != identity
            ):
                raise PolicyDenied()
            matches.append(identity)
        if len(matches) != 1:
            raise PolicyDenied()
        return matches[0]
