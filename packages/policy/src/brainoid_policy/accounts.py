"""Current account authority, including explicitly unprovisioned Firebase identities."""

import hashlib
import json

from brainoid_contracts import FirebaseUid
from brainoid_contracts.accounts import UserProfile
from brainoid_contracts.transactions import Transaction
from pydantic import ValidationError


class AccountUnavailable(Exception):
    def __init__(self, *, malformed: bool = False) -> None:
        self.malformed = malformed
        super().__init__("account_unavailable")


def fingerprint(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def account_stamp(tx: Transaction, user_id: FirebaseUid) -> str:
    documents = tx.find("users", "user_id", user_id)
    if len(documents) > 1:
        raise AccountUnavailable(malformed=True)
    if not documents:
        return fingerprint(["unprovisioned", user_id])
    document = documents[0]
    try:
        profile = UserProfile.model_validate(
            {field: document.get(field) for field in UserProfile.model_fields}
        )
    except ValidationError:
        raise AccountUnavailable(malformed=True) from None
    if profile.user_id != user_id or profile.status != "active":
        raise AccountUnavailable()
    return fingerprint(
        [profile.user_id, profile.status, profile.version, document.get("deletion_epoch", 0)]
    )
