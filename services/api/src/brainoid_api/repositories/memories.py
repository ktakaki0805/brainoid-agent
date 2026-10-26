"""Explicit memory CRUD, atomic supersession and dormant indexing outbox."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import uuid4

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_jobs import Jobs
from brainoid_memory import CanonicalSources, MemoryRepository
from brainoid_memory.models import ClaimInput, Fact, FactInput, MemoryValue, SourceRef, Value
from brainoid_memory.repository import FactTransaction, VersionConflict
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from pydantic import Field, JsonValue, field_validator, model_validator

from brainoid_api.auth.account_state import account_stamp
from brainoid_api.auth.authorization import transaction_policy
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository
from brainoid_api.repositories.entities import EntityIdentity, _Access, _Caller
from brainoid_api.repositories.memory_sources import SavedMessages
from brainoid_api.repositories.tenants import Tenants


class MemoryCreate(ClaimInput):
    source_refs: Annotated[tuple[SourceRef, ...], Field(max_length=20)] = ()
    valid_from: UtcTimestamp | None = None
    valid_to: UtcTimestamp | None = None

    @field_validator("valid_from", mode="before")
    @classmethod
    def nonnull_start(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied valid_from cannot be null")
        return value

    @model_validator(mode="after")
    def interval_and_sources(self) -> Self:
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_to <= self.valid_from:
                raise ValueError("valid_to must follow valid_from")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Duplicate sources")
        return self

    def fact(self, now: datetime) -> FactInput:
        return FactInput.model_validate(
            {
                **self.model_dump(exclude_none=True),
                "valid_from": self.valid_from or now,
                "origin": "user_explicit",
                "status": "confirmed",
            }
        )


class MemoryRevision(MemoryValue):
    replacement: MemoryCreate
    reason: Annotated[str, Field(strict=True, max_length=500)] | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def nonnull_reason(cls, value: object) -> object:
        if value is None:
            raise ValueError("A supplied reason cannot be null")
        return value


class MemoryView(FactInput):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    recorded_at: UtcTimestamp
    created_by: FirebaseUid
    deletion_epoch: Annotated[int, Field(strict=True, ge=0)]
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)
    supersedes_fact_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    object_entity_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    value: Value | None = Field(default=None, exclude_if=lambda v: v is None)
    valid_to: UtcTimestamp | None = Field(default=None, exclude_if=lambda v: v is None)


class MemoryPage(MemoryValue):
    items: Annotated[list[MemoryView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


def policy_call[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except PolicyUnavailable:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    except PolicyDenied as error:
        raise APIError(ErrorCode(error.code)) from None
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
    ) -> None:
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
        memory = ResourceId(str(uuid4()))
        access: FactTransaction | None = None
        epoch = 0

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access, epoch
            access = self._open(tx, identity, tenant, session)
            if (
                access.principal.user_id is None
                or "memory.write" not in access.principal.context.actor.allowed_scopes
            ):
                raise PolicyDenied(hidden=False)
            # A revision receipt identifies the NEW record; always check both sides.
            old = None if predecessor is None else access.get(predecessor, write=True)
            epoch = 0 if old is None else old.deletion_epoch
            record = old
            if replay is not None:
                if replay.resource_id is None:
                    raise PolicyDenied()
                record = access.get(replay.resource_id, write=True)
                if record.supersedes_fact_id != predecessor:
                    raise PolicyDenied()
                if record.deletion_epoch != 0:
                    raise PolicyDenied(hidden=False)
            actual = Scope(visibility=body.visibility, display_policy=body.display_policy)
            if record is not None:
                actual = Scope(visibility=record.visibility, display_policy=record.display_policy)
            return self._scope(tx, access, actual, stamp=epoch)

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            # A missing valid_from defaults to the committing attempt's server time.
            # Request validation already checks supplied intervals; validate the
            # implicit start too, without turning invalid business input into a 500.
            recorded = self.now()
            if body.valid_from is None and body.valid_to is not None and body.valid_to <= recorded:
                raise APIError(ErrorCode.INVALID_REQUEST)
            change = access.prepare(
                memory,
                body.fact(recorded),
                predecessor=predecessor,
                expected_version=expected,
                expected_deletion_epoch=epoch,
            )
            self.jobs.add_created_memory(
                tx,
                grant=change.grant,
                source=change.document,
                before_write=access.finish,
            )
            access.save(change, source_already_created=True)
            access.finish()
            return MutationResult(
                status_code=201 if predecessor is None else 200, resource_id=memory, version=1
            )

        def run() -> MemoryView:
            result = self.idempotency.execute(
                operation="memories.create"
                if predecessor is None
                else f"memories.revise:{predecessor}",
                key=key,
                body={
                    "replacement": body.model_dump(mode="json", exclude_none=True),
                    "version": expected,
                    "reason": reason,
                },
                authorize=authorize,
                mutate=mutate,
            )
            assert access is not None and result.resource_id is not None
            access.finish()
            return self.get(identity, tenant, result.resource_id, session)

        return policy_call(run)

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
