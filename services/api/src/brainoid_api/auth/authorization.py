"""HTTP authorization conversions; canonical state reader lives in shared policy."""

from collections.abc import Callable
from datetime import datetime

from brainoid_contracts import normalize_utc
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from brainoid_policy.transactions import (
    TransactionPolicyState as TransactionPolicyState,
)
from brainoid_policy.transactions import (
    transaction_policy as transaction_policy,
)

from brainoid_api.auth.context import SelectedTenantIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.errors import APIError, ErrorCode


def policy_call[T](work: Callable[[], T]) -> T:
    """Translate only content-free policy errors into the common public error contract."""
    try:
        return work()
    except PolicyDenied as error:
        raise APIError(ErrorCode(error.code)) from None


def authorized_scope(grant: ResourceGrant) -> AuthorizedScope:
    """Call inside the current authorization callback, after Evaluator.resource().

    Conversion does not reauthorize a saved receipt. Replays and cursor reads must
    create a new evaluator over their current transaction and call policy again.
    """
    return AuthorizedScope(
        context=grant.context,
        scope=grant.scope,
        owner_user_id=grant.owner_user_id,
        authorization_stamp=(
            grant.principal.stamp
            if grant.output_stamp is None
            else fingerprint([grant.principal.stamp, grant.output_stamp])
        ),
    )


def user_subject(selection: SelectedTenantIdentity, *, now: datetime) -> UserSubject:
    """Only consume IAM-01's verified identity. Selection still requires membership."""
    current = normalize_utc(now)
    if current >= selection.identity.expires_at or current < selection.identity.issued_at:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    return UserSubject(user_id=selection.identity.user_id)
