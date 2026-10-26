"""HTTP identity and pagination adapter for the common reminder service."""

from brainoid_contracts import DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.transactions import Transaction
from brainoid_memory.reminders import (
    ReminderCreate,
    ReminderPage,
    ReminderPatch,
    Reminders,
    ReminderSnooze,
    ReminderStatus,
    ReminderView,
    view,
)
from brainoid_memory.repository import FactTransaction
from pydantic import JsonValue

from brainoid_api.common.pagination import page_limit
from brainoid_api.common.versioning import require_if_match
from brainoid_api.repositories.entities import EntityIdentity
from brainoid_api.repositories.memories import Memories, policy_call


class ReminderRequests:
    def __init__(self, access: Memories) -> None:
        self.access = access
        self.service = Reminders(access.store, now=access.now)

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        reminder: ResourceId,
        session: ResourceId | None = None,
    ) -> ReminderView:
        def read(tx: Transaction) -> tuple[ReminderView, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            return view(self.service.get(access, reminder)[0]), access

        def work() -> ReminderView:
            result, access = self.service.store.run(read)
            access.finish()
            return result

        return policy_call(work)

    def list(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        status: ReminderStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ReminderPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> tuple[ReminderPage, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            records = [
                r for r in self.service.candidates(access) if status is None or r.status == status
            ]
            current = AuthorizedScope(
                context=access.principal.context,
                scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                owner_user_id=access.principal.user_id,
                authorization_stamp=fingerprint(
                    [access.principal.stamp, [[r.id, r.version] for r in records]]
                ),
            )
            params: JsonValue = {
                "operation": "reminders.list",
                "tenant": tenant,
                "limit": limit,
                "status": status,
            }
            position = (
                ""
                if cursor is None
                else self.access.cursors.read(cursor, query=params, authorize=lambda: current)
            )
            remaining = [r for r in records if r.id > position]
            next_cursor = (
                self.access.cursors.issue(
                    position=remaining[limit - 1].id, query=params, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            access.finish()
            return ReminderPage(
                items=[view(r) for r in remaining[:limit]], next_cursor=next_cursor
            ), access

        def work() -> ReminderPage:
            result, access = self.service.store.run(read)
            access.finish()
            return result

        return policy_call(work)

    def create(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: ReminderCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> ReminderView:
        return policy_call(
            lambda: view(
                self.service.mutate(
                    lambda tx: self.access._open(tx, identity, tenant, session), body, key
                )
            )
        )

    def update(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        reminder: ResourceId,
        body: ReminderPatch,
        key: str | None,
        if_match: str | None,
        session: ResourceId | None = None,
    ) -> ReminderView:
        return policy_call(
            lambda: view(
                self.service.mutate(
                    lambda tx: self.access._open(tx, identity, tenant, session),
                    body,
                    key,
                    identity=reminder,
                    expected=require_if_match(if_match),
                )
            )
        )

    def snooze(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        reminder: ResourceId,
        body: ReminderSnooze,
        key: str | None,
        session: ResourceId | None = None,
    ) -> ReminderView:
        return policy_call(
            lambda: view(
                self.service.mutate(
                    lambda tx: self.access._open(tx, identity, tenant, session),
                    body.changes(),
                    key,
                    identity=reminder,
                    expected=body.expected_version,
                )
            )
        )
