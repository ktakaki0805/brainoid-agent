"""Accepted home commands, with current authority and atomic pre-start cancellation.

This module performs no network I/O and never grants a physical execution start.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import GuardedTransactions
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Transaction, TransactionStore
from brainoid_policy.accounts import account_stamp
from brainoid_policy.devices import (
    CredentialBinding,
    DeviceBinding,
    current_device,
    current_family,
)
from brainoid_policy.models import Action, DeviceSubject, ResourceKind, ResourceRef, Subject
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field, ValidationError, field_validator, model_validator

from brainoid_memory.entity_resolver import external_key
from brainoid_memory.models import Entity, ExternalRef, MemoryValue
from brainoid_memory.mutations import MutationScope
from brainoid_memory.repository import FactTransaction, VersionConflict


class LightParameters(MemoryValue):
    brightness_pct: Annotated[int, Field(strict=True, ge=1, le=100)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("brightness_pct", mode="before")
    @classmethod
    def nonnull(cls, value: object) -> object:
        if value is None:
            raise ValueError("Omit absent brightness")
        return value


class EmptyParameters(MemoryValue):
    pass


class TemperatureParameters(MemoryValue):
    temperature_c: Annotated[float, Field(strict=True, ge=16, le=30)]


class ModeParameters(MemoryValue):
    hvac_mode: Literal["off", "heat", "cool", "auto"]


class LightOn(MemoryValue):
    action: Literal["light.turn_on"]
    parameters: LightParameters


class LightOff(MemoryValue):
    action: Literal["light.turn_off"]
    parameters: EmptyParameters


class Temperature(MemoryValue):
    action: Literal["climate.set_temperature"]
    parameters: TemperatureParameters


class Mode(MemoryValue):
    action: Literal["climate.set_hvac_mode"]
    parameters: ModeParameters


HomeAction = Annotated[LightOn | LightOff | Temperature | Mode, Field(discriminator="action")]
ActionName = Literal[
    "light.turn_on", "light.turn_off", "climate.set_temperature", "climate.set_hvac_mode"
]
Status = Literal[
    "queued",
    "delivered",
    "accepted",
    "running",
    "succeeded",
    "failed",
    "expired",
    "cancelled",
    "outcome_unknown",
]
PRE_START = frozenset({"queued", "delivered", "accepted"})


class CommandCreate(MemoryValue):
    device_id: ResourceId
    target_entity_id: ResourceId
    operation: HomeAction


class ObservedState(MemoryValue):
    power: Literal["on", "off"] | None = None
    brightness_pct: Annotated[int, Field(strict=True, ge=0, le=100)] | None = None
    temperature_c: Annotated[float, Field(strict=True)] | None = None
    hvac_mode: Literal["off", "heat", "cool", "auto"] | None = None


class Command(CommandCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    status: Status
    expires_at: UtcTimestamp
    execution_id: ResourceId | None = None
    observed_state: ObservedState | None = None
    error_code: Annotated[str, Field(strict=True, max_length=100)] | None = None

    @model_validator(mode="after")
    def success_observed(self) -> Self:
        if self.status == "succeeded" and self.observed_state is None:
            raise ValueError("Success requires observed state")
        return self


class HomeTarget(MemoryValue):
    """Trusted DEV-06 inventory snapshot, never accepted from HTTP or tool input."""

    tenant_id: ResourceId
    device_id: ResourceId
    entity_id: ResourceId
    reference: ExternalRef
    version: Version
    status: Literal["active", "revoked"]
    allowed_actions: tuple[ActionName, ...]


class Binding(MemoryValue):
    authorization_version: Version
    connection_generation: Version
    target_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ActorCredential(CredentialBinding):
    family_expires_at: UtcTimestamp


class CommandDelivery(MemoryValue):
    sequence: Version
    family_id: ResourceId
    credential_epoch: ResourceId
    private_session_id: ResourceId | None = None


class CommandExecution(MemoryValue):
    generation: Version
    claimed_at: UtcTimestamp
    expires_at: UtcTimestamp


class CommandRecord(Command, Scope):
    schema_version: Literal[1] = 1
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)] = 0
    deleted: Literal[False] = False
    suppress_reuse: Literal[False] = False
    owner_user_id: FirebaseUid | None
    created_by: str
    actor: Subject
    actor_stamp: str
    account_stamp: str | None
    binding: Binding
    actor_credential: ActorCredential | None = None
    delivery: CommandDelivery | None = None
    accepted_execution_id: ResourceId | None = None
    execution: CommandExecution | None = None
    status_receipts: dict[str, dict[str, Any]] = Field(default_factory=dict)


def command_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/commands/{identity}"


def view(record: CommandRecord) -> Command:
    return Command.model_validate({k: getattr(record, k) for k in Command.model_fields})


class Commands:
    def __init__(self, store: TransactionStore, *, now: Callable[[], datetime]) -> None:
        self.store = store if isinstance(store, GuardedTransactions) else GuardedTransactions(store)
        self.now = now
        self.idempotency = Idempotency(self.store, now=now)

    @staticmethod
    def credential(
        access: FactTransaction, actor: Subject, binding: CredentialBinding | None
    ) -> ActorCredential | None:
        if not isinstance(actor, DeviceSubject):
            if binding is not None:
                raise PolicyDenied()
            return None
        if (
            binding is None
            or binding.tenant_id != access.principal.context.tenant_id
            or binding.device_id != actor.device_id
            or binding.connection_generation != actor.connection_generation
        ):
            # Legacy device commands cannot acquire evidence from a new reader.
            raise PolicyDenied()
        family = current_family(
            access._tx,
            binding,
            credential_epoch=access._repo._credential_epoch,
            now=access._repo._now,
        )
        current_device(
            access._authority.policy,
            binding,
            private_session_id=actor.private_session_id,
        )
        return ActorCredential(
            **{k: getattr(binding, k) for k in CredentialBinding.model_fields},
            family_expires_at=family["expires_at"],
        )

    @staticmethod
    def target(access: FactTransaction, body: CommandCreate) -> tuple[Entity, Binding]:
        """Same current policy/ledger/credential authority as all canonical resources."""
        tenant, tx = access.principal.context.tenant_id, access._tx
        policy = access._authority.policy
        if "home.control" not in access.principal.context.actor.allowed_scopes:
            raise PolicyDenied()
        policy.resource(
            tenant,
            access.caller.subject,
            ResourceRef(tenant_id=tenant, kind=ResourceKind.ENTITY, id=body.target_entity_id),
            action=Action.CONTROL,
            output=access._repo._output,
        )
        document = tx.get(f"tenants/{tenant}/entities/{body.target_entity_id}")
        if document is None:
            raise PolicyDenied()
        try:
            entity = Entity.model_validate(
                fenced_document(tx, tenant, "entities", body.target_entity_id, document)
            )
        except ValidationError:
            raise PolicyDenied() from None
        if entity.deleted or entity.deletion_epoch:
            raise PolicyDenied()
        if entity.kind != "appliance" or len(entity.external_refs) != 1 or entity.suppress_reuse:
            raise PolicyDenied()
        reference = entity.external_refs[0]
        key = external_key(tenant, reference)
        integration = tx.get(f"tenants/{tenant}/integrations/{reference.integration_id}")
        index = tx.get(f"tenants/{tenant}/entity_external_ids/{key}")
        if any(
            tx.get(f"resource_deletion_fences/{fingerprint(path)}") is not None
            for path in (
                f"tenants/{tenant}/integrations/{reference.integration_id}",
                f"tenants/{tenant}/home_targets/{key}",
                f"tenants/{tenant}/entity_external_ids/{key}",
            )
        ):
            raise PolicyDenied()
        try:
            target = HomeTarget.model_validate(tx.get(f"tenants/{tenant}/home_targets/{key}"))
        except ValidationError:
            raise PolicyDenied() from None
        if (
            integration is None
            or integration.get("id") != reference.integration_id
            or integration.get("tenant_id") != tenant
            or integration.get("status") != "active"
            or integration.get("provider") != reference.provider
            or integration.get("device_id") != body.device_id
            or integration.get("owner_user_id") not in (None, access.principal.user_id)
            or (
                integration.get("visibility") == "private"
                and entity.visibility != Visibility.PRIVATE
            )
            or index
            != {
                "reference": reference.model_dump(mode="json"),
                "entity_id": entity.id,
                "deletion_epoch": 0,
            }
            or target.tenant_id != tenant
            or target.device_id != body.device_id
            or target.entity_id != entity.id
            or target.reference != reference
            or target.status != "active"
            or body.operation.action not in target.allowed_actions
        ):
            raise PolicyDenied()
        device = policy._state.device(tenant, body.device_id)
        if device is None:
            raise PolicyDenied()
        current_device(
            policy,
            DeviceBinding(
                tenant_id=tenant,
                device_id=body.device_id,
                authorization_version=device.authorization_version,
                connection_generation=device.connection_generation,
            ),
            scope="home.control",
            entity_id=entity.id,
        )
        # A Pi credential controls only its own paired appliance allowlist.
        if (
            isinstance(access.caller.subject, DeviceSubject)
            and access.caller.subject.device_id != body.device_id
        ):
            raise PolicyDenied()
        binding = Binding(
            authorization_version=device.authorization_version,
            connection_generation=device.connection_generation,
            target_fingerprint=fingerprint(
                [entity.model_dump(mode="json"), integration, target.model_dump(mode="json")]
            ),
        )
        access.finish()
        return entity, binding

    @classmethod
    def get(cls, access: FactTransaction, identity: ResourceId) -> CommandRecord:
        tenant, tx = access.principal.context.tenant_id, access._tx
        doc = tx.get(command_path(tenant, identity))
        if doc is None:
            raise PolicyDenied()
        try:
            record = CommandRecord.model_validate(
                fenced_document(tx, tenant, "commands", identity, doc)
            )
        except ValidationError:
            raise PolicyDenied() from None
        if record.id != identity or record.tenant_id != tenant or record.deletion_epoch:
            raise PolicyDenied()
        policy = access._authority.policy
        policy.resource(
            tenant,
            access.caller.subject,
            ResourceRef(tenant_id=tenant, kind=ResourceKind.COMMAND, id=identity),
            output=access._repo._output,
        )
        _, binding = cls.target(
            access,
            CommandCreate.model_validate(
                {k: getattr(record, k) for k in CommandCreate.model_fields}
            ),
        )
        if binding != record.binding:
            raise PolicyDenied()
        if cls.credential(access, record.actor, record.actor_credential) != record.actor_credential:
            raise PolicyDenied()
        initiator = policy.principal(tenant, record.actor)
        if initiator.stamp != record.actor_stamp or (
            initiator.user_id is not None
            and account_stamp(tx, initiator.user_id) != record.account_stamp
        ):
            raise PolicyDenied()
        access.finish()
        return record

    def create(
        self,
        opened: Callable[[Transaction], FactTransaction],
        body: CommandCreate,
        key: str | None,
        *,
        effect: ResourceId | None = None,
        extra: Callable[[Transaction, CommandRecord], Callable[[], None]] | None = None,
    ) -> CommandRecord:
        body = CommandCreate.model_validate(body.model_dump())
        identity = effect or ResourceId(str(uuid4()))
        access: FactTransaction | None = None
        binding: Binding | None = None
        actor_credential: ActorCredential | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access, binding, actor_credential
            access = opened(tx)
            entity, binding = self.target(access, body)
            actor_credential = self.credential(
                access, access.caller.subject, access.caller.device_credential
            )
            if replay is not None:
                assert replay.resource_id is not None
                self.get(access, replay.resource_id)
            return MutationScope(
                context=access.principal.context,
                scope=Scope(visibility=entity.visibility, display_policy=entity.display_policy),
                owner_user_id=access.principal.user_id,
                authorization_stamp=fingerprint(
                    [
                        access.principal.stamp,
                        binding.model_dump(mode="json"),
                        None
                        if actor_credential is None
                        else actor_credential.model_dump(mode="json"),
                        None
                        if access.principal.user_id is None
                        else account_stamp(tx, access.principal.user_id),
                    ]
                ),
                valid_from=access.caller.valid_from,
                valid_to=access.caller.valid_to,
            )

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None and binding is not None
            tenant, principal = current.context.tenant_id, access.principal
            path = command_path(tenant, identity)
            if tx.get(path) is not None or fenced_document(
                tx,
                tenant,
                "commands",
                identity,
                {"visibility": current.scope.visibility, "owner_user_id": principal.user_id},
            ).get("deleted"):
                raise PolicyDenied()
            now = self.now()
            record = CommandRecord(
                **body.model_dump(),
                **current.scope.model_dump(),
                id=identity,
                tenant_id=tenant,
                version=1,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=30),
                status="queued",
                owner_user_id=principal.user_id
                if current.scope.visibility == Visibility.PRIVATE
                else None,
                created_by=str(principal.user_id or principal.context.actor.actor_id),
                actor=access.caller.subject,
                actor_stamp=principal.stamp,
                account_stamp=None
                if principal.user_id is None
                else account_stamp(tx, principal.user_id),
                binding=binding,
                actor_credential=actor_credential,
            )
            save = extra(tx, record) if extra is not None else None
            access.finish()
            tx.create(path, record.model_dump(mode="json"))
            if save is not None:
                save()
            return MutationResult(status_code=202, resource_id=identity, version=1)

        receipt = self.idempotency.execute(
            operation="commands.create",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=mutate,
        )
        assert access is not None and receipt.resource_id is not None
        access.finish()
        return self.read(opened, receipt.resource_id)

    def read(
        self, opened: Callable[[Transaction], FactTransaction], identity: ResourceId
    ) -> CommandRecord:
        def read(tx: Transaction) -> tuple[CommandRecord, FactTransaction]:
            access = opened(tx)
            record = self.get(access, identity)
            if (
                record.status in PRE_START
                and record.execution_id is None
                and self.now() >= record.expires_at
            ):
                record = record.model_copy(
                    update={
                        "status": "expired",
                        "version": record.version + 1,
                        "updated_at": self.now(),
                    }
                )
                access.finish()
                tx.set(command_path(record.tenant_id, identity), record.model_dump(mode="json"))
            return record, access

        record, access = self.store.run(read)
        access.finish()
        return record

    def cancel(
        self,
        opened: Callable[[Transaction], FactTransaction],
        identity: ResourceId,
        key: str | None,
        expected: Version | None,
    ) -> CommandRecord:
        access: FactTransaction | None = None
        record: CommandRecord | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access, record
            access = opened(tx)
            record = self.get(access, identity)
            if access.caller.subject != record.actor:
                raise PolicyDenied()
            return MutationScope(
                context=access.principal.context,
                scope=Scope(visibility=record.visibility, display_policy=record.display_policy),
                owner_user_id=record.owner_user_id,
                authorization_stamp=record.actor_stamp,
                valid_from=access.caller.valid_from,
                valid_to=access.caller.valid_to,
            )

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert record is not None and access is not None
            if record.execution_id is not None:
                raise APIError(ErrorCode.CONFLICT)
            if record.status == "expired" or (
                record.status in PRE_START and self.now() >= record.expires_at
            ):
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            if record.status not in PRE_START:
                raise APIError(ErrorCode.CONFLICT)
            if expected is not None and record.version != expected:
                raise VersionConflict()
            updated = record.model_copy(
                update={
                    "status": "cancelled",
                    "version": record.version + 1,
                    "updated_at": self.now(),
                }
            )
            access.finish()
            tx.set(command_path(record.tenant_id, identity), updated.model_dump(mode="json"))
            return MutationResult(status_code=200, resource_id=identity, version=updated.version)

        self.idempotency.execute(
            operation=f"commands.cancel:{identity}",
            key=key,
            body={"expected_version": expected},
            authorize=authorize,
            mutate=mutate,
        )
        assert access is not None
        access.finish()
        return self.read(opened, identity)
