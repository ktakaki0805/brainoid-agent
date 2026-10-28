"""Metadata-only conversation adapter for the existing resource deletion protocol."""

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_contracts.deletion import fenced_document
from brainoid_policy.models import Action, ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from pydantic import StrictBool, ValidationError

from brainoid_memory.models import Epoch, MemoryValue
from brainoid_memory.repository import FactTransaction


class ConversationRecord(Scope, MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    owner_user_id: FirebaseUid | None
    created_by: FirebaseUid
    version: Version
    deletion_epoch: Epoch
    deleted: StrictBool
    suppress_reuse: StrictBool
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class ConversationDeletion:
    kind = ResourceKind.CONVERSATION

    @classmethod
    def path(cls, tenant: ResourceId, identity: ResourceId) -> str:
        return f"tenants/{tenant}/conversations/{identity}"

    @classmethod
    def scopes(cls, access: FactTransaction) -> tuple[Scope, ...]:
        allowed = access._authority.policy.search(
            access.principal.context.tenant_id,
            access.caller.subject,
            cls.kind,
            output=access._repo._output,
        )
        access.finish()
        return tuple(
            Scope(visibility=f.visibility, display_policy=d)
            for f in allowed.filters
            for d in sorted(f.display_policies)
            if f.visibility != Visibility.PRIVATE or d != DisplayPolicy.SHARED_DISPLAY
        )

    @classmethod
    def get(
        cls, access: FactTransaction, identity: ResourceId, *, write: bool = False
    ) -> tuple[ConversationRecord, ResourceGrant]:
        tenant, tx = access.principal.context.tenant_id, access._tx
        grant = access._authority.policy.resource(
            tenant,
            access.caller.subject,
            ResourceRef(tenant_id=tenant, kind=cls.kind, id=identity),
            action=Action.WRITE if write else Action.READ,
            output=access._repo._output,
        )
        raw = tx.get(cls.path(tenant, identity))
        if raw is None:
            raise PolicyDenied()
        raw = fenced_document(tx, tenant, str(cls.kind), identity, raw)
        try:
            record = ConversationRecord.model_validate(
                {key: raw.get(key) for key in ConversationRecord.model_fields}
            )
        except ValidationError:
            raise PolicyDenied() from None
        if (
            record.id != identity
            or record.tenant_id != tenant
            or record.deleted
            or record.deletion_epoch
        ):
            raise PolicyDenied()
        access.finish()
        return record, grant
