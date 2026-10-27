"""Current recipient-authorized inbox and idempotent receipt mutations."""

from brainoid_contracts import DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope
from brainoid_contracts.idempotency import MutationResult
from brainoid_contracts.transactions import Transaction
from brainoid_memory.notifications import (
    NotificationAck,
    NotificationPage,
    NotificationView,
    acknowledge,
    public_notification,
)
from brainoid_memory.repository import FactTransaction
from brainoid_policy.scopes import PolicyDenied
from pydantic import JsonValue, TypeAdapter

from brainoid_api.repositories.entities import EntityIdentity
from brainoid_api.repositories.memories import Memories, policy_call


class NotificationRequests:
    def __init__(self, access: Memories) -> None:
        self.access = access

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        notification: ResourceId,
        session: ResourceId | None = None,
    ) -> NotificationView:
        def read(tx: Transaction) -> tuple[NotificationView, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            return NotificationView.model_validate(
                public_notification(access, notification)
            ), access

        def work() -> NotificationView:
            result, access = self.access.store.run(read)
            access.finish()
            return result

        return policy_call(work)

    def list(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> NotificationPage:
        def read(tx: Transaction) -> tuple[NotificationPage, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            records = []
            for path, _ in tx.collection(f"tenants/{tenant}/notifications"):
                try:
                    item = TypeAdapter(ResourceId).validate_python(path.rsplit("/", 1)[-1])
                    records.append(
                        NotificationView.model_validate(public_notification(access, item))
                    )
                except PolicyDenied:
                    continue
            records.sort(key=lambda r: r.id)
            current = self.access._scope(
                tx,
                access,
                Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                stamp=[r.model_dump(mode="json") for r in records],
            )
            params: JsonValue = {
                "operation": "notifications.list",
                "tenant": tenant,
                "limit": limit,
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
            return NotificationPage(items=remaining[:limit], next_cursor=next_cursor), access

        def work() -> NotificationPage:
            result, access = self.access.store.run(read)
            access.finish()
            return result

        return policy_call(work)

    def ack(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        notification: ResourceId,
        body: NotificationAck,
        key: str | None,
        session: ResourceId | None = None,
    ) -> NotificationView:
        access: FactTransaction | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access
            access = self.access._open(tx, identity, tenant, session)
            record = public_notification(access, notification)
            return self.access._scope(
                tx,
                access,
                Scope(visibility=record["visibility"], display_policy=record["display_policy"]),
                stamp=notification,
            )

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            acknowledge(access, notification, body.action)
            return MutationResult(status_code=200, resource_id=notification)

        policy_call(
            lambda: self.access.idempotency.execute(
                operation=f"notifications.ack:{notification}",
                key=key,
                body=body.model_dump(mode="json"),
                authorize=authorize,
                mutate=mutate,
            )
        )
        return self.get(identity, tenant, notification, session)
