"""User-token binding for export admission/status/download, with no lifecycle bypass."""

from collections.abc import Callable

from brainoid_contracts import ResourceId
from brainoid_contracts.guards import ReleaseGuard, protect
from brainoid_contracts.transactions import Transaction
from brainoid_memory.export_manifest import (
    Authorize,
    ExportCreate,
    ObjectRevoked,
    ObjectUnavailable,
)
from brainoid_memory.exports import Exports
from brainoid_memory.forget import Operation
from brainoid_memory.repository import VerifiedCaller
from brainoid_policy.models import UserSubject

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.memories import policy_call
from brainoid_api.repositories.tenants import Tenants


class ExportRequests:
    def __init__(self, exports: Exports, tenants: Tenants) -> None:
        self.exports, self.tenants = exports, tenants

    def authorizer(self, identity: AuthenticatedIdentity) -> Authorize:
        def authorize(tx: Transaction, tenant: ResourceId) -> VerifiedCaller:
            if not isinstance(identity, AuthenticatedIdentity):
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
            scope = self.tenants.authorize_user(tx, identity)
            protect(tx, lambda: scope.ensure_live(self.exports.now()))
            return VerifiedCaller(
                subject=UserSubject(user_id=identity.user_id),
                valid_from=identity.issued_at,
                valid_to=identity.expires_at,
            )

        return authorize

    def create(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        body: ExportCreate,
        key: str | None,
    ) -> Operation:
        return policy_call(
            lambda: self.exports.create(tenant, body, key, self.authorizer(identity))
        )

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, operation: ResourceId
    ) -> Operation:
        return policy_call(lambda: self.exports.get(tenant, operation, self.authorizer(identity)))

    def guarded[T](self, operation: ResourceId, work: Callable[[], T]) -> T:
        try:
            return policy_call(work)
        except ObjectUnavailable:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        except ObjectRevoked:
            self.exports.revoke(operation)
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND) from None
        except APIError:
            # Only independent canonical invalidity permits erasure. An expired or
            # wrong user's token must not revoke another user's still-valid export.
            self.exports.cleanup_one(operation)
            raise

    def download(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, operation: ResourceId
    ) -> bytes:
        return self.guarded(
            operation, lambda: self.exports.download(tenant, operation, self.authorizer(identity))
        )

    def release(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, operation: ResourceId
    ) -> ReleaseGuard:
        return self.guarded(
            operation,
            lambda: self.exports.validate(tenant, operation, self.authorizer(identity))[1],
        )
