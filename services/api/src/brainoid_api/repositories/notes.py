"""HTTP identity and pagination adapter for the common note service."""

from brainoid_contracts import DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.transactions import Transaction
from brainoid_memory.notes import NoteCreate, NotePage, NotePatch, Notes, NoteView, view
from brainoid_memory.repository import FactTransaction
from pydantic import JsonValue

from brainoid_api.common.pagination import page_limit
from brainoid_api.common.versioning import require_if_match
from brainoid_api.repositories.entities import EntityIdentity
from brainoid_api.repositories.memories import Memories, policy_call


class NoteRequests:
    def __init__(self, access: Memories) -> None:
        self.access = access
        self.service = Notes(access.store, now=access.now)

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        note: ResourceId,
        session: ResourceId | None = None,
    ) -> NoteView:
        def read(tx: Transaction) -> tuple[NoteView, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            return view(self.service.get(access, note)[0]), access

        def work() -> NoteView:
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
        query: str = "",
        limit: int = 20,
        cursor: str | None = None,
    ) -> NotePage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> tuple[NotePage, FactTransaction]:
            access = self.access._open(tx, identity, tenant, session)
            records = self.service.candidates(access, query=query)
            current = AuthorizedScope(
                context=access.principal.context,
                scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                owner_user_id=access.principal.user_id,
                authorization_stamp=fingerprint(
                    [access.principal.stamp, [[r.id, r.version] for r in records]]
                ),
            )
            params: JsonValue = {
                "operation": "notes.list",
                "tenant": tenant,
                "query": query,
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
            return NotePage(
                items=[view(r) for r in remaining[:limit]], next_cursor=next_cursor
            ), access

        def work() -> NotePage:
            result, access = self.service.store.run(read)
            access.finish()
            return result

        return policy_call(work)

    def create(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: NoteCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> NoteView:
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
        note: ResourceId,
        body: NotePatch,
        key: str | None,
        if_match: str | None,
        session: ResourceId | None = None,
    ) -> NoteView:
        return policy_call(
            lambda: view(
                self.service.mutate(
                    lambda tx: self.access._open(tx, identity, tenant, session),
                    body,
                    key,
                    identity=note,
                    expected=require_if_match(if_match),
                )
            )
        )
