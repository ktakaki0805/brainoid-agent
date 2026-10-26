"""Short-lived, user-consented Pi delegation and transaction-bound activity admission."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp, Version, normalize_utc
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import Action, DeviceSubject, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied, Principal, ResourceGrant
from pydantic import Field, StrictBool, ValidationError, model_validator

from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.common.access import AuthorizedUserScope, fingerprint
from brainoid_api.common.transactions import Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.device_credentials import DeviceCredentialRepository, Input, _key
from brainoid_api.repositories.tenants import Tenants

SessionScope = Literal[
    "conversation",
    "memory.read",
    "memory.write",
    "notes",
    "tasks",
    "reminders",
    "calendar.read",
    "calendar.propose",
    "home.control",
]
LIFETIME = timedelta(minutes=5)
IDLE = timedelta(seconds=60)


class PrivateSessionCreate(Input):
    allowed_scopes: Annotated[list[SessionScope], Field(min_length=1, max_length=9)]
    acknowledge_shared_space: StrictBool

    @model_validator(mode="after")
    def consent(self) -> Self:
        if not self.acknowledge_shared_space or len(set(self.allowed_scopes)) != len(
            self.allowed_scopes
        ):
            raise ValueError("Explicit shared-space consent and unique scopes are required")
        return self


class PrivateSession(Input):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    user_id: FirebaseUid
    device_id: ResourceId
    allowed_scopes: list[SessionScope]
    expires_at: UtcTimestamp
    idle_expires_at: UtcTimestamp
    status: Literal["active", "ended", "expired"]


class SessionRecord(PrivateSession):
    authorization_version: Version
    membership_authorization_version: Version
    device_authorization_version: Version
    connection_generation: Version
    user_authorization_stamp: str
    last_activity_at: UtcTimestamp


class PrivateSessions:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        ledger: ControlLedger,
        credentials: DeviceCredentialRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store, self._tenants, self._ledger = store, tenants, ledger
        self._credentials, self._now = credentials, now

    def _policy(self, tx: Transaction) -> Evaluator:
        return transaction_policy(
            tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
        )

    def _user(
        self, tx: Transaction, identity: AuthenticatedIdentity, tenant: ResourceId
    ) -> tuple[AuthorizedUserScope, Principal]:
        user = self._tenants.authorize_user(tx, identity)
        principal = policy_call(
            lambda: self._policy(tx).principal(tenant, UserSubject(user_id=identity.user_id))
        )
        return user, principal

    @staticmethod
    def _path(tenant: ResourceId, session: ResourceId) -> str:
        return f"tenants/{tenant}/private_sessions/{session}"

    @staticmethod
    def _slot(tenant: ResourceId, device: ResourceId) -> str:
        return f"tenants/{tenant}/private_session_slots/{device}"

    def _read(self, tx: Transaction, tenant: ResourceId, session: ResourceId) -> SessionRecord:
        doc = tx.get(self._path(tenant, session))
        if doc is None or doc.get("tenant_id") != tenant or doc.get("id") != session:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        try:
            return SessionRecord.model_validate(doc)
        except ValidationError:
            # Pre-IAM-09/incomplete records are never upgraded into consented sessions.
            raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None

    @staticmethod
    def _view(record: SessionRecord) -> PrivateSession:
        return PrivateSession.model_validate(
            {field: getattr(record, field) for field in PrivateSession.model_fields}
        )

    def _effective(self, tx: Transaction, record: SessionRecord) -> SessionRecord:
        if record.status != "active":
            return record
        try:
            self._policy(tx).principal(
                record.tenant_id,
                DeviceSubject(
                    device_id=record.device_id,
                    connection_generation=record.connection_generation,
                    private_session_id=record.id,
                ),
            )
        except PolicyDenied:
            return record.model_copy(
                update={
                    "status": "expired",
                    "version": record.version + 1,
                    "authorization_version": record.authorization_version + 1,
                    "updated_at": normalize_utc(self._now()),
                }
            )
        return record

    def _persist(self, tx: Transaction, record: SessionRecord) -> None:
        tx.set(self._path(record.tenant_id, record.id), record.model_dump())

    def start(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        device: ResourceId,
        body: PrivateSessionCreate,
        key: str | None,
        request_id: str,
    ) -> PrivateSession:
        key = _key(key)
        binding = fingerprint(
            [
                device,
                ",".join(sorted(body.allowed_scopes)),
                body.acknowledge_shared_space,
            ]
        )
        receipt_path = f"tenants/{tenant}/private_session_requests/{fingerprint(['start', identity.user_id, key])}"

        def start(tx: Transaction) -> PrivateSession:
            user, member = self._user(tx, identity, tenant)
            receipt = tx.get(receipt_path)
            if receipt is not None:
                if receipt["binding"] != binding:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                record = self._read(tx, tenant, ResourceId(receipt["session_id"]))
                if record.user_id != identity.user_id or record.device_id != device:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                current = self._effective(tx, record)
                if current != record:
                    self._persist(tx, current)
                return self._view(current)
            device_doc = tx.get(f"tenants/{tenant}/devices/{device}")
            if device_doc is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            device_subject = DeviceSubject(
                device_id=device, connection_generation=device_doc["connection_generation"]
            )
            target = policy_call(lambda: self._policy(tx).principal(tenant, device_subject))
            if not set(body.allowed_scopes) <= target.context.actor.allowed_scopes:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            slot_path = self._slot(tenant, device)
            slot = tx.get(slot_path)
            old = None
            if slot is not None:
                old = self._effective(tx, self._read(tx, tenant, ResourceId(slot["session_id"])))
                if old.status == "active":
                    raise APIError(ErrorCode.CONFLICT)
            now = normalize_utc(self._now())
            assert member.membership is not None
            record = SessionRecord(
                id=ResourceId(str(uuid4())),
                tenant_id=tenant,
                device_id=device,
                user_id=identity.user_id,
                allowed_scopes=sorted(body.allowed_scopes),
                status="active",
                version=1,
                authorization_version=1,
                membership_authorization_version=member.membership.authorization_version,
                device_authorization_version=target.context.actor.authorization_version,
                connection_generation=device_subject.connection_generation,
                user_authorization_stamp=user.authorization_stamp,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
                expires_at=now + LIFETIME,
                idle_expires_at=now + IDLE,
            )
            user.ensure_live(now)
            # All authorization/slot/receipt reads precede every write.
            if old is not None:
                self._persist(tx, old)
            tx.create(self._path(tenant, record.id), record.model_dump())
            tx.set(slot_path, {"session_id": record.id})
            tx.create(receipt_path, {"binding": binding, "session_id": record.id})
            self._audit(tx, record, "started", request_id)
            return self._view(record)

        return self._store.run(start)

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, session: ResourceId
    ) -> PrivateSession:
        def get(tx: Transaction) -> PrivateSession:
            self._user(tx, identity, tenant)
            record = self._read(tx, tenant, session)
            if record.user_id != identity.user_id:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            current = self._effective(tx, record)
            if current != record:
                self._persist(tx, current)
            return self._view(current)

        return self._store.run(get)

    def end(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        session: ResourceId,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> None:
        key, expected = _key(key), require_if_match(if_match)
        path = f"tenants/{tenant}/private_session_requests/{fingerprint(['end', identity.user_id, key])}"
        binding = fingerprint([session, expected])

        def end(tx: Transaction) -> None:
            self._user(tx, identity, tenant)
            record = self._read(tx, tenant, session)
            if record.user_id != identity.user_id:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            receipt = tx.get(path)
            if receipt is not None:
                if receipt["binding"] != binding:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return
            # Idle updates leave the consent version unchanged; terminal transitions don't.
            if record.version != expected:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            current = self._effective(tx, record)
            if current.status == "active":
                current = current.model_copy(
                    update={
                        "status": "ended",
                        "version": current.version + 1,
                        "authorization_version": current.authorization_version + 1,
                        "updated_at": normalize_utc(self._now()),
                    }
                )
            self._persist(tx, current)
            tx.create(path, {"binding": binding, "session_id": session})
            self._audit(tx, current, current.status, request_id)

        self._store.run(end)

    @staticmethod
    def _audit(tx: Transaction, record: SessionRecord, result: str, request_id: str) -> None:
        tx.create(
            f"tenants/{record.tenant_id}/audit_events/{uuid4()}",
            {
                "actor_user_id": record.user_id,
                "target_session_id": record.id,
                "result": result,
                "request_id": request_id,
                "at": record.updated_at,
            },
        )

    def current(
        self,
        tx: Transaction,
        identity: DeviceIdentity,
        session: ResourceId,
        *,
        scope: str | None = None,
        expected_user_id: FirebaseUid | None = None,
    ) -> Principal:
        """Recheck in the resource/effect transaction; IDs/old principals confer no authority."""
        self._credentials.authorize(tx, identity)
        principal = policy_call(
            lambda: self._policy(tx).principal(
                identity.tenant_id,
                DeviceSubject(
                    device_id=identity.sub,
                    connection_generation=identity.connection_generation,
                    private_session_id=session,
                ),
            )
        )
        if (scope is not None and scope not in principal.context.actor.allowed_scopes) or (
            expected_user_id is not None and expected_user_id != principal.user_id
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return principal

    def context(self, identity: DeviceIdentity, session: ResourceId) -> Principal:
        return self._store.run(lambda tx: self.current(tx, identity, session))

    def snapshot(self, tx: Transaction, identity: DeviceIdentity) -> PrivateSession | None:
        """DEV-04 adapter: read after other snapshot reads, before writes; never activity.

        Returns only the current active session; persists derived terminal state, without
        deleting the slot or old receipts. No heartbeat or reconnect may extend idle time.
        """
        self._credentials.authorize(tx, identity)
        slot = tx.get(self._slot(identity.tenant_id, identity.sub))
        if slot is None:
            return None
        record = self._read(tx, identity.tenant_id, ResourceId(slot["session_id"]))
        if record.device_id != identity.sub:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        current = self._effective(tx, record)
        if current != record:
            self._persist(tx, current)
        return self._view(current) if current.status == "active" else None

    def accept_activity(
        self,
        tx: Transaction,
        identity: DeviceIdentity,
        session: ResourceId,
        activity_id: ResourceId,
        grant: ResourceGrant,
    ) -> PrivateSession:
        """Internal admission hook for a NEW user-initiated resource operation.

        Call after business reads and before business writes in their SAME transaction.
        The activity ID is the server's durable operation ID, reused on request replay.
        Never call for heartbeat, polling, snapshots, worker/model/output completion, or
        replayed reads. Resource grants must originate from policy, never request/model.
        This hook performs no external effects and rechecks the supplied grant itself.
        """
        return self.prepare_activity(tx, identity, session, activity_id, grant)()

    def prepare_activity(
        self,
        tx: Transaction,
        identity: DeviceIdentity,
        session: ResourceId,
        activity_id: ResourceId,
        grant: ResourceGrant,
        *,
        output: Subject | None = None,
    ) -> Callable[[], PrivateSession]:
        """Read/authorize now, return DB-only writes for an atomic outbox admission.

        The callback belongs to this transaction and must run after all other reads.
        It is never retained across transactions or invoked on request replay.
        """
        principal = self.current(tx, identity, session)
        if grant.principal.subject != principal.subject or grant.action == Action.MODEL_READ:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        policy_call(
            lambda: self._policy(tx).resource(
                identity.tenant_id,
                principal.subject,
                grant.ref,
                action=Action(grant.action),
                output=output,
                previous=grant,
            )
        )
        path = f"tenants/{identity.tenant_id}/private_session_activities/{activity_id}"
        binding = fingerprint(
            [
                identity.sub,
                session,
                grant.ref.model_dump(mode="json"),
                grant.action,
            ]
        )
        receipt = tx.get(path)
        record = self._read(tx, identity.tenant_id, session)
        if receipt is not None:
            if receipt["binding"] != binding:
                raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return lambda: self._view(record)
        now = normalize_utc(self._now())
        if now >= min(record.expires_at, record.idle_expires_at):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        current = record.model_copy(
            update={
                "last_activity_at": now,
                "idle_expires_at": min(record.expires_at, now + IDLE),
            }
        )

        def write() -> PrivateSession:
            self._persist(tx, current)
            tx.create(path, {"binding": binding, "session_id": session})
            return self._view(current)

        return write
