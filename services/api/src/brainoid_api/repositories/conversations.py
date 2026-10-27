"""Authorized conversation reads and atomic user-message/run/outbox admission."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from uuid import uuid4

from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Scope, Visibility
from brainoid_contracts.deletion import fenced_document, purge_epoch
from brainoid_contracts.guards import GuardedTransactions, ReleaseGuard, protect, release_guard
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_jobs import Jobs
from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.models import (
    Action,
    DeviceSubject,
    ResourceKind,
    ResourceRef,
    Subject,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, Principal, ResourceGrant
from brainoid_policy.transactions import resource_state
from brainoid_runs.events import save_projection
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.models import (
    Conversation,
    ConversationCreate,
    ConversationPage,
    ConversationPatch,
    CredentialBinding,
    Message,
    MessagePage,
    Run,
    RunAccepted,
    TurnCreate,
)
from brainoid_runs.state import (
    KIND,
    agent,
    conversation_path,
    failed_projection,
    history,
    run_path,
    view,
)
from brainoid_usage import QuotaExceeded, UsageLedger
from pydantic import JsonValue

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.versioning import compare_and_write, require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.agents import AgentIdentity
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.private_sessions import PrivateSessions


def policy_call[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except QuotaExceeded:
        raise APIError(ErrorCode.RATE_LIMITED) from None
    except PolicyUnavailable:
        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
    except PolicyDenied as error:
        raise APIError(ErrorCode(error.code)) from None


@dataclass(frozen=True)
class ReadResult[T]:
    value: T
    guard: ReleaseGuard

    def release(self) -> T:
        policy_call(self.guard.finish)
        return self.value


class Conversations:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        cursors: Cursors,
        jobs: Jobs,
        *,
        credentials: DeviceCredentialRepository | None = None,
        sessions: PrivateSessions | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store, self.tenants, self.cursors, self.jobs = store, tenants, cursors, jobs
        self.credentials, self.sessions, self.now = credentials, sessions, now
        self.idempotency = Idempotency(store, now=now)
        self.usage = UsageLedger(store, now=now)

    def current_read[T](self, work: Callable[[Transaction], T]) -> ReadResult[T]:
        """Read evidence is local to the successful attempt, including response delay."""

        def read(tx: Transaction) -> ReadResult[T]:
            result = work(tx)
            return ReadResult(result, release_guard(tx))

        return policy_call(lambda: GuardedTransactions(self.store).run(read))

    def principal(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None,
    ) -> tuple[Principal, str | None]:
        account = None
        if isinstance(identity, AuthenticatedIdentity):
            if session is not None:
                raise APIError(ErrorCode.INVALID_REQUEST)
            account = self.tenants.authorize_user(tx, identity).authorization_stamp
            subject: Subject = UserSubject(user_id=identity.user_id)
        else:
            if self.credentials is None:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            self.credentials.authorize(tx, identity)
            if identity.tenant_id != tenant:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            subject = DeviceSubject(
                device_id=identity.sub,
                connection_generation=identity.connection_generation,
                private_session_id=session,
            )
        return policy_call(lambda: self.jobs._policy(tx).principal(tenant, subject)), account

    @staticmethod
    def scope(
        principal: Principal,
        account: str | None,
        scope: Scope,
        owner: FirebaseUid | None,
        output_stamp: str | None = None,
    ) -> AuthorizedScope:
        return AuthorizedScope(
            context=principal.context,
            scope=scope,
            owner_user_id=owner,
            authorization_stamp=fingerprint([principal.stamp, account, output_stamp]),
        )

    @staticmethod
    def document(tx: Transaction, tenant: ResourceId, conversation: ResourceId) -> Document:
        doc = tx.get(conversation_path(tenant, conversation))
        if doc is None or doc.get("id") != conversation or doc.get("tenant_id") != tenant:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return doc

    def access(
        self,
        tx: Transaction,
        identity: AgentIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        session: ResourceId | None,
        *,
        action: Action = Action.READ,
        output: Subject | None = None,
    ) -> tuple[ResourceGrant, AuthorizedScope]:
        principal, account = self.principal(tx, identity, tenant, session)
        grant = policy_call(
            lambda: self.jobs._policy(tx).resource(
                tenant,
                principal.subject,
                ResourceRef(tenant_id=tenant, kind=ResourceKind.CONVERSATION, id=conversation),
                action=action,
                output=output,
            )
        )
        current = self.scope(
            principal, account, grant.scope, grant.owner_user_id, grant.output_stamp
        )
        current.ensure_live(self.now())
        return grant, current

    def get(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        session: ResourceId | None = None,
    ) -> Conversation:
        def read(tx: Transaction) -> Conversation:
            _, current = self.access(tx, identity, tenant, conversation, session)
            result = view(Conversation, self.document(tx, tenant, conversation))
            current.ensure_live(self.now())
            return result

        return self.store.run(read)

    def create(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        body: ConversationCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> Conversation:
        conversation = ResourceId(str(uuid4()))
        requested = Scope(visibility=body.visibility, display_policy=body.display_policy)

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            principal, account = self.principal(tx, identity, tenant, session)
            search = policy_call(
                lambda: self.jobs._policy(tx).search(
                    tenant,
                    principal.subject,
                    ResourceKind.CONVERSATION,
                )
            )
            owner = principal.user_id if body.visibility == Visibility.PRIVATE else None
            if not any(
                f.visibility == body.visibility
                and body.display_policy in f.display_policies
                and f.owner_user_id == owner
                for f in search.filters
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if replay is not None:
                assert replay.resource_id is not None
                self.access(tx, identity, tenant, replay.resource_id, session)
            return self.scope(principal, account, requested, owner)

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            policy_call(lambda: agent(tx, tenant, body.agent_id))
            now = self.now()
            result = Conversation(
                **body.model_dump(),
                id=conversation,
                tenant_id=tenant,
                version=1,
                created_at=now,
                updated_at=now,
                owner_user_id=current.owner_user_id,
            )
            tx.create(
                conversation_path(tenant, conversation),
                {
                    **result.model_dump(mode="json"),
                    "owner_user_id": current.owner_user_id,
                    "created_by": current.context.actor.actor_id,
                    "deletion_epoch": 0,
                    "deleted": False,
                    "suppress_reuse": False,
                    "history_revision": 0,
                    "next_sequence": 1,
                    "active_run_id": None,
                },
            )
            return MutationResult(status_code=201, resource_id=conversation, version=1)

        receipt = self.idempotency.execute(
            operation="conversations.create",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=mutate,
        )
        assert receipt.resource_id is not None
        return self.get(identity, tenant, receipt.resource_id, session)

    def update(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        body: ConversationPatch,
        key: str | None,
        if_match: str | None,
    ) -> Conversation:
        expected = require_if_match(if_match)

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            _, current = self.access(tx, identity, tenant, conversation, None, action=Action.WRITE)
            if body.display_policy is not None:
                try:
                    target = Scope(
                        visibility=current.scope.visibility, display_policy=body.display_policy
                    )
                except ValueError:
                    raise APIError(ErrorCode.INVALID_REQUEST) from None
                # Current resource authorization still runs above on every replay.
                current = current.model_copy(update={"scope": target})
            return current

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            doc = self.document(tx, tenant, conversation)
            replacement = {
                **doc,
                **body.model_dump(mode="json", exclude_unset=True),
                "updated_at": self.now().isoformat(),
            }
            try:
                Scope(
                    visibility=replacement["visibility"],
                    display_policy=replacement["display_policy"],
                )
            except ValueError:
                raise APIError(ErrorCode.INVALID_REQUEST) from None
            version = compare_and_write(
                tx,
                authorized_scope=current,
                path=conversation_path(tenant, conversation),
                if_match=if_match,
                replacement=replacement,
            )
            return MutationResult(status_code=200, resource_id=conversation, version=version)

        self.idempotency.execute(
            operation=f"conversations.update:{conversation}",
            key=key,
            body={"patch": body.model_dump(mode="json", exclude_unset=True), "version": expected},
            authorize=authorize,
            mutate=mutate,
        )
        return self.get(identity, tenant, conversation)

    def list(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ConversationPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> ConversationPage:
            principal, account = self.principal(tx, identity, tenant, session)
            search = policy_call(
                lambda: self.jobs._policy(tx).search(
                    tenant, principal.subject, ResourceKind.CONVERSATION
                )
            )
            items = []
            for path, doc in tx.collection(f"tenants/{tenant}/conversations"):
                identity_id = ResourceId(doc["id"])
                if path != conversation_path(tenant, identity_id):
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                resource = policy_call(
                    partial(
                        resource_state,
                        ResourceRef(
                            tenant_id=tenant, kind=ResourceKind.CONVERSATION, id=identity_id
                        ),
                        doc,
                    )
                )
                if (
                    resource is not None
                    and not resource.deleted
                    and any(f.matches(resource) for f in search.filters)
                ):
                    items.append(view(Conversation, doc))
            items.sort(key=lambda x: x.id)
            current = self.scope(
                principal,
                account,
                Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                None,
            )
            current = current.model_copy(
                update={
                    "authorization_stamp": fingerprint(
                        [current.authorization_stamp, [i.model_dump(mode="json") for i in items]]
                    )
                }
            )
            query: JsonValue = {"operation": "conversations.list", "tenant": tenant, "limit": limit}
            position = (
                ""
                if cursor is None
                else self.cursors.read(cursor, query=query, authorize=lambda: current)
            )
            remaining = [item for item in items if item.id > position]
            next_cursor = (
                self.cursors.issue(
                    position=remaining[limit - 1].id, query=query, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            current.ensure_live(self.now())
            return ConversationPage(items=remaining[:limit], next_cursor=next_cursor)

        return self.store.run(read)

    def messages(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> MessagePage:
        return self.messages_for_release(
            identity, tenant, conversation, session, limit=limit, cursor=cursor
        ).release()

    def messages_for_release(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        session: ResourceId | None = None,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ReadResult[MessagePage]:
        limit = page_limit(limit)

        def read(tx: Transaction) -> MessagePage:
            grant, current = self.access(tx, identity, tenant, conversation, session)
            parent = self.document(tx, tenant, conversation)
            # Display only public fields; internal model signatures never reach this view.
            docs = tx.collection(conversation_path(tenant, conversation) + "/messages")
            docs.sort(key=lambda pair: pair[1]["sequence"])
            items = []
            for path, doc in docs:
                item = view(Message, doc)
                if (
                    doc.get("tenant_id") != tenant
                    or doc.get("conversation_id") != conversation
                    or path != conversation_path(tenant, conversation) + "/messages/" + item.id
                    or type(doc.get("deleted")) is not bool
                ):
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                current_document = fenced_document(
                    tx, tenant, "messages", item.id, doc, parent=conversation
                )
                if current_document["deleted"]:
                    continue
                try:
                    check_calendar_sources(
                        tx,
                        self.jobs._policy(tx),
                        grant.principal,
                        doc,
                        credential_epoch=self.credentials.credential_epoch
                        if self.credentials
                        else None,
                        sources=CanonicalSources(SavedMessages()),
                    )
                except PolicyUnavailable:
                    raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
                except PolicyDenied:
                    continue
                items.append(item)
            current = current.model_copy(
                update={
                    "authorization_stamp": fingerprint(
                        [
                            current.authorization_stamp,
                            parent["version"],
                            [i.model_dump(mode="json") for i in items],
                        ]
                    )
                }
            )
            query: JsonValue = {
                "operation": "messages.list",
                "tenant": tenant,
                "conversation": conversation,
                "limit": limit,
            }
            position = (
                ""
                if cursor is None
                else self.cursors.read(cursor, query=query, authorize=lambda: current)
            )
            start = (
                0
                if not position
                else next((i + 1 for i, m in enumerate(items) if m.id == position), len(items))
            )
            remaining = items[start:]
            next_cursor = (
                self.cursors.issue(
                    position=remaining[limit - 1].id, query=query, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            protect(tx, lambda: current.ensure_live(self.now()))
            return MessagePage(items=remaining[:limit], next_cursor=next_cursor)

        return self.current_read(read)

    def output(self, tx: Transaction, principal: Principal, body: TurnCreate) -> Subject:
        if body.output_device_id is None:
            output = principal.subject
        else:
            device = tx.get(
                f"tenants/{principal.context.tenant_id}/devices/{body.output_device_id}"
            )
            if device is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            session = None
            # The current slot, never a request's unverified session ID, selects the
            # same user's private display session for a smartphone -> Pi output.
            slot = tx.get(
                f"tenants/{principal.context.tenant_id}/private_session_slots/{body.output_device_id}"
            )
            if slot is not None and principal.user_id is not None:
                candidate = tx.get(
                    f"tenants/{principal.context.tenant_id}/private_sessions/{slot['session_id']}"
                )
                if candidate is not None and candidate.get("user_id") == principal.user_id:
                    session = ResourceId(candidate["id"])
            output = DeviceSubject(
                device_id=body.output_device_id,
                connection_generation=device["connection_generation"],
                private_session_id=session,
            )
        if isinstance(output, DeviceSubject):
            device = tx.get(f"tenants/{principal.context.tenant_id}/devices/{output.device_id}")
            if device is None or "display" not in device.get("capabilities", []):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return output

    def turn(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        conversation: ResourceId,
        body: TurnCreate,
        key: str | None,
        session: ResourceId | None = None,
    ) -> RunAccepted:
        run_id, message_id = ResourceId(str(uuid4())), ResourceId(str(uuid4()))

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            principal, _ = self.principal(tx, identity, tenant, session)
            output = self.output(tx, principal, body)
            _, current = self.access(
                tx, identity, tenant, conversation, session, action=Action.POST, output=output
            )
            if replay is not None:
                assert replay.resource_id is not None
                doc = tx.get(run_path(tenant, replay.resource_id))
                if doc is None or doc.get("conversation_id") != conversation:
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            return current

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            principal, _ = self.principal(tx, identity, tenant, session)
            output = self.output(tx, principal, body)
            grant, _ = self.access(
                tx, identity, tenant, conversation, session, action=Action.POST, output=output
            )
            parent = self.document(tx, tenant, conversation)
            prior = None
            old = None
            if parent.get("active_run_id") is not None:
                old = tx.get(run_path(tenant, parent["active_run_id"]))
                if old is None:
                    raise APIError(ErrorCode.CONFLICT)
                prior = failed_projection(tx, old)
                if prior["status"] in ("queued", "running", "waiting_confirmation"):
                    raise APIError(ErrorCode.CONFLICT)
            definition = policy_call(lambda: agent(tx, tenant, parent["agent_id"]))
            prior_messages, _, history_digest = policy_call(
                lambda: history(tx, tenant, conversation)
            )
            admission_epoch = purge_epoch(tx, tenant)
            policy_call(
                lambda: self.jobs._policy(tx).resource(
                    tenant, principal.subject, grant.ref, action=Action.MODEL_READ, output=output
                )
            )
            destination = policy_call(lambda: self.jobs._policy(tx).principal(tenant, output))
            scopes: list[Scope] = []
            if "memory.read" in (
                principal.context.actor.allowed_scopes & destination.context.actor.allowed_scopes
            ):
                search = policy_call(
                    lambda: self.jobs._policy(tx).search(
                        tenant, principal.subject, ResourceKind.MEMORY, output=output
                    )
                )
                scopes = [
                    Scope(visibility=f.visibility, display_policy=d)
                    for f in search.filters
                    for d in sorted(f.display_policies)
                    if f.visibility != Visibility.PRIVATE or d != DisplayPolicy.SHARED_DISPLAY
                    if grant.scope.visibility == Visibility.PRIVATE
                    or f.visibility == Visibility.SHARED
                    if {
                        DisplayPolicy.NEVER: 0,
                        DisplayPolicy.AUTHENTICATED_SESSION: 1,
                        DisplayPolicy.SHARED_DISPLAY: 2,
                    }[d]
                    >= {
                        DisplayPolicy.NEVER: 0,
                        DisplayPolicy.AUTHENTICATED_SESSION: 1,
                        DisplayPolicy.SHARED_DISPLAY: 2,
                    }[grant.scope.display_policy]
                ]
            activity = None
            if isinstance(identity, DeviceIdentity) and session is not None:
                if self.sessions is None:
                    raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
                activity = self.sessions.prepare_activity(
                    tx, identity, session, run_id, grant, output=output
                )
            now = self.now()
            record = Run(
                run_id=run_id,
                conversation_id=conversation,
                status="queued",
                version=1,
                created_at=now,
            ).model_dump(mode="json", exclude_none=True)
            record.update(
                {
                    "tenant_id": tenant,
                    "events_version": 1,
                    "input_message_id": message_id,
                    "input_text": body.text,
                    "input_sequence": parent.get("next_sequence", 1),
                    "agent": definition.model_dump(mode="json"),
                    "history_revision": parent.get("history_revision", 0),
                    "history_hash": history_digest,
                    "purge_epoch": admission_epoch,
                    "calendar_sources": list(
                        {
                            fingerprint(source): source
                            for message in prior_messages
                            for source in message.get("calendar_sources", [])
                        }.values()
                    ),
                    "history_run_ids": sorted(
                        {d["run_id"] for d in prior_messages if d["role"] != "user"}
                    ),
                    "memory_scopes": [s.model_dump(mode="json") for s in scopes],
                    "credential": None
                    if isinstance(identity, AuthenticatedIdentity)
                    else CredentialBinding(
                        tenant_id=tenant,
                        device_id=identity.sub,
                        family_id=identity.family_id,
                        credential_epoch=identity.credential_epoch,
                        authorization_version=identity.authorization_version,
                        connection_generation=identity.connection_generation,
                    ).model_dump(mode="json"),
                }
            )
            message = Message(
                id=message_id, role="user", text=body.text, run_id=run_id, created_at=now
            )
            reserve_usage = policy_call(
                lambda: self.usage.prepare_admit(
                    tx,
                    tenant,
                    run_id,
                    user=principal.user_id,
                    device=principal.subject.device_id
                    if isinstance(principal.subject, DeviceSubject)
                    else None,
                )
            )
            # add_pending performs the last reads before any business/session writes.
            policy_call(
                lambda: self.jobs.add_pending(
                    tx, operation_id=run_id, kind=KIND, grant=grant, output=output
                )
            )
            reserve_usage()
            if activity is not None:
                activity()
            if prior is not None:
                assert old is not None
                save_projection(tx, old, prior, now)
            tx.create(run_path(tenant, run_id), record)
            tx.create(
                conversation_path(tenant, conversation) + "/messages/" + message_id,
                {
                    **message.model_dump(mode="json"),
                    "tenant_id": tenant,
                    "conversation_id": conversation,
                    "sequence": record["input_sequence"],
                    "created_by": principal.user_id,
                    "version": 1,
                    "deletion_epoch": 0,
                    "deleted": False,
                    "suppress_reuse": False,
                },
            )
            tx.set(
                conversation_path(tenant, conversation),
                {
                    **parent,
                    "active_run_id": run_id,
                    "next_sequence": record["input_sequence"] + 1,
                },
            )
            return MutationResult(status_code=202, resource_id=run_id)

        receipt = self.idempotency.execute(
            operation=f"turns.create:{conversation}",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=mutate,
        )
        assert receipt.resource_id is not None
        base = f"/v1/tenants/{tenant}/runs/{receipt.resource_id}"
        return RunAccepted(
            run_id=receipt.resource_id, events_path=base + "/events", status_path=base
        )

    def get_run(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        run_id: ResourceId,
        session: ResourceId | None = None,
    ) -> Run:
        return self.run_for_release(identity, tenant, run_id, session).release()

    def run_for_release(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        run_id: ResourceId,
        session: ResourceId | None = None,
    ) -> ReadResult[Run]:
        def read(tx: Transaction) -> Run:
            doc = tx.get(run_path(tenant, run_id))
            if doc is None or doc.get("tenant_id") != tenant or doc.get("run_id") != run_id:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            grant, current = self.access(tx, identity, tenant, doc["conversation_id"], session)
            policy_call(
                lambda: check_calendar_sources(
                    tx,
                    self.jobs._policy(tx),
                    grant.principal,
                    doc,
                    credential_epoch=self.credentials.credential_epoch
                    if self.credentials
                    else None,
                    sources=CanonicalSources(SavedMessages()),
                )
            )
            updated = failed_projection(tx, doc)
            protect(tx, lambda: current.ensure_live(self.now()))
            release = (
                self.usage.prepare_release(tx, tenant, run_id)
                if updated["status"] in ("completed", "failed", "cancelled")
                else lambda: None
            )
            release()
            save_projection(tx, doc, updated, self.now())
            return view(Run, updated)

        return self.current_read(read)

    def cancel_run(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        run_id: ResourceId,
        key: str | None,
        session: ResourceId | None = None,
    ) -> Run:
        return self.cancel_for_release(identity, tenant, run_id, key, session).release()

    def cancel_for_release(
        self,
        identity: AgentIdentity,
        tenant: ResourceId,
        run_id: ResourceId,
        key: str | None,
        session: ResourceId | None = None,
    ) -> ReadResult[Run]:
        from brainoid_jobs import Job
        from brainoid_jobs.models import job_id
        from brainoid_jobs.repository import path as job_path
        from brainoid_runs.control import cancellation

        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            doc = tx.get(run_path(tenant, run_id))
            if doc is None or doc.get("tenant_id") != tenant or doc.get("run_id") != run_id:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            grant, current = self.access(
                tx, identity, tenant, doc["conversation_id"], session, action=Action.POST
            )
            saved = tx.get(job_path(tenant, job_id(tenant, KIND, run_id)))
            if saved is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            job = Job.model_validate(saved)
            actor, initiator = grant.principal, job.grant.principal
            if actor.subject != initiator.subject and not (
                isinstance(actor.subject, UserSubject)
                and actor.user_id is not None
                and actor.user_id == initiator.user_id
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return current

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            doc = tx.get(run_path(tenant, run_id))
            assert doc is not None
            current.ensure_live(self.now())
            policy_call(lambda: cancellation(tx, self.jobs, doc))
            return MutationResult(status_code=200, resource_id=run_id)

        self.idempotency.execute(
            operation=f"runs.cancel:{run_id}", key=key, body={}, authorize=authorize, mutate=mutate
        )
        return self.run_for_release(identity, tenant, run_id, session)
