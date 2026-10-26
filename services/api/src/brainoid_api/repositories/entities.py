"""Current entity CRUD and provider registration, atomic with common idempotency."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
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
from brainoid_memory import CanonicalSources, Entity, EntityPatch, MemoryRepository, VerifiedCaller
from brainoid_memory.entity_resolver import Inventory, external_key, register_external
from brainoid_memory.models import EntityInput, ExternalRef, Label
from brainoid_memory.repository import EntityTransaction, VersionConflict
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceSubject, Role, UserSubject
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field, JsonValue, TypeAdapter

from brainoid_api.auth.account_state import account_stamp
from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository, Input
from brainoid_api.repositories.tenants import Tenants

type EntityIdentity = AuthenticatedIdentity | DeviceIdentity


class EntityCreate(Scope):
    kind: Literal["person", "room", "appliance", "activity", "topic"]
    label: Label
    aliases: Annotated[tuple[Label, ...], Field(max_length=10)] = ()


class EntityView(EntityCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda value: value is None)
    external_refs: Annotated[tuple[ExternalRef, ...], Field(max_length=10)] = ()


class EntityPage(Input):
    items: Annotated[list[EntityView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class _Access(AuthorizedScope):
    valid_from: UtcTimestamp
    valid_to: UtcTimestamp

    def ensure_live(self, now: datetime) -> None:
        super().ensure_live(now)
        if not self.valid_from <= now < self.valid_to:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

    def binding(self) -> str:
        # A refreshed bearer changes deadlines, never resource ownership/grants.
        return AuthorizedScope.model_validate(
            {field: getattr(self, field) for field in AuthorizedScope.model_fields}
        ).binding()


class _Caller:
    def __init__(
        self,
        identity: EntityIdentity,
        tenants: Tenants,
        credentials: DeviceCredentialRepository | None,
        session: ResourceId | None,
    ) -> None:
        self.identity, self.tenants, self.credentials, self.session = (
            identity,
            tenants,
            credentials,
            session,
        )

    def authorize(self, tx: Transaction, context: TenantContext) -> VerifiedCaller:
        return self.verify(tx, context.tenant_id)

    def verify(self, tx: Transaction, tenant: ResourceId) -> VerifiedCaller:
        identity = self.identity
        if isinstance(identity, AuthenticatedIdentity):
            if self.session is not None:
                raise APIError(ErrorCode.INVALID_REQUEST)
            self.tenants.authorize_user(tx, identity)
            return VerifiedCaller(
                subject=UserSubject(user_id=identity.user_id),
                valid_from=identity.issued_at,
                valid_to=identity.expires_at,
            )
        if self.credentials is None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.credentials.authorize(tx, identity)
        if identity.tenant_id != tenant:
            raise PolicyDenied()
        family = tx.get("device_credential_families/" + identity.family_id)
        if family is None:
            raise PolicyDenied()
        expiry = TypeAdapter(UtcTimestamp).validate_python(family["expires_at"])
        return VerifiedCaller(
            subject=DeviceSubject(
                device_id=identity.sub,
                connection_generation=identity.connection_generation,
                private_session_id=self.session,
            ),
            valid_from=datetime.fromtimestamp(identity.iat, UTC),
            valid_to=min(datetime.fromtimestamp(identity.exp, UTC), expiry),
        )


class Entities:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        cursors: Cursors,
        *,
        ledger: ControlLedger,
        credentials: DeviceCredentialRepository | None = None,
        inventory: Inventory | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        allow_untracked_bootstrap: bool = False,
    ) -> None:
        self._store, self._tenants, self._cursors = store, tenants, cursors
        self._ledger, self._credentials, self._inventory = ledger, credentials, inventory
        self._now, self._untracked = now, allow_untracked_bootstrap
        self._idempotency = Idempotency(store, now=now)

    def _open(
        self,
        tx: Transaction,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
    ) -> EntityTransaction:
        caller = _Caller(identity, self._tenants, self._credentials, session)
        verified = caller.verify(tx, tenant)
        principal = transaction_policy(
            tx, now=self._now, ledger=self._ledger, allow_untracked_bootstrap=self._untracked
        ).principal(tenant, verified.subject)
        memory = MemoryRepository(
            self._store,
            authorizer=caller,
            sources=CanonicalSources(),
            ledger=self._ledger,
            now=self._now,
            allow_untracked_bootstrap=self._untracked,
        )
        return memory.entities(tx, principal.context)

    def _scope(
        self,
        tx: Transaction,
        access: EntityTransaction,
        scope: Scope,
        *,
        stamp: JsonValue = None,
    ) -> _Access:
        principal = access.principal
        account = None if principal.user_id is None else account_stamp(tx, principal.user_id)
        result = _Access(
            context=principal.context,
            scope=scope,
            owner_user_id=principal.user_id,
            authorization_stamp=fingerprint([principal.stamp, account, stamp]),
            valid_from=access.caller.valid_from,
            valid_to=access.caller.valid_to,
        )
        access.finish()
        return result

    @staticmethod
    def _view(record: Entity) -> EntityView:
        return EntityView.model_validate(
            {field: getattr(record, field) for field in EntityView.model_fields}
        )

    def get(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        entity: ResourceId,
        session: ResourceId | None = None,
    ) -> EntityView:
        def read(tx: Transaction) -> tuple[EntityView, EntityTransaction]:
            access = self._open(tx, identity, tenant, session)
            result = self._view(access.get(entity))
            access.finish()
            return result, access

        def run() -> EntityView:
            result, access = self._store.run(read)
            access.finish()
            return result

        return policy_call(run)

    def list(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        query: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> EntityPage:
        limit = page_limit(limit)
        if query is not None and (not isinstance(query, str) or len(query) > 200):
            raise APIError(ErrorCode.INVALID_REQUEST)

        def read(tx: Transaction) -> tuple[EntityPage, EntityTransaction]:
            access = self._open(tx, identity, tenant, session)
            records = access.candidates(query)
            current = self._scope(
                tx,
                access,
                Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                stamp=[[r.id, r.version, r.deletion_epoch] for r in records],
            )
            params: JsonValue = {
                "operation": "entities.list",
                "tenant": tenant,
                "query": query,
                "limit": limit,
            }
            position = (
                ""
                if cursor is None
                else self._cursors.read(cursor, query=params, authorize=lambda: current)
            )
            remaining = [r for r in records if r.id > position]
            next_cursor = (
                self._cursors.issue(
                    position=remaining[limit - 1].id, query=params, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            access.finish()
            return EntityPage(
                items=[self._view(r) for r in remaining[:limit]], next_cursor=next_cursor
            ), access

        def run() -> EntityPage:
            result, access = self._store.run(read)
            access.finish()
            return result

        return policy_call(run)

    def _mutate(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
        *,
        operation: str,
        body: JsonValue,
        key: str | None,
        scope: Scope,
        target: ResourceId | None,
        mutate: Callable[[Transaction, EntityTransaction], Entity],
        prepare: Callable[[Transaction, EntityTransaction, Entity | None], Scope] | None = None,
    ) -> EntityView:
        access: EntityTransaction | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            nonlocal access
            access = self._open(tx, identity, tenant, session)
            if access.principal.user_id is None or (
                "memory.write" not in access.principal.context.actor.allowed_scopes
            ):
                raise PolicyDenied(hidden=False)
            resource = target if replay is None else replay.resource_id
            if replay is not None and (resource is None or (target and resource != target)):
                raise PolicyDenied()
            record = None if resource is None else access.get(resource, write=True)
            if record is not None:
                actual = Scope(visibility=record.visibility, display_policy=record.display_policy)
            else:
                actual = scope
            if prepare is not None:
                actual = prepare(tx, access, record)
            return self._scope(
                tx, access, actual, stamp=0 if record is None else record.deletion_epoch
            )

        def write(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            assert access is not None
            result = mutate(tx, access)
            access.finish()
            return MutationResult(
                status_code=201 if target is None else 200,
                resource_id=result.id,
                version=result.version,
            )

        def run() -> EntityView:
            try:
                result = self._idempotency.execute(
                    operation=operation, body=body, key=key, authorize=authorize, mutate=write
                )
            except VersionConflict:
                raise APIError(ErrorCode.VERSION_CONFLICT) from None
            assert access is not None and result.resource_id is not None
            access.finish()
            return self.get(identity, tenant, result.resource_id, session)

        return policy_call(run)

    def create(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        body: EntityCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> EntityView:
        values = EntityInput.model_validate(body.model_dump(mode="python"))
        entity = ResourceId(str(uuid4()))
        return self._mutate(
            identity,
            tenant,
            session,
            operation="entities.create",
            body=body.model_dump(mode="json"),
            key=key,
            target=None,
            scope=Scope(visibility=body.visibility, display_policy=body.display_policy),
            mutate=lambda tx, access: access.create(entity, values),
        )

    def update(
        self,
        identity: EntityIdentity,
        tenant: ResourceId,
        entity: ResourceId,
        body: EntityPatch,
        key: str | None,
        if_match: str | None,
        session: ResourceId | None = None,
    ) -> EntityView:
        expected = require_if_match(if_match)

        def update(tx: Transaction, access: EntityTransaction) -> Entity:
            record = access.get(entity, write=True)
            return access.update(
                entity,
                body,
                expected_version=expected,
                expected_deletion_epoch=record.deletion_epoch,
            )

        return self._mutate(
            identity,
            tenant,
            session,
            operation=f"entities.update:{entity}",
            body={"patch": body.model_dump(mode="json", exclude_none=True), "version": expected},
            key=key,
            target=entity,
            scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
            mutate=update,
        )

    def register(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        reference: ExternalRef,
        key: str | None,
    ) -> EntityView:
        """Internal trusted inventory entrypoint; deliberately has no HTTP route."""
        reference = ExternalRef.model_validate(reference.model_dump(mode="python"))
        if self._inventory is None:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
        inventory = self._inventory
        entity = ResourceId(str(uuid4()))

        def prepare(tx: Transaction, access: EntityTransaction, record: Entity | None) -> Scope:
            integration = tx.get(f"tenants/{tenant}/integrations/{reference.integration_id}")
            if (
                integration is None
                or integration.get("id") != reference.integration_id
                or integration.get("tenant_id") != tenant
                or integration.get("provider") != reference.provider
                or integration.get("status") != "active"
                or integration.get("owner_user_id") not in (None, access.principal.user_id)
                or access.principal.role not in (Role.OWNER, Role.ADMIN)
            ):
                raise PolicyDenied()
            values = inventory.resolve(tx, access.principal.context, reference)
            if values.kind != "appliance" or values.external_refs != (reference,):
                raise PolicyDenied()
            if record is not None and (
                record.external_refs != (reference,)
                or record.kind != "appliance"
                or record.deletion_epoch != 0
                or record.suppress_reuse
                or (record.visibility, record.display_policy)
                != (values.visibility, values.display_policy)
            ):
                raise PolicyDenied()
            if record is not None:
                index = tx.get(
                    f"tenants/{tenant}/entity_external_ids/{external_key(tenant, reference)}"
                )
                if index != {
                    "entity_id": record.id,
                    "deletion_epoch": record.deletion_epoch,
                    "reference": reference.model_dump(mode="json"),
                }:
                    raise PolicyDenied()
            access.finish()
            return Scope(visibility=values.visibility, display_policy=values.display_policy)

        return self._mutate(
            identity,
            tenant,
            None,
            operation="entities.register",
            body=reference.model_dump(mode="json"),
            key=key,
            target=None,
            scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
            prepare=prepare,
            mutate=lambda tx, access: register_external(tx, access, entity, reference, inventory),
        )
