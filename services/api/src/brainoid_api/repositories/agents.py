"""Shared agent reads and owner/admin compare-and-swap personality updates."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Self

from brainoid_agent_core.definition import AgentDefinition
from brainoid_contracts import DisplayPolicy, ResourceId, Scope, Visibility
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceSubject, ManagementAction, UserSubject
from pydantic import Field, JsonValue, ValidationError, model_validator

from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.common.versioning import compare_and_write, require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository, Input
from brainoid_api.repositories.tenants import Name, Tenants

type AgentIdentity = AuthenticatedIdentity | DeviceIdentity


class AgentPatch(Input):
    name: Name | None = None
    persona: Annotated[str, Field(strict=True, max_length=4000)] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, f) is None for f in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null setting")
        return self


class AgentPage(Input):
    items: Annotated[list[AgentDefinition], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class Agents:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        cursors: Cursors,
        *,
        ledger: ControlLedger | None = None,
        credentials: DeviceCredentialRepository | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store, self._tenants, self._cursors = store, tenants, cursors
        self._ledger, self._credentials, self._now = ledger, credentials, now
        self._idempotency = Idempotency(store, now=now)

    def _authorize(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        manage: bool = False,
    ) -> AuthorizedScope:
        policy = transaction_policy(
            tx, now=self._now, ledger=self._ledger, allow_untracked_bootstrap=True
        )
        account = None
        if isinstance(identity, AuthenticatedIdentity):
            if session is not None:
                raise APIError(ErrorCode.INVALID_REQUEST)
            account = self._tenants.authorize_user(tx, identity).authorization_stamp
            subject = UserSubject(user_id=identity.user_id)
            principal = policy_call(
                lambda: (
                    policy.management(tenant, subject, ManagementAction.MANAGE_AGENTS)
                    if manage
                    else policy.principal(tenant, subject)
                )
            )
        else:
            if manage:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if self._credentials is None:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            self._credentials.authorize(tx, identity)
            if identity.tenant_id != tenant:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            principal = policy_call(
                lambda: policy.principal(
                    tenant,
                    DeviceSubject(
                        device_id=identity.sub,
                        connection_generation=identity.connection_generation,
                        private_session_id=session,
                    ),
                )
            )
        return AuthorizedScope(
            context=principal.context,
            scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
            owner_user_id=None,
            authorization_stamp=fingerprint([principal.stamp, account]),
        )

    def _document(self, tx: Transaction, tenant: ResourceId, agent: ResourceId) -> Document:
        doc = tx.get(f"tenants/{tenant}/agents/{agent}")
        if doc is None or doc.get("id") != agent or doc.get("tenant_id") != tenant:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return doc

    def _view(self, doc: Document) -> AgentDefinition:
        try:
            return AgentDefinition.model_validate(
                {f: doc.get(f) for f in AgentDefinition.model_fields}
            )
        except ValidationError:
            raise APIError(ErrorCode.INTERNAL_ERROR) from None

    def get(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        agent: ResourceId,
        session: ResourceId | None = None,
    ) -> AgentDefinition:
        def read(tx: Transaction) -> AgentDefinition:
            current = self._authorize(tx, identity, tenant, session)
            result = self._view(self._document(tx, tenant, agent))
            current.ensure_live(self._now())
            return result

        return self._store.run(read)

    def list(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AgentPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> AgentPage:
            current = self._authorize(tx, identity, tenant, session)
            agents = []
            for path, doc in tx.collection(f"tenants/{tenant}/agents"):
                item = self._view(doc)
                if item.tenant_id != tenant or path != f"tenants/{tenant}/agents/{item.id}":
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                agents.append(item)
            agents.sort(key=lambda a: a.id)
            current = current.model_copy(
                update={
                    "authorization_stamp": fingerprint(
                        [
                            current.authorization_stamp,
                            [[a.id, a.version] for a in agents],
                        ]
                    )
                }
            )
            query: JsonValue = {"operation": "agents.list", "tenant": tenant, "limit": limit}
            position = (
                ""
                if cursor is None
                else self._cursors.read(cursor, query=query, authorize=lambda: current)
            )
            remaining = [a for a in agents if a.id > position]
            next_cursor = (
                self._cursors.issue(
                    position=remaining[limit - 1].id, query=query, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            current.ensure_live(self._now())
            return AgentPage(items=remaining[:limit], next_cursor=next_cursor)

        return self._store.run(read)

    def update(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        agent: ResourceId,
        body: AgentPatch,
        key: str | None,
        if_match: str | None,
    ) -> AgentDefinition:
        expected = require_if_match(if_match)

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            current = self._authorize(tx, identity, tenant, manage=True)
            if replay is not None and replay.resource_id != agent:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return current

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            doc = self._document(tx, tenant, agent)
            version = compare_and_write(
                tx,
                authorized_scope=current,
                path=f"tenants/{tenant}/agents/{agent}",
                if_match=if_match,
                replacement={
                    **doc,
                    **body.model_dump(mode="json", exclude_unset=True),
                    "updated_at": self._now(),
                },
            )
            return MutationResult(status_code=200, resource_id=agent, version=version)

        self._idempotency.execute(
            operation=f"agents.update:{agent}",
            key=key,
            body={"patch": body.model_dump(mode="json", exclude_unset=True), "version": expected},
            authorize=authorize,
            mutate=mutate,
        )
        return self.get(identity, tenant, agent)
