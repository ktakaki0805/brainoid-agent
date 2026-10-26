"""Framework-free public error categories shared by DB mutation adapters."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    VALIDATION_FAILED = "validation_failed"
    AUTHENTICATION_REQUIRED = "authentication_required"
    REAUTH_REQUIRED = "reauth_required"
    OPERATION_FORBIDDEN = "operation_forbidden"
    RESOURCE_NOT_FOUND = "resource_not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    OWNERSHIP_TRANSFER_REQUIRED = "ownership_transfer_required"
    PAIRING_PENDING = "pairing_pending"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RESOURCE_EXPIRED = "resource_expired"
    VERSION_CONFLICT = "version_conflict"
    VERSION_REQUIRED = "version_required"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMITED = "rate_limited"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INTERNAL_ERROR = "internal_error"


class APIError(Exception):
    """Only catalogued public messages are accepted; private details stay out of responses."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)
