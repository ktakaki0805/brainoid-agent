"""One-use code claims and resumable, independently confirmed membership grants."""

import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
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
from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    LedgerError,
    MemberTarget,
    subject_key,
)
from brainoid_policy.models import ManagementAction, Role, UserSubject
from brainoid_policy.scopes import Principal
from pydantic import BaseModel, ConfigDict, Field

from brainoid_api.auth.authorization import policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.repositories.tenants import Tenants

Code = Annotated[str, Field(strict=True, min_length=16, max_length=256)]
InviteRole = Literal["admin", "member"]
LIFETIME = timedelta(hours=24)
ATTEMPT_WINDOW = timedelta(minutes=15)
ATTEMPT_LIMIT = 5


class InvitationCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    role: InviteRole


class Invitation(InvitationCreate):
    id: ResourceId
    code: Code
    expires_at: UtcTimestamp


class InvitationAccept(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    code: Code


class MembershipResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: FirebaseUid
    role: Role
    status: Literal["active"] = "active"
    version: Version


class Invitations:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        ledger: ControlLedger,
        *,
        code_key: bytes,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if len(code_key) < 32:
            raise ValueError("Invitation signing key requires at least 32 bytes")
        self._store, self._tenants, self._ledger = store, tenants, ledger
        self._key, self._now = code_key, now
        self._idempotency = Idempotency(store, now=now)
        self._changes = AuthzChanges(store, ledger)

    def _code(self, invitation_id: ResourceId) -> str:
        # Domain-separated from cursor MACs. Only the digest is persisted anywhere.
        return hmac.new(
            self._key, b"brainoid/invitation/v1\0" + invitation_id.encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()

    def _issuer(self, tx: Transaction, tenant_id: ResourceId, uid: FirebaseUid) -> Principal:
        policy = transaction_policy(
            tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
        )
        principal = policy_call(
            lambda: policy.management(
                tenant_id, UserSubject(user_id=uid), ManagementAction.INVITE, new_role=Role.MEMBER
            )
        )
        return principal

    def create(
        self,
        identity: AuthenticatedIdentity,
        tenant_id: ResourceId,
        body: InvitationCreate,
        key: str | None,
    ) -> Invitation:
        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            user = self._tenants.authorize_user(tx, identity)
            principal = self._issuer(tx, tenant_id, identity.user_id)

            if replay is not None:
                self._issuance(tx, tenant_id, replay.resource_id, identity.user_id)
            return AuthorizedScope(
                context=principal.context,
                scope=Scope(visibility=Visibility.SHARED, display_policy=DisplayPolicy.NEVER),
                owner_user_id=None,
                authorization_stamp=fingerprint([principal.stamp, user.authorization_stamp]),
            )

        def mutate(tx: Transaction, current: AuthorizedScope) -> MutationResult:
            invitation_id = ResourceId(str(uuid4()))
            digest = self._digest(self._code(invitation_id))
            path = f"tenants/{tenant_id}/invitations/{invitation_id}"
            epoch = self._epoch(tx, tenant_id)
            tx.create(
                path,
                {
                    "id": invitation_id,
                    "tenant_id": tenant_id,
                    "role": body.role,
                    "code_hash": digest,
                    "created_by": identity.user_id,
                    "created_at": self._now(),
                    "expires_at": self._now() + LIFETIME,
                    "used_operation_id": None,
                    "invitation_epoch": epoch,
                },
            )
            tx.create(f"invitation_codes/{digest}", {"path": path})
            return MutationResult(status_code=201, resource_id=invitation_id)

        result = self._idempotency.execute(
            operation=f"invitations.create:{tenant_id}",
            key=key,
            body=body.model_dump(mode="json"),
            authorize=authorize,
            mutate=mutate,
        )

        def read(tx: Transaction) -> Invitation:
            authorize(tx, result)
            doc = self._issuance(tx, tenant_id, result.resource_id, identity.user_id)
            code = self._code(doc["id"])
            if not hmac.compare_digest(self._digest(code), doc["code_hash"]):
                # A rotated key cannot reissue a different code for the same receipt.
                raise APIError(ErrorCode.RESOURCE_EXPIRED)
            return Invitation(
                id=doc["id"], code=code, role=doc["role"], expires_at=doc["expires_at"]
            )

        return self._store.run(read)

    def _issuance(
        self,
        tx: Transaction,
        tenant_id: ResourceId,
        invitation_id: ResourceId | None,
        uid: FirebaseUid,
    ) -> Document:
        doc = tx.get(f"tenants/{tenant_id}/invitations/{invitation_id}")
        if doc is None or doc["created_by"] != uid:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if self._now() >= doc["expires_at"] or doc["used_operation_id"] is not None:
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        if doc["invitation_epoch"] != self._epoch(tx, tenant_id):
            raise APIError(ErrorCode.RESOURCE_EXPIRED)
        return doc

    @staticmethod
    def _epoch(tx: Transaction, tenant_id: ResourceId) -> int:
        tenant = tx.get(f"tenants/{tenant_id}")
        if tenant is None or tenant.get("status") != "active":
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        epoch = tenant.get("invitation_epoch", 0)
        if type(epoch) is not int or epoch < 0:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return epoch

    def _member(self, tx: Transaction, tenant_id: ResourceId, uid: FirebaseUid) -> Document | None:
        documents = tx.find(f"tenants/{tenant_id}/memberships", "user_id", uid)
        if len(documents) > 1:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if documents and documents[0].get("tenant_id") != tenant_id:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return documents[0] if documents else None

    def _eligible(
        self,
        tx: Transaction,
        tenant_id: ResourceId,
        uid: FirebaseUid,
        change: Change | None = None,
    ) -> int:
        # A completed revoke must release its tenant reservation before a rejoin can
        # advance the same subject. Otherwise its still-pending receipt is stranded.
        tenant = tx.get(f"tenants/{tenant_id}")
        if tenant is not None and tenant.get("membership_mutation") is not None:
            raise APIError(ErrorCode.CONFLICT)
        doc = self._member(tx, tenant_id, uid)
        latest = self._ledger.latest(subject_key(tenant_id, "membership", uid))
        if change is not None and not isinstance(change.target, MemberTarget):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if change is not None and latest is not None and latest.change == change:
            assert isinstance(change.target, MemberTarget)
            if doc is not None and doc.get("authorization_operation_id") == change.operation_id:
                if (
                    doc.get("authorization_version") != change.authorization_version
                    or doc.get("role") != change.target.role
                    or doc.get("status") not in ("active", "suspended")
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                return change.expected_version
            # Prepared intent with no DB application yet: preserve the original baseline.
            if doc is None and change.expected_version == 0:
                return 0
            if (
                doc is not None
                and doc.get("status") == "revoked"
                and doc.get("authorization_version") == change.expected_version
            ):
                return change.expected_version
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if doc is None and latest is None:
            return 0
        if (
            doc is not None
            and latest is not None
            and latest.state == "committed"
            and isinstance(latest.change.target, MemberTarget)
            and latest.change.target.status == "revoked"
            and doc.get("status") == "revoked"
            and doc.get("authorization_version") == latest.change.authorization_version
            and doc.get("role") == latest.change.target.role
        ):
            return latest.change.authorization_version
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)

    def accept(
        self,
        identity: AuthenticatedIdentity,
        body: InvitationAccept,
        key: str | None,
    ) -> MembershipResult:
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)
        digest = self._digest(body.code)
        actor = fingerprint(["user", identity.user_id])
        claim_path = f"user_operations/{actor}/invitation_acceptances/{fingerprint([key])}"
        attempts_path = f"invitation_attempts/{actor}"

        def claim(tx: Transaction) -> Change | ErrorCode:
            self._tenants.authorize_user(tx, identity)
            now = self._now()
            attempts = tx.get(attempts_path)
            count = 0 if attempts is None or now >= attempts["expires_at"] else attempts["count"]
            if count >= ATTEMPT_LIMIT:
                return ErrorCode.RATE_LIMITED
            expiry = (
                now + ATTEMPT_WINDOW if count == 0 or attempts is None else attempts["expires_at"]
            )
            receipt = tx.get(claim_path)
            locator = tx.get(f"invitation_codes/{digest}")
            invitation = None if locator is None else tx.get(locator["path"])
            result: Change | ErrorCode
            if receipt is not None:
                result = (
                    Change.model_validate(receipt["change"])
                    if receipt["code_hash"] == digest
                    else ErrorCode.IDEMPOTENCY_CONFLICT
                )
            elif (
                invitation is None
                or invitation["code_hash"] != digest
                or invitation["used_operation_id"] is not None
                or invitation["role"] not in ("admin", "member")
            ):
                result = ErrorCode.RESOURCE_NOT_FOUND
            elif now >= invitation["expires_at"]:
                result = ErrorCode.RESOURCE_EXPIRED
            else:
                tenant = ResourceId(invitation["tenant_id"])
                try:
                    if invitation["invitation_epoch"] != self._epoch(tx, tenant):
                        raise APIError(ErrorCode.RESOURCE_EXPIRED)
                    self._issuer(tx, tenant, FirebaseUid(invitation["created_by"]))
                    version = self._eligible(tx, tenant, identity.user_id)
                except (APIError, LedgerError) as error:
                    tx.set(attempts_path, {"count": count + 1, "expires_at": expiry})
                    return (
                        error.code
                        if isinstance(error, APIError)
                        else ErrorCode.TEMPORARILY_UNAVAILABLE
                    )
                result = Change(
                    operation_id=ResourceId(str(uuid4())),
                    tenant_id=tenant,
                    expected_version=version,
                    authorization_version=version + 1,
                    effect="grant",
                    target=MemberTarget(
                        user_id=identity.user_id, role=invitation["role"], status="active"
                    ),
                )
                assert locator is not None
                tx.set(locator["path"], {**invitation, "used_operation_id": result.operation_id})
                tx.create(
                    claim_path,
                    {
                        "code_hash": digest,
                        "invitation_path": locator["path"],
                        "change": result.model_dump(mode="json"),
                        "claimed_at": now,
                    },
                )
            # Return rejected lookups instead of raising: their counters must commit.
            tx.set(attempts_path, {"count": count + 1, "expires_at": expiry})
            return result

        try:
            change = self._store.run(claim)
            if isinstance(change, ErrorCode):
                raise APIError(change)

            def authorize(tx: Transaction, proposed: Change) -> None:
                self._tenants.authorize_user(tx, identity)
                receipt = tx.get(claim_path)
                if (
                    not isinstance(proposed.target, MemberTarget)
                    or proposed.target.role == Role.OWNER
                    or receipt is None
                    or receipt["code_hash"] != digest
                    or Change.model_validate(receipt["change"]) != proposed
                    or proposed.target.user_id != identity.user_id
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                invitation = tx.get(receipt["invitation_path"])
                if (
                    invitation is None
                    or invitation["used_operation_id"] != proposed.operation_id
                    or invitation["tenant_id"] != proposed.tenant_id
                    or invitation["role"] != proposed.target.role
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                if invitation["invitation_epoch"] != self._epoch(tx, proposed.tenant_id):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                self._issuer(tx, proposed.tenant_id, FirebaseUid(invitation["created_by"]))
                if (
                    self._eligible(tx, proposed.tenant_id, identity.user_id, proposed)
                    != proposed.expected_version
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)

            self._changes.execute(change, authorize=authorize)

            def project(tx: Transaction) -> MembershipResult:
                authorize(tx, change)
                policy = transaction_policy(tx, ledger=self._ledger, now=self._now)
                principal = policy_call(
                    lambda: policy.principal(
                        change.tenant_id, UserSubject(user_id=identity.user_id)
                    )
                )
                assert principal.role is not None
                return MembershipResult(
                    user_id=identity.user_id,
                    role=principal.role,
                    version=change.authorization_version,
                )

            return self._store.run(project)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
