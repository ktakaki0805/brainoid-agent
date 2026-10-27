"""Authenticated Calendar HTTP admission and explicit owner calendar selection."""

from datetime import timedelta

from brainoid_contracts import ResourceId
from brainoid_contracts.access import AuthorizedScope
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import protect
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.calendar_access import current_integration
from brainoid_memory.calendar_oauth import IntegrationView, integration_view
from brainoid_memory.calendar_reader import (
    CalendarPage,
    CalendarQuery,
    CalendarReader,
    CalendarSelection,
    integration_path,
    integration_stamp,
    normalize,
)

from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.versioning import require_if_match
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.services.calendar_authorization import CalendarAuthorization
from brainoid_api.services.private_sessions import PrivateSessions


class _SelectionProbeRequired(Exception):
    """Abort an uncommitted transaction before performing provider I/O."""

    def __init__(self, record: Document) -> None:
        super().__init__("Calendar selection requires access verification")
        self.record = record


class CalendarEvents:
    def __init__(
        self,
        authorization: CalendarAuthorization,
        reader: CalendarReader,
        sessions: PrivateSessions | None = None,
    ) -> None:
        self.authorization, self.reader, self.sessions = authorization, reader, sessions

    def current(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        session: ResourceId | None = None,
        *,
        calendar: str | None = None,
    ) -> tuple[Document, AuthorizedScope]:
        tenants = self.authorization.tenants
        if isinstance(identity, AuthenticatedIdentity):
            if session is not None:
                raise APIError(ErrorCode.INVALID_REQUEST)
            user = tenants.authorize_user(tx, identity)
            protect(tx, lambda: user.ensure_live(tenants._now()))
            principal = tenants._principal(tx, identity, tenant)
        else:
            if self.sessions is None or session is None or identity.tenant_id != tenant:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            principal = self.sessions.current(tx, identity, session, scope="calendar.read")
        policy = transaction_policy(
            tx, now=tenants._now, ledger=tenants._ledger, allow_untracked_bootstrap=True
        )
        record, scope = policy_call(
            lambda: current_integration(tx, policy, principal, integration, calendar=calendar)
        )
        protect(tx, lambda: scope.ensure_live(tenants._now()))
        return record, scope

    def list(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        query: CalendarQuery,
        session: ResourceId | None = None,
    ) -> CalendarPage:
        return self.reader.list(
            lambda tx: self.current(
                tx, identity, tenant, integration, session, calendar=query.calendar_id
            ),
            query,
        )

    def select(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        integration: ResourceId,
        body: CalendarSelection,
        key: str | None,
        if_match: str | None,
    ) -> IntegrationView:
        expected = require_if_match(if_match)
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)
        verified_stamp: str | None = None

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            self.current(tx, identity, tenant, integration)
            if replay is not None and replay.resource_id != integration:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            # Mutation receipts bind owner/account/membership, not mutable selection.
            return self.authorization.authority(tx, identity, tenant)

        def mutate(tx: Transaction, scope: AuthorizedScope) -> MutationResult:
            record, _ = self.current(tx, identity, tenant, integration)
            if record["version"] != expected:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if verified_stamp is None:
                raise _SelectionProbeRequired(record)
            if integration_stamp(record) != verified_stamp:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            tx.set(
                integration_path(tenant, integration),
                {
                    **record,
                    **body.model_dump(mode="json"),
                    "version": expected + 1,
                    "updated_at": self.reader.now(),
                },
            )
            return MutationResult(status_code=200, resource_id=integration, version=expected + 1)

        def execute() -> MutationResult:
            return Idempotency(self.reader.store, now=self.reader.now).execute(
                operation="calendar.select:" + integration,
                key=key,
                body={"expected": expected, **body.model_dump(mode="json")},
                authorize=authorize,
                mutate=mutate,
            )

        def probe(record: Document) -> None:
            def before_send() -> None:
                latest, _ = self.reader.store.run(
                    lambda tx: self.current(tx, identity, tenant, integration)
                )
                if integration_stamp(latest) != integration_stamp(record):
                    raise APIError(ErrorCode.VERSION_CONFLICT)

            # Only uncommitted mutations probe actual token ACLs. These read-only
            # external calls must stay outside retried Firestore transactions.
            for calendar in body.allowed_calendar_ids:
                now = self.reader.now()
                query = CalendarQuery(
                    calendar_id=calendar,
                    time_min=now,
                    time_max=now + timedelta(seconds=1),
                    limit=1,
                )
                raw = self.reader.fetch(record, query, None, before_send)
                try:
                    normalize(raw, query, self.reader.now())
                except (ValueError, KeyError, TypeError):
                    raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
                if raw.get("accessRole") not in {
                    "reader",
                    "writer",
                    "writerWithoutPrivateAccess",
                    "owner",
                }:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None

        pending_record: Document | None = None
        try:
            execute()
        except _SelectionProbeRequired as pending:
            pending_record = pending.record
        if pending_record is not None:
            try:
                probe(pending_record)
            except APIError as failure:
                # Another identical request may have committed during secret I/O,
                # between probes, or before a failed provider response. Recheck
                # current authority and receipts without permitting a new write:
                # verified_stamp is still None, so no receipt means abort again.
                try:
                    execute()
                except _SelectionProbeRequired:
                    raise failure from None
            else:
                verified_stamp = integration_stamp(pending_record)
                # Only a new mutation checks captured version/stamp. A concurrent
                # same-key commit returns its receipt under current authorization.
                execute()
        return integration_view(
            self.reader.store.run(lambda tx: self.current(tx, identity, tenant, integration))[0]
        )
