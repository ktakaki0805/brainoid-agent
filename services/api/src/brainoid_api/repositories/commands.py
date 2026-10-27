"""HTTP credential adapter for the shared command service."""

from brainoid_contracts import ResourceId
from brainoid_memory.commands import Command, CommandCreate, Commands, view

from brainoid_api.common.versioning import require_if_match
from brainoid_api.repositories.entities import EntityIdentity
from brainoid_api.repositories.memories import Memories, policy_call


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
