"""HTTP credential adapter for the shared command service."""

from brainoid_contracts import ResourceId
from brainoid_memory.commands import Command, CommandCreate, Commands, view
from brainoid_memory.models import ExternalRef, MemoryValue
from brainoid_memory.repository import FactTransaction

from brainoid_api.common.versioning import require_if_match
from brainoid_api.repositories.entities import EntityIdentity
from brainoid_api.repositories.memories import Memories, policy_call


class CommandTarget(MemoryValue):
    """Exact control target; home.control does not grant general entity reads."""

    tenant_id: ResourceId
    device_id: ResourceId
    command_id: ResourceId
    target_entity_id: ResourceId
    reference: ExternalRef


class CommandRequests:
    def __init__(self, access: Memories) -> None:
        self.access = access
        self.service = Commands(access.store, now=access.now)

    def create(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: CommandCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> Command:
        return policy_call(
            lambda: view(
                self.service.create(
                    lambda tx: self.access._open(tx, identity, tenant, session), body, key
                )
            )
        )

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        command: ResourceId,
        session: ResourceId | None = None,
    ) -> Command:
        return policy_call(
            lambda: view(
                self.service.read(
                    lambda tx: self.access._open(tx, identity, tenant, session), command
                )
            )
        )

    def cancel(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        command: ResourceId,
        key: str | None,
        match: str | None,
        session: ResourceId | None = None,
    ) -> Command:
        return policy_call(
            lambda: view(
                self.service.cancel(
                    lambda tx: self.access._open(tx, identity, tenant, session),
                    command,
                    key,
                    None if match is None else require_if_match(match),
                )
            )
        )

    def target(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        command: ResourceId,
        session: ResourceId | None = None,
    ) -> CommandTarget:
        from brainoid_contracts.transactions import Transaction

        def read(tx: Transaction) -> tuple[CommandTarget, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            record = Commands.get(access, command)
            entity, _ = Commands.target(
                access,
                CommandCreate(
                    device_id=record.device_id,
                    target_entity_id=record.target_entity_id,
                    operation=record.operation,
                ),
            )
            access.finish()
            return CommandTarget(
                tenant_id=tenant,
                device_id=record.device_id,
                command_id=command,
                target_entity_id=entity.id,
                reference=entity.external_refs[0],
            ), access

        def release() -> CommandTarget:
            result, access = self.service.store.run(read)
            access.finish()
            return result

        return policy_call(release)
