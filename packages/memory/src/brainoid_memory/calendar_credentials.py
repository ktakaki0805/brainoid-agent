"""Provider credential failures are distinct from application authentication."""

from typing import Literal

from brainoid_contracts.errors import APIError, ErrorCode

type ReauthReason = Literal["credentials_expired_or_revoked", "permission_revoked"]


class CalendarCredentialsRejected(APIError):
    def __init__(self, reason: ReauthReason) -> None:
        super().__init__(ErrorCode.OPERATION_FORBIDDEN)
        self.reason = reason


def credential_failure(status: int, body: object = None) -> ReauthReason | None:
    if status == 401:
        return "credentials_expired_or_revoked"
    if status == 403 and isinstance(body, dict):
        error = body.get("error")
        errors = error.get("errors") if isinstance(error, dict) else None
        if (
            isinstance(errors, list)
            and errors
            and all(
                isinstance(item, dict) and item.get("reason") == "insufficientPermissions"
                for item in errors
            )
        ):
            return "permission_revoked"
    return None
