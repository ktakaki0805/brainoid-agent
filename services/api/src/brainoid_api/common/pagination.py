"""Signed opaque cursors bound to the currently authorized query and resource scope."""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import datetime, timedelta

from brainoid_contracts import normalize_utc
from pydantic import JsonValue

from brainoid_api.common.access import AuthorizedScope, canonical_json, fingerprint
from brainoid_api.errors import APIError, ErrorCode


def page_limit(value: int | None = None) -> int:
    if value is None:
        return 20
    if type(value) is not int or not 1 <= value <= 100:
        raise APIError(ErrorCode.INVALID_REQUEST)
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(decoded) != value:
        raise ValueError("Noncanonical encoding")
    return decoded


class Cursors:
    """The caller supplies a secret shared across API replicas, never a default key.

    Signing prevents edits; it does not encrypt. Positions must contain only opaque
    resource IDs/order keys, never private text or credentials. Rotating the signing
    key invalidates old cursors. `query` includes operation, filters, order and limit.
    """

    def __init__(self, signing_key: bytes, *, now: Callable[[], datetime]) -> None:
        if len(signing_key) < 32:
            raise ValueError("Cursor signing requires at least 32 secret bytes")
        self._key = signing_key
        self._now = now

    def issue(
        self,
        *,
        position: str,
        query: JsonValue,
        authorize: Callable[[], AuthorizedScope],
        lifetime: timedelta = timedelta(hours=1),
    ) -> str:
        current = authorize()
        now = normalize_utc(self._now())
        current.ensure_live(now)
        if (
            not position
            or len(position) > 512
            or not timedelta(0) < lifetime <= timedelta(hours=24)
        ):
            raise ValueError("Invalid cursor position or lifetime")
        payload = _encode(
            canonical_json(
                {
                    "v": 1,
                    "scope": current.binding(),
                    "query": fingerprint(query),
                    "position": position,
                    "expires": (now + lifetime).timestamp(),
                }
            ).encode("ascii")
        )
        token = payload + "." + self._signature(payload)
        if len(token) > 2048:
            raise ValueError("Cursor exceeds the wire limit")
        return token

    def read(
        self,
        token: str,
        *,
        query: JsonValue,
        authorize: Callable[[], AuthorizedScope],
    ) -> str:
        current = authorize()
        now = normalize_utc(self._now())
        current.ensure_live(now)
        try:
            if len(token) > 2048:
                raise ValueError
            payload, signature = token.split(".")
            if not hmac.compare_digest(signature, self._signature(payload)):
                raise ValueError
            data = json.loads(_decode(payload))
            if (
                data["v"] != 1
                or data["scope"] != current.binding()
                or data["query"] != fingerprint(query)
                or now.timestamp() >= data["expires"]
                or not isinstance(data["position"], str)
                or not 1 <= len(data["position"]) <= 512
            ):
                raise ValueError
            return str(data["position"])
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError):
            raise APIError(ErrorCode.INVALID_REQUEST) from None

    def _signature(self, payload: str) -> str:
        return _encode(
            hmac.new(self._key, b"cursor:v1:" + payload.encode("ascii"), hashlib.sha256).digest()
        )
