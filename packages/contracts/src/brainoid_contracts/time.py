"""Explicit-offset RFC 3339 timestamps and real IANA timezone names."""

import re
from datetime import UTC, datetime
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

from brainoid_contracts.identifiers import _StringValue

# This storage profile preserves Python's microsecond precision without silent truncation.
RFC3339_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)


def normalize_utc(value: object) -> datetime:
    """Validate an aware datetime or RFC 3339 string and return an aware UTC datetime."""
    if isinstance(value, str):
        if re.fullmatch(RFC3339_PATTERN, value) is None or value.endswith("-00:00"):
            raise ValueError("Timestamp requires RFC 3339 with a known UTC offset")
        # fromisoformat accepts overflowed offset minutes, so check the RFC grammar first.
        if value[-1] not in "Zz" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
            raise ValueError("Timestamp offset is invalid")
        try:
            value = datetime.fromisoformat(value.upper())
        except ValueError as exc:
            raise ValueError("Timestamp date or time is invalid") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone-aware datetime")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Timestamp is outside the UTC datetime range") from exc


def format_utc(value: datetime) -> str:
    """Serialize an instant as RFC 3339 UTC, using Z and preserving microseconds."""
    return normalize_utc(value).isoformat().replace("+00:00", "Z")


UtcTimestamp = Annotated[
    datetime,
    BeforeValidator(normalize_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time", "pattern": RFC3339_PATTERN}),
]


class IanaTimezone(_StringValue):
    """Case-sensitive IANA zone key; UTC and IANA aliases are accepted."""

    max_length = 80
    json_format = "iana-timezone"

    def __new__(cls, value: str) -> Self:
        checked = super().__new__(cls, value)
        if checked in {"localtime", "posixrules"} or checked.startswith(("posix/", "right/")):
            raise ValueError("Timezone must be an IANA name, not a host-specific zone")
        try:
            ZoneInfo(checked)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Timezone must be a known IANA name") from exc
        return checked
