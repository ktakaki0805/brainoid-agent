"""Trusted provider registration; labels and aliases never prove object identity."""

import hashlib
import json
from typing import Protocol

from brainoid_contracts import ResourceId, TenantContext
from brainoid_contracts.transactions import Transaction
from brainoid_policy.scopes import PolicyDenied
from pydantic import TypeAdapter, ValidationError

from brainoid_memory.models import Entity, EntityInput, ExternalRef
from brainoid_memory.repository import EntityTransaction, _validated


class Inventory(Protocol):
    """Server-injected port, never implemented by an HTTP/model request.

    Resolve current, provider-verified inventory from canonical DB records in tx.
    Verify tenant/integration/provider/opaque external ID, current integration and
    inventory permissions, owner and scope. No network/provider I/O in this callback.
    Missing inventory fails closed. DEV-06 will supply the real provider adapter.
    """

    def resolve(
        self, tx: Transaction, context: TenantContext, reference: ExternalRef
    ) -> EntityInput: ...


def external_key(tenant: ResourceId, reference: ExternalRef) -> str:
    """Unambiguous namespace; opaque provider IDs never become document paths."""
    data = [tenant, reference.integration_id, reference.provider, reference.external_id]
    return hashlib.sha256(json.dumps(data, ensure_ascii=True).encode("ascii")).hexdigest()


def register_external(
    tx: Transaction,
    access: EntityTransaction,
    identity: ResourceId,
    reference: ExternalRef,
    inventory: Inventory,
) -> Entity:
    """Atomically create an entity and unique external-ID index, or return its owner.

    Current WRITE authorization is required even for deduplication. An index is a
    lookup, not a permission. Existing values are never relabeled or made shared.
    The caller owns the transaction and must finish again after the commit response.
    """
    reference = _validated(ExternalRef, reference)
    context = access.principal.context
    values = _validated(EntityInput, inventory.resolve(tx, context, reference))
    if values.kind != "appliance" or values.external_refs != (reference,):
        raise PolicyDenied()
    path = f"tenants/{context.tenant_id}/entity_external_ids/{external_key(context.tenant_id, reference)}"
    current = tx.get(path)
    indexed = current is not None
    if current is None:
        # MEM-01 records may predate the unique index. Never create a second object,
        # including when the first is private, deleted or otherwise unreadable.
        legacy = [
            doc
            for _, doc in tx.collection(f"tenants/{context.tenant_id}/entities")
            if reference.model_dump(mode="json") in doc.get("external_refs", [])
        ]
        if len(legacy) > 1:
            raise PolicyDenied()
        if legacy:
            current = {
                "reference": reference.model_dump(mode="json"),
                "entity_id": legacy[0].get("id"),
                "deletion_epoch": legacy[0].get("deletion_epoch"),
            }
    if current is not None:
        if current.get("reference") != reference.model_dump(mode="json"):
            raise PolicyDenied()
        try:
            identity = TypeAdapter(ResourceId).validate_python(current.get("entity_id"))
        except ValidationError:
            raise PolicyDenied() from None
        record = access.get(identity, write=True)
        if (
            record.external_refs != (reference,)
            or record.kind != "appliance"
            or record.deletion_epoch != current.get("deletion_epoch")
            or record.deletion_epoch != 0
            or record.suppress_reuse
            or (record.visibility, record.display_policy)
            != (values.visibility, values.display_policy)
        ):
            raise PolicyDenied()
        access.finish()
        if not indexed:
            tx.create(path, current)
        return record
    record = access.create(identity, values)
    access.finish()
    tx.create(
        path,
        {
            "entity_id": record.id,
            "deletion_epoch": record.deletion_epoch,
            "reference": reference.model_dump(mode="json"),
        },
    )
    return record
