"""Current account authority, including explicitly unprovisioned Firebase identities."""

from brainoid_contracts import FirebaseUid
from pydantic import ValidationError

from brainoid_api.auth.profiles import UserProfile
from brainoid_api.common.access import fingerprint
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode


def account_stamp(tx: Transaction, user_id: FirebaseUid) -> str:
    documents = tx.find("users", "user_id", user_id)
    if len(documents) > 1:
        raise APIError(ErrorCode.INTERNAL_ERROR)
    if not documents:
        return fingerprint(["unprovisioned", user_id])
    document = documents[0]
    try:
        profile = UserProfile.model_validate(
            {field: document.get(field) for field in UserProfile.model_fields}
        )
    except ValidationError:
        raise APIError(ErrorCode.INTERNAL_ERROR) from None
    if profile.user_id != user_id or profile.status != "active":
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    return fingerprint(
        [profile.user_id, profile.status, profile.version, document.get("deletion_epoch", 0)]
    )
