"""Explicit memory CRUD, atomic supersession and dormant indexing outbox."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from brainoid_contracts import (
    DisplayPolicy,
    ResourceId,
    Scope,
    Version,
    Visibility,
)
from brainoid_jobs import Jobs
from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.dto import MemoryCreate as MemoryCreate
from brainoid_memory.dto import MemoryPage as MemoryPage
from brainoid_memory.dto import MemoryRevision as MemoryRevision
from brainoid_memory.dto import MemoryView as MemoryView
from brainoid_memory.forget import DeleteNote, DeleteTask, ForgetCreate, ForgetMemories, Operation
from brainoid_memory.models import Fact
from brainoid_memory.mutations import MemoryMutations
from brainoid_memory.repository import FactTransaction, VersionConflict
from brainoid_memory.retrieval import SearchInput, retrieve
from brainoid_memory.vector_store import VectorConfigurationError, VectorSearch, VectorUnavailable
from brainoid_policy.control_ledger import ControlLedger, LedgerError
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from pydantic import JsonValue

from brainoid_api.auth.account_state import account_stamp
from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.common.access import fingerprint
from brainoid_api.common.idempotency import Idempotency
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository
from brainoid_api.repositories.entities import EntityIdentity, _Access, _Caller
from brainoid_api.repositories.memory_sources import SavedMessages
from brainoid_api.repositories.tenants import Tenants


def policy_call[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except (PolicyUnavailable, LedgerError):
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    except PolicyDenied as error:
        raise APIError(ErrorCode(error.code)) from None
    except (VectorUnavailable, VectorConfigurationError):
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    except VersionConflict:
        raise APIError(ErrorCode.VERSION_CONFLICT) from None


class Memories:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        cursors: Cursors,
        *,
        ledger: ControlLedger,
        credentials: DeviceCredentialRepository | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        allow_untracked_bootstrap: bool = False,
        vector_search: VectorSearch | None = None,
        deletion_ledger: DeletionLedger | None = None,
    ) -> None:
        self.deletion_ledger = deletion_ledger
        self.vector_search = vector_search
        self.store, self.tenants, self.cursors = store, tenants, cursors
        self.ledger, self.credentials, self.now = ledger, credentials, now
        self.untracked = allow_untracked_bootstrap
        self.idempotency = Idempotency(store, now=now)
        self.jobs = Jobs(
            store, ledger, now=now, allow_untracked_bootstrap=allow_untracked_bootstrap
        )

    def _open(
        self,
        tx: Transaction,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
    ) -> FactTransaction:
        caller = _Caller(identity, self.tenants, self.credentials, session)
        verified = caller.verify(tx, tenant)
        principal = transaction_policy(
            tx, now=self.now, ledger=self.ledger, allow_untracked_bootstrap=self.untracked
        ).principal(tenant, verified.subject)
        repository = MemoryRepository(
            self.store,
            authorizer=caller,
            sources=CanonicalSources(SavedMessages()),
            ledger=self.ledger,
            now=self.now,
            allow_untracked_bootstrap=self.untracked,
        )
        return repository.facts(tx, principal.context)

    def _scope(
        self,
        tx: Transaction,
        access: FactTransaction,
        scope: Scope,
        *,
        stamp: JsonValue,
    ) -> _Access:
        principal = access.principal
        account = None if principal.user_id is None else account_stamp(tx, principal.user_id)
        current = _Access(
            context=principal.context,
            scope=scope,
            owner_user_id=principal.user_id,
            authorization_stamp=fingerprint([principal.stamp, account, stamp]),
            valid_from=access.caller.valid_from,
            valid_to=access.caller.valid_to,
        )
        access.finish()
        return current

    @staticmethod
    def _view(record: Fact) -> MemoryView:
        return MemoryView.model_validate(
            {
                key: value
                for key, value in record.model_dump(exclude_none=True).items()
                if key in MemoryView.model_fields
            }
        )

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        memory: ResourceId,
        session: ResourceId | None = None,
    ) -> MemoryView:
        def read(tx: Transaction) -> tuple[MemoryView, FactTransaction]:
            access = self._open(tx, identity, tenant, session)
            result = self._view(access.get(memory))
            access.finish()
            return result, access

        def run() -> MemoryView:
            result, access = self.store.run(read)
            access.finish()
            return result

        return policy_call(run)

    def list(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> MemoryPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> tuple[MemoryPage, FactTransaction]:
            access = self._open(tx, identity, tenant, session)
            records = access.candidates()
            current = self._scope(
                tx,
                access,
                Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                stamp=[[r.id, r.version, r.deletion_epoch] for r in records],
            )
            params: JsonValue = {"operation": "memories.list", "tenant": tenant, "limit": limit}
            position = (
                ""
                if cursor is None
                else self.cursors.read(
                    cursor,
                    query=params,
                    authorize=lambda: current,
                )
            )
            remaining = [r for r in records if r.id > position]
            next_cursor = (
                self.cursors.issue(
                    position=remaining[limit - 1].id,
                    query=params,
                    authorize=lambda: current,
                )
                if len(remaining) > limit
                else None
            )
            access.finish()
            return MemoryPage(
                items=[self._view(r) for r in remaining[:limit]], next_cursor=next_cursor
            ), access

        def run() -> MemoryPage:
            result, access = self.store.run(read)
            access.finish()
            return result

        return policy_call(run)

    def search(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: SearchInput,
        key: str | None,
        session: ResourceId | None = None,
    ) -> MemoryPage:
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)

        provider = None

        def read(tx: Transaction) -> tuple[MemoryPage, FactTransaction, tuple[Fact, ...]]:
            access = self._open(tx, identity, tenant, session)
            found = retrieve(access, access.entities(), body, provider=provider)
            logging.getLogger("brainoid_memory").info("memory_search %s", found.trace.model_dump())
            return (
                MemoryPage(items=[self._view(r) for r in found.records], next_cursor=None),
                access,
                found.records,
            )

        def run() -> MemoryPage:
            nonlocal provider
            if self.vector_search is not None:
                provider = self.vector_search.prepare(
                    self.store, lambda tx: self._open(tx, identity, tenant, session), body
                )
            page, access, records = self.store.run(read)
            access.finish_records(records)
            return page

        return policy_call(run)

    def _mutate(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: MemoryCreate,
        key: str | None,
        session: ResourceId | None,
        *,
        predecessor: ResourceId | None = None,
        expected: Version | None = None,
        reason: str | None = None,
    ) -> MemoryView:
        return policy_call(
            lambda: self._view(
                MemoryMutations(self.store, self.jobs, now=self.now).mutate(
                    lambda tx: self._open(tx, identity, tenant, session),
                    body,
                    key,
                    predecessor=predecessor,
                    expected=expected,
                    reason=reason,
                )
            )
        )

    def create(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: MemoryCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> MemoryView:
        return self._mutate(identity, tenant, body, key, session)

    def revise(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        memory: ResourceId,
        body: MemoryRevision,
        key: str | None,
        if_match: str | None,
        session: ResourceId | None = None,
    ) -> MemoryView:
        return self._mutate(
            identity,
            tenant,
            body.replacement,
            key,
            session,
            predecessor=memory,
            expected=require_if_match(if_match),
            reason=body.reason,
        )

    def forgetting(self) -> ForgetMemories:
        if self.deletion_ledger is None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        return ForgetMemories(self.store, self.deletion_ledger, now=self.now)

    def forget(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: ForgetCreate | DeleteNote | DeleteTask,
        key: str | None,
    ) -> Operation:
        return policy_call(
            lambda: self.forgetting().forget(
                lambda tx: self._open(tx, identity, tenant, None),
                body,
                key,
            )
        )

    def operation(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        operation: ResourceId,
    ) -> Operation:
        return policy_call(
            lambda: self.forgetting().get(
                lambda tx: self._open(tx, identity, tenant, None),
                operation,
            )
        )
