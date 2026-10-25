"""Atomic tenant bootstrap and current-membership reads and settings updates."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from brainoid_contracts import (
    DisplayPolicy,
    IanaTimezone,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import ManagementAction, Role
from brainoid_policy.scopes import Principal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from brainoid_api.auth.authorization import policy_call, transaction_policy, user_subject
from brainoid_api.auth.context import AuthenticatedIdentity, select_tenant
from brainoid_api.auth.profiles import UserProfile
from brainoid_api.common.access import AuthorizedScope, AuthorizedUserScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.common.versioning import compare_and_write, require_if_match
from brainoid_api.errors import APIError, ErrorCode

Name = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
_resource_id = TypeAdapter(ResourceId)


class TenantCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    name: Name
    timezone: IanaTimezone = IanaTimezone("Asia/Tokyo")


class TenantPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    name: Name | None = None
    timezone: IanaTimezone | None = None

    @model_validator(mode="after")
    def nonempty_nonnull(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null setting")
        return self


class Tenant(TenantCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    status: Literal["active", "deleting"]
    my_role: Role


class TenantPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: Annotated[list[Tenant], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class Tenants:
    def __init__(
        self,
        store: TransactionStore,
        cursors: Cursors,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        ledger: ControlLedger | None = None,
    ) -> None:
        self._store = store
        self._cursors = cursors
        self._now = now
        self._ledger = ledger
        self._idempotency = Idempotency(store, now=now)

    def authorize_user(
        self, tx: Transaction, identity: AuthenticatedIdentity
    ) -> AuthorizedUserScope:
        scope = AuthorizedUserScope(
            user_id=identity.user_id,
            authorization_stamp=fingerprint(["unprovisioned", identity.user_id]),
            issued_at=identity.issued_at,
            expires_at=identity.expires_at,
        )
        scope.ensure_live(self._now())
        documents = tx.find("users", "user_id", identity.user_id)
        if len(documents) > 1:
            raise APIError(ErrorCode.INTERNAL_ERROR)
        if documents:
            document = documents[0]
            try:
                profile = UserProfile.model_validate(
                    {field: document.get(field) for field in UserProfile.model_fields}
                )
            except ValidationError:
                raise APIError(ErrorCode.INTERNAL_ERROR) from None
            if profile.user_id != identity.user_id or profile.status != "active":
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            scope = scope.model_copy(
                update={
                    "authorization_stamp": fingerprint(
                        [
                            profile.user_id,
                            profile.status,
                            profile.version,
                            document.get("deletion_epoch", 0),
                        ]
                    )
                }
            )
        return scope

    def _principal(
        self,
        tx: Transaction,
        identity: AuthenticatedIdentity,
        tenant_id: ResourceId,
        *,
        manage: bool = False,
    ) -> Principal:
        subject = user_subject(select_tenant(identity, tenant_id), now=self._now())
        policy = transaction_policy(
            tx, now=self._now, ledger=self._ledger, allow_untracked_bootstrap=True
        )
        return policy_call(
            lambda: (
                policy.management(tenant_id, subject, ManagementAction.UPDATE_TENANT)
                if manage
                else policy.principal(tenant_id, subject)
            )
        )

    def _project(self, tx: Transaction, principal: Principal) -> Tenant:
        tenant_id = principal.context.tenant_id
        document = tx.get(f"tenants/{tenant_id}")
        if document is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        try:
            result = Tenant.model_validate(
                {
                    **{
                        field: document.get(field)
                        for field in Tenant.model_fields
                        if field != "my_role"
                    },
                    "my_role": principal.role,
                }
            )
        except ValidationError:
            raise APIError(ErrorCode.INTERNAL_ERROR) from None
        if result.id != tenant_id or result.tenant_id != tenant_id:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return result

    def get(self, identity: AuthenticatedIdentity, tenant_id: ResourceId) -> Tenant:
        def read(tx: Transaction) -> Tenant:
            self.authorize_user(tx, identity)
            return self._project(tx, self._principal(tx, identity, tenant_id))

        return self._store.run(read)

    def create(
        self, identity: AuthenticatedIdentity, body: TenantCreate, key: str | None
    ) -> Tenant:
        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedUserScope:
            current = self.authorize_user(tx, identity)
            if replay is not None:
                if replay.resource_id is None:
                    raise APIError(ErrorCode.INTERNAL_ERROR)
                principal = self._principal(tx, identity, replay.resource_id)
                if principal.role != Role.OWNER:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return current

        def mutate(tx: Transaction, current: AuthorizedUserScope) -> MutationResult:
            tenant_id, agent_id = ResourceId(str(uuid4())), ResourceId(str(uuid4()))
            timestamp = self._now()
            metadata = {
                "tenant_id": tenant_id,
                "created_by": current.user_id,
                "created_at": timestamp,
                "updated_at": timestamp,
                "version": 1,
                "schema_version": 1,
                "deletion_epoch": 0,
            }
            tx.create(
                f"tenants/{tenant_id}",
                {
                    **metadata,
                    **body.model_dump(mode="json"),
                    "id": tenant_id,
                    "status": "active",
                    "policy_version": 1,
                },
            )
            member_key = fingerprint(["user", current.user_id])
            tx.create(
                f"tenants/{tenant_id}/memberships/{member_key}",
                {
                    **metadata,
                    "user_id": current.user_id,
                    "role": "owner",
                    "status": "active",
                    "authorization_version": 1,
                },
            )
            # Canonical initial configuration. RUN-03 will consume/edit the saved agent.
            tx.create(
                f"tenants/{tenant_id}/agents/{agent_id}",
                {
                    **metadata,
                    "id": agent_id,
                    "name": "Brainoid",
                    "persona": "",
                    "allowed_tools": [],
                    "prompt_version": 1,
                },
            )
            return MutationResult(status_code=201, resource_id=tenant_id, version=1)

        result = self._idempotency.execute_user(
            operation="tenants.create",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=mutate,
        )
        assert result.resource_id is not None
        return self.get(identity, result.resource_id)

    def update(
        self,
        identity: AuthenticatedIdentity,
        tenant_id: ResourceId,
        body: TenantPatch,
        key: str | None,
        if_match: str | None,
    ) -> Tenant:
        expected = require_if_match(if_match)

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            user = self.authorize_user(tx, identity)
            principal = self._principal(tx, identity, tenant_id, manage=True)
            if replay is not None and replay.resource_id != tenant_id:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return AuthorizedScope(
                context=principal.context,
                scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                owner_user_id=None,
                authorization_stamp=fingerprint([principal.stamp, user.authorization_stamp]),
            )

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            path = f"tenants/{tenant_id}"
            document = tx.get(path)
            if document is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            version = compare_and_write(
                tx,
                authorized_scope=current,
                path=path,
                if_match=if_match,
                replacement={
                    **document,
                    **body.model_dump(mode="json", exclude_unset=True),
                    "updated_at": self._now(),
                },
            )
            return MutationResult(status_code=200, resource_id=tenant_id, version=version)

        self._idempotency.execute(
            operation=f"tenants.update:{tenant_id}",
            key=key,
            body={"patch": body.model_dump(mode="json", exclude_unset=True), "version": expected},
            authorize=authorize,
            mutate=mutate,
        )
        return self.get(identity, tenant_id)

    def list(
        self,
        identity: AuthenticatedIdentity,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> TenantPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> TenantPage:
            user = self.authorize_user(tx, identity)
            candidates: set[ResourceId] = set()
            for path, document in tx.memberships(identity.user_id):
                parts = path.split("/")
                if len(parts) != 4 or parts[0] != "tenants" or parts[2] != "memberships":
                    continue
                try:
                    tenant_id = _resource_id.validate_python(parts[1])
                except ValidationError:
                    continue
                if document.get("tenant_id") == tenant_id:
                    candidates.add(tenant_id)
            principals = []
            for tenant_id in sorted(candidates):
                try:
                    principals.append(self._principal(tx, identity, tenant_id))
                except APIError as error:
                    if error.code != ErrorCode.RESOURCE_NOT_FOUND:
                        raise
            # Bind the whole current membership set, including pages already returned.
            binding = fingerprint(
                [
                    user.authorization_stamp,
                    [[p.context.tenant_id, p.stamp] for p in principals],
                ]
            )
            current = user.model_copy(update={"authorization_stamp": binding})
            query: JsonValue = {"operation": "tenants.list", "order": "tenant_id", "limit": limit}
            position = (
                ""
                if cursor is None
                else self._cursors.read(
                    cursor,
                    query=query,
                    authorize=lambda: current,
                )
            )
            remaining = [p for p in principals if p.context.tenant_id > position]
            items = [self._project(tx, p) for p in remaining[:limit]]
            next_cursor = None
            if len(remaining) > limit:
                next_cursor = self._cursors.issue(
                    position=items[-1].id,
                    query=query,
                    authorize=lambda: current,
                )
            current.ensure_live(self._now())
            return TenantPage(items=items, next_cursor=next_cursor)

        return self._store.run(read)
