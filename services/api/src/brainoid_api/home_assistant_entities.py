"""Device-authenticated HA inventory, canonical external identity and command targets."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_contracts.access import AuthorizedScope, AuthorizedUserScope, fingerprint
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_memory.commands import ActionName, HomeTarget
from brainoid_memory.entity_resolver import external_key
from brainoid_memory.models import Entity, EntityInput, ExternalRef, Label, MemoryValue
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceSubject, ManagementAction, UserSubject
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field, ValidationError, model_validator

from brainoid_api.auth.account_state import account_stamp
from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository
from brainoid_api.repositories.devices import Devices
from brainoid_api.repositories.entities import EntityPage, EntityView


class HomeIntegrationCreate(MemoryValue):
    device_id: ResourceId
    name: Label


class HomeIntegration(MemoryValue):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    provider: Literal["home_assistant"] = "home_assistant"
    status: Literal["active", "revoked"]
    device_id: ResourceId


class HomeInventoryItem(MemoryValue):
    external_id: Annotated[
        str, Field(strict=True, pattern=r"^(light|climate)\.[a-z0-9_]+$", max_length=255)
    ]
    label: Label
    domain: Literal["light", "climate"]
    supported_actions: Annotated[tuple[ActionName, ...], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        if (
            not self.external_id.startswith(self.domain + ".")
            or any(not action.startswith(self.domain + ".") for action in self.supported_actions)
            or len(set(self.supported_actions)) != len(self.supported_actions)
        ):
            raise ValueError("invalid_inventory_capabilities")
        return self


class HomeInventorySync(MemoryValue):
    entities: Annotated[tuple[HomeInventoryItem, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({item.external_id for item in self.entities}) != len(self.entities):
            raise ValueError("duplicate_external_identity")
        return self


def integration_path(tenant: ResourceId, integration: ResourceId) -> str:
    return f"tenants/{tenant}/integrations/{integration}"


def integration_record(tx: Transaction, tenant: ResourceId, integration: ResourceId) -> Document:
    """One fenced loader for active authority and admin revoked-metadata views."""
    document = tx.get(integration_path(tenant, integration))
    if document is None:
        raise PolicyDenied()
    document = fenced_document(tx, tenant, "integrations", integration, document)
    if (
        document.get("deleted")
        or document.get("suppress_reuse")
        or document.get("provider") != "home_assistant"
        or document.get("id") != integration
        or document.get("tenant_id") != tenant
    ):
        raise PolicyDenied()
    return document


def inventory_state_path(tenant: ResourceId, integration: ResourceId) -> str:
    return f"tenants/{tenant}/home_inventory_states/{integration}"


class HomeInventory:
    """Trusted resolver reads canonical records and current independent authority."""

    def __init__(self, ledger: ControlLedger, now: Callable[[], datetime]) -> None:
        self.ledger, self.now = ledger, now

    def current(self, tx: Transaction, tenant: ResourceId, integration: ResourceId) -> Document:
        document = integration_record(tx, tenant, integration)
        try:
            view = HomeIntegration.model_validate(
                {field: document[field] for field in HomeIntegration.model_fields}
            )
            owner = FirebaseUid(document["source_user_id"])
        except (KeyError, ValueError):
            raise PolicyDenied() from None
        if (
            view.id != integration
            or view.tenant_id != tenant
            or view.status != "active"
            or document.get("visibility") != "shared"
            or document.get("owner_user_id") is not None
        ):
            raise PolicyDenied()
        account_stamp(tx, owner)
        policy = transaction_policy(
            tx, ledger=self.ledger, allow_untracked_bootstrap=True, now=self.now
        )
        policy.management(tenant, UserSubject(user_id=owner), ManagementAction.MANAGE_DEVICES)
        device = policy._state.device(tenant, view.device_id)
        if device is None:
            raise PolicyDenied()
        policy.principal(
            tenant,
            DeviceSubject(
                device_id=view.device_id, connection_generation=device.connection_generation
            ),
        )
        return document

    @staticmethod
    def state(tx: Transaction, tenant: ResourceId, integration: ResourceId) -> Document:
        document = tx.get(inventory_state_path(tenant, integration))
        if document is None:
            raise PolicyDenied()
        document = fenced_document(tx, tenant, "home_inventory_states", integration, document)
        if (
            document.get("deleted")
            or document.get("suppress_reuse")
            or document.get("schema_version") != 1
        ):
            raise PolicyDenied()
        for field in ("inventory_keys", "known_keys"):
            keys = document.get(field)
            if (
                not isinstance(keys, list)
                or len(keys) > 100
                or any(
                    not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None
                    for key in keys
                )
                or len(set(keys)) != len(keys)
            ):
                raise PolicyDenied()
        if not set(document["inventory_keys"]) <= set(document["known_keys"]):
            raise PolicyDenied()
        return {field: document[field] for field in ("inventory_keys", "known_keys")}

    def resolve(
        self, tx: Transaction, context: TenantContext, reference: ExternalRef
    ) -> EntityInput:
        integration = self.current(tx, context.tenant_id, reference.integration_id)
        key = external_key(context.tenant_id, reference)
        path = f"tenants/{context.tenant_id}/home_inventory/{key}"
        record = tx.get(path)
        if record is None:
            raise PolicyDenied()
        if (
            tx.get("resource_deletion_fences/" + fingerprint(path)) is not None
            or record.get("deleted")
            or record.get("suppress_reuse")
        ):
            raise PolicyDenied()
        if (
            record.get("reference") != reference.model_dump(mode="json")
            or record.get("device_id") != integration["device_id"]
            or record.get("status") != "active"
        ):
            raise PolicyDenied()
        try:
            item = HomeInventoryItem.model_validate(record["item"])
        except (KeyError, ValueError):
            raise PolicyDenied() from None
        if item.external_id != reference.external_id:
            raise PolicyDenied()
        return EntityInput(
            kind="appliance",
            label=item.label,
            external_refs=(reference,),
            visibility=Visibility.SHARED,
            display_policy=DisplayPolicy.SHARED_DISPLAY,
        )


class HomeAssistantEntities:
    def __init__(
        self,
        store: TransactionStore,
        devices: Devices,
        credentials: DeviceCredentialRepository,
        *,
        ledger: ControlLedger,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = GuardedTransactions(store)
        self.devices, self.credentials, self.ledger, self.now = devices, credentials, ledger, now
        self.inventory = HomeInventory(ledger, now)
        self.idempotency = Idempotency(self.store, now=now)

    @staticmethod
    def _view(document: Document) -> HomeIntegration:
        return HomeIntegration.model_validate(
            {field: document[field] for field in HomeIntegration.model_fields}
        )

    def _admin(
        self,
        tx: Transaction,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        device: ResourceId,
    ) -> AuthorizedUserScope:
        user = self.devices._authorize(tx, identity, tenant)
        if self.devices._state(tx, tenant, device).status != "active":
            raise PolicyDenied()
        protect(tx, lambda: user.ensure_live(self.now()))
        return user

    def create(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        body: HomeIntegrationCreate,
        key: str | None,
    ) -> HomeIntegration:
        integration = ResourceId(str(uuid4()))

        def authorize(tx: Transaction, receipt: MutationResult | None) -> AuthorizedUserScope:
            scope = self._admin(tx, identity, tenant, body.device_id)
            if receipt:
                assert receipt.resource_id is not None
                current = self.inventory.current(tx, tenant, receipt.resource_id)
                if (
                    current["device_id"] != body.device_id
                    or current["source_user_id"] != identity.user_id
                ):
                    raise PolicyDenied()
            return scope

        def mutate(tx: Transaction, scope: AuthorizedUserScope) -> MutationResult:
            now = self.now()
            value = HomeIntegration(
                id=integration,
                tenant_id=tenant,
                version=1,
                created_at=now,
                updated_at=now,
                status="active",
                device_id=body.device_id,
            )
            tx.create(
                integration_path(tenant, integration),
                {
                    **value.model_dump(mode="json"),
                    "source_user_id": identity.user_id,
                    "name": body.name,
                    "visibility": "shared",
                    "owner_user_id": None,
                },
            )
            tx.create(
                inventory_state_path(tenant, integration),
                {"schema_version": 1, "inventory_keys": [], "known_keys": []},
            )
            return MutationResult(status_code=201, resource_id=integration, version=1)

        receipt = policy_call(
            lambda: self.idempotency.execute_user(
                operation=f"home.create:{tenant}",
                key=key,
                body=body.model_dump(mode="json"),
                authorize=authorize,
                mutate=mutate,
            )
        )
        assert receipt.resource_id is not None
        return self.get(identity, tenant, receipt.resource_id)

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, integration: ResourceId
    ) -> HomeIntegration:
        def read(tx: Transaction) -> HomeIntegration:
            document = integration_record(tx, tenant, integration)
            self._admin(tx, identity, tenant, ResourceId(document["device_id"]))
            return self._view(document)

        return policy_call(lambda: self.store.run(read))

    def revoke(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        key: str | None,
    ) -> HomeIntegration:
        def authorize(tx: Transaction, receipt: MutationResult | None) -> AuthorizedUserScope:
            doc = integration_record(tx, tenant, integration)
            return self._admin(tx, identity, tenant, ResourceId(doc["device_id"]))

        def mutate(tx: Transaction, scope: AuthorizedUserScope) -> MutationResult:
            path = integration_path(tenant, integration)
            doc = integration_record(tx, tenant, integration)
            if doc["status"] != "revoked":
                doc.update(
                    status="revoked", version=doc["version"] + 1, updated_at=self.now().isoformat()
                )
                tx.set(path, doc)
            return MutationResult(status_code=200, resource_id=integration)

        policy_call(
            lambda: self.idempotency.execute_user(
                operation=f"home.revoke:{tenant}:{integration}",
                key=key,
                body={},
                authorize=authorize,
                mutate=mutate,
            )
        )
        return self.get(identity, tenant, integration)

    def _device(
        self, tx: Transaction, identity: DeviceIdentity, tenant: ResourceId, integration: ResourceId
    ) -> tuple[AuthorizedScope, Document]:
        self.credentials.authorize(tx, identity)
        raw = tx.get(integration_path(tenant, integration))
        if identity.tenant_id != tenant or raw is None or raw.get("device_id") != identity.sub:
            raise PolicyDenied()
        try:
            doc = self.inventory.current(tx, tenant, integration)
        except PolicyDenied:
            # Only the current, exactly-bound device learns that this configured
            # integration is no longer usable; item refusals never take this path.
            raise APIError(ErrorCode.RESOURCE_EXPIRED) from None
        policy = transaction_policy(
            tx, ledger=self.ledger, allow_untracked_bootstrap=True, now=self.now
        )
        principal = policy.principal(
            tenant,
            DeviceSubject(
                device_id=identity.sub, connection_generation=identity.connection_generation
            ),
        )
        family = tx.get("device_credential_families/" + identity.family_id)
        assert family is not None
        from pydantic import TypeAdapter

        expires = min(
            datetime.fromtimestamp(identity.exp, UTC),
            TypeAdapter(UtcTimestamp).validate_python(family["expires_at"]),
        )

        def live() -> None:
            if not identity.iat <= self.now().timestamp() < expires.timestamp():
                raise PolicyDenied()

        protect(tx, live)
        return AuthorizedScope(
            context=principal.context,
            scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.SHARED_DISPLAY),
            owner_user_id=None,
            authorization_stamp=fingerprint(
                [
                    principal.stamp,
                    identity.family_id,
                    identity.credential_epoch,
                    doc["source_user_id"],
                    doc["status"],
                ]
            ),
        ), {**doc, **self.inventory.state(tx, tenant, integration)}

    def sync(
        self,
        identity: DeviceIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        body: HomeInventorySync,
        key: str | None,
    ) -> EntityPage:
        def authorize(tx: Transaction, receipt: MutationResult | None) -> AuthorizedScope:
            scope, _ = self._device(tx, identity, tenant, integration)
            return scope

        def mutate(tx: Transaction, scope: AuthorizedScope) -> MutationResult:
            _, doc = self._device(tx, identity, tenant, integration)
            old_keys = doc.get("inventory_keys", [])
            if not isinstance(old_keys, list) or len(old_keys) > 100:
                raise PolicyDenied()
            writes: list[tuple[str, Document]] = []
            new_keys = []
            for item in body.entities:
                reference = ExternalRef(
                    provider="home_assistant",
                    integration_id=integration,
                    external_id=item.external_id,
                )
                external = external_key(tenant, reference)
                index_path = f"tenants/{tenant}/entity_external_ids/{external}"
                inventory_path = f"tenants/{tenant}/home_inventory/{external}"
                target_path = f"tenants/{tenant}/home_targets/{external}"
                if any(
                    tx.get("resource_deletion_fences/" + fingerprint(path)) is not None
                    for path in (index_path, inventory_path, target_path)
                ):
                    continue
                indexed = tx.get(index_path)
                prior_inventory, prior_target = tx.get(inventory_path), tx.get(target_path)
                if indexed:
                    if indexed.get("reference") != reference.model_dump(mode="json"):
                        raise PolicyDenied()
                    entity_id = ResourceId(indexed["entity_id"])
                    document = tx.get(f"tenants/{tenant}/entities/{entity_id}")
                    if document is None:
                        continue
                    try:
                        entity = Entity.model_validate(
                            fenced_document(tx, tenant, "entities", entity_id, document)
                        )
                    except ValidationError:
                        raise PolicyDenied() from None
                    if (
                        entity.external_refs != (reference,)
                        or entity.tenant_id != tenant
                        or entity.id != entity_id
                        or entity.kind != "appliance"
                    ):
                        raise PolicyDenied()
                    if (
                        entity.deleted
                        or entity.deletion_epoch
                        or entity.suppress_reuse
                        or indexed.get("deletion_epoch") != 0
                        or entity.visibility != Visibility.SHARED
                        or entity.display_policy != DisplayPolicy.SHARED_DISPLAY
                    ):
                        # A canonical denial excludes this item, not its healthy
                        # siblings. Retain its unique index; never recreate/rebind it.
                        continue
                    # Preserve user-edited canonical labels/aliases. HA rename updates
                    # provider metadata, never identity or personal naming choices.
                else:
                    if prior_inventory is not None or prior_target is not None:
                        raise PolicyDenied()
                    # Exact provider-reference array membership is indexed and
                    # capped before materialization. Private/deleted legacy rows
                    # still block reuse; unrelated catalogs never participate.
                    if tx.find_array(
                        f"tenants/{tenant}/entities",
                        "external_refs",
                        reference.model_dump(mode="json"),
                    ):
                        raise PolicyDenied()
                    entity_id = ResourceId(str(uuid4()))
                    now = self.now()
                    entity = Entity(
                        id=entity_id,
                        tenant_id=tenant,
                        kind="appliance",
                        label=item.label,
                        external_refs=(reference,),
                        visibility=Visibility.SHARED,
                        display_policy=DisplayPolicy.SHARED_DISPLAY,
                        owner_user_id=None,
                        created_by=FirebaseUid(doc["source_user_id"]),
                        created_via_device_id=identity.sub,
                        version=1,
                        deletion_epoch=0,
                        schema_version=1,
                        created_at=now,
                        updated_at=now,
                        deleted=False,
                        suppress_reuse=False,
                    )
                    writes.append(
                        (f"tenants/{tenant}/entities/{entity_id}", entity.model_dump(mode="json"))
                    )
                    writes.append(
                        (
                            index_path,
                            {
                                "entity_id": entity_id,
                                "reference": reference.model_dump(mode="json"),
                                "deletion_epoch": 0,
                            },
                        )
                    )
                inventory = {
                    "reference": reference.model_dump(mode="json"),
                    "device_id": identity.sub,
                    "entity_id": entity_id,
                    "status": "active",
                    "item": item.model_dump(mode="json"),
                }
                new_keys.append(external)
                if prior_inventory is not None and any(
                    prior_inventory.get(field) != inventory[field]
                    for field in ("reference", "device_id", "entity_id")
                ):
                    raise PolicyDenied()
                if prior_target is not None:
                    try:
                        previous = HomeTarget.model_validate(prior_target)
                    except ValidationError:
                        raise PolicyDenied() from None
                    if (
                        previous.tenant_id,
                        previous.device_id,
                        previous.entity_id,
                        previous.reference,
                    ) != (tenant, identity.sub, entity_id, reference):
                        raise PolicyDenied()
                if prior_inventory != inventory:
                    writes.append((inventory_path, inventory))
                target = HomeTarget(
                    tenant_id=tenant,
                    device_id=identity.sub,
                    entity_id=entity_id,
                    reference=reference,
                    version=prior_target["version"] if prior_target else 1,
                    status="active",
                    allowed_actions=tuple(sorted(item.supported_actions)),
                )
                if prior_target != target.model_dump(mode="json"):
                    if prior_target:
                        target = target.model_copy(update={"version": target.version + 1})
                    writes.append((target_path, target.model_dump(mode="json")))
            for removed in set(old_keys) - set(new_keys):
                for collection in ("home_inventory", "home_targets"):
                    path = f"tenants/{tenant}/{collection}/{removed}"
                    value = tx.get(path)
                    fenced = tx.get("resource_deletion_fences/" + fingerprint(path)) is not None
                    if value and not fenced:
                        value["status"] = "revoked"
                        if collection == "home_targets":
                            value["version"] += 1
                        writes.append((path, value))
            known = set(doc.get("known_keys", [])) | set(new_keys)
            if len(known) > 100:
                raise APIError(ErrorCode.INVALID_REQUEST)
            if sorted(old_keys) != sorted(new_keys):
                writes.append(
                    (
                        inventory_state_path(tenant, integration),
                        {
                            "schema_version": 1,
                            "inventory_keys": sorted(new_keys),
                            "known_keys": sorted(known),
                        },
                    )
                )
            # All reads precede writes, including authority and deletion fences.
            for path, value in writes:
                tx.set(path, value)
            return MutationResult(status_code=200, resource_id=integration)

        policy_call(
            lambda: self.idempotency.execute(
                operation=f"home.sync:{integration}",
                key=key,
                body=body.model_dump(mode="json"),
                authorize=authorize,
                mutate=mutate,
            )
        )

        def read(tx: Transaction) -> EntityPage:
            scope, doc = self._device(tx, identity, tenant, integration)
            items = []
            for key in doc["inventory_keys"]:
                index = tx.get(f"tenants/{tenant}/entity_external_ids/{key}")
                if index is None:
                    raise PolicyDenied()
                reference = ExternalRef.model_validate(index["reference"])
                if (
                    reference.integration_id != integration
                    or external_key(tenant, reference) != key
                    or index.get("deletion_epoch") != 0
                ):
                    raise PolicyDenied()
                self.inventory.resolve(tx, scope.context, reference)
                entity_id = ResourceId(index["entity_id"])
                value = tx.get(f"tenants/{tenant}/entities/{entity_id}")
                if value is None:
                    raise PolicyDenied()
                value = fenced_document(tx, tenant, "entities", entity_id, value)
                entity = Entity.model_validate(value)
                if (
                    entity.deleted
                    or entity.deletion_epoch
                    or entity.suppress_reuse
                    or entity.external_refs != (reference,)
                    or entity.tenant_id != tenant
                    or entity.id != entity_id
                    or entity.kind != "appliance"
                    or entity.visibility != Visibility.SHARED
                    or entity.display_policy != DisplayPolicy.SHARED_DISPLAY
                ):
                    raise PolicyDenied()
                items.append(
                    EntityView.model_validate(
                        {field: getattr(entity, field) for field in EntityView.model_fields}
                    )
                )
            return EntityPage(items=items, next_cursor=None)

        return policy_call(lambda: self.store.run(read))
