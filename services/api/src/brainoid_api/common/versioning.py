"""Strong ETags and atomic compare/update/delete using the caller's transaction."""

import re

from brainoid_contracts import Version
from pydantic import TypeAdapter

from brainoid_api.common.access import AuthorizedScope
from brainoid_api.common.transactions import Document, Transaction
from brainoid_api.errors import APIError, ErrorCode

_version = TypeAdapter(Version)


def etag(version: int) -> str:
    return f'"{_version.validate_python(version)}"'


def require_if_match(value: str | None) -> int:
    if value is None:
        raise APIError(ErrorCode.VERSION_REQUIRED)
    if re.fullmatch(r'"[1-9][0-9]*"', value) is None:
        raise APIError(ErrorCode.INVALID_REQUEST)
    try:
        return int(value[1:-1])
    except ValueError:
        raise APIError(ErrorCode.INVALID_REQUEST) from None


def compare_and_write(
    transaction: Transaction,
    *,
    authorized_scope: AuthorizedScope,
    path: str,
    if_match: str | None,
    replacement: Document | None,
) -> int:
    """Replace the complete resource (or delete with None) in this transaction.

    The caller must authorize this specific existing resource and requested transition
    in the same transaction. This helper checks the tenant and version, not business
    permissions or permitted fields. Compose with Idempotency.execute for replay.
    Returns the next version; a deleted document does not persist a new version.
    """
    authorized_scope.require_tenant_path(path)
    expected = require_if_match(if_match)
    current = transaction.get(path)
    if current is None:
        raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
    version = _version.validate_python(current["version"])
    if expected != version:
        raise APIError(ErrorCode.VERSION_CONFLICT)
    if replacement is None:
        transaction.delete(path)
    else:
        transaction.set(path, {**replacement, "version": version + 1})
    return version + 1
