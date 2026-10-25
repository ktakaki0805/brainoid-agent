"""HTTP error compatibility for the shared account authorization check."""

from brainoid_contracts import FirebaseUid
from brainoid_contracts.transactions import Transaction
from brainoid_policy.accounts import AccountUnavailable
from brainoid_policy.accounts import account_stamp as policy_account_stamp

from brainoid_api.errors import APIError, ErrorCode


def account_stamp(tx: Transaction, user_id: FirebaseUid) -> str:
    try:
        return policy_account_stamp(tx, user_id)
    except AccountUnavailable as error:
        code = ErrorCode.INTERNAL_ERROR if error.malformed else ErrorCode.OPERATION_FORBIDDEN
        raise APIError(code) from None
