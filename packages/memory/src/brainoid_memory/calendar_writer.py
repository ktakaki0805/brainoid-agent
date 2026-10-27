"""Fixed Calendar mutation wire contract and conservative reconciliation evidence."""

from dataclasses import dataclass
from typing import Literal, Protocol

from brainoid_contracts.access import fingerprint
from brainoid_contracts.transactions import Document
from pydantic import SecretStr

from brainoid_memory.calendar_proposals import CHANGE, DeleteEvent, EventContent
from brainoid_memory.calendar_reader import CalendarDate, CalendarDateTime, endpoint

KIND: Literal["calendar_write"] = "calendar_write"


@dataclass(frozen=True)
class ProviderReply:
    status: int
    body: Document | None = None


class CalendarWriter(Protocol):
    def inspect(self, token: SecretStr, calendar: str, event: str) -> ProviderReply: ...
    def write(
        self,
        token: SecretStr,
        action: str,
        calendar: str,
        event: str,
        etag: str | None,
        body: Document | None,
    ) -> ProviderReply: ...


def event_id(record: Document) -> str:
    change = record["normalized_request"]
    # Hex is a subset of Google's base32hex; the operation/proposal is stable.
    return change.get("event_id") or "b" + fingerprint(
        [record["tenant_id"], record["operation_id"]]
    )


def marker(record: Document) -> str:
    return fingerprint([record["tenant_id"], record["operation_id"], record["args_hash"]])


def body(record: Document, original: Document | None = None) -> Document | None:
    change = CHANGE.validate_python(record["normalized_request"])
    if isinstance(change, DeleteEvent):
        return None

    def point(value: CalendarDate | CalendarDateTime) -> Document:
        return (
            {"date": value.date.isoformat()}
            if isinstance(value, CalendarDate)
            else {"dateTime": value.date_time.isoformat(), "timeZone": value.timezone}
        )

    private = dict((original or {}).get("extendedProperties", {}).get("private", {}))
    private["brainoidOperation"] = marker(record)
    result = {
        "summary": change.summary,
        "description": change.description or "",
        "start": point(change.start),
        "end": point(change.end),
        "extendedProperties": {"private": private},
    }
    if change.action == "create":
        result["id"] = event_id(record)
    return result


def supported(raw: Document) -> bool:
    return not (
        raw.get("status") == "cancelled"
        or raw.get("attendees")
        or raw.get("recurrence")
        or raw.get("recurringEventId")
        or raw.get("attendeesOmitted")
        or raw.get("locked")
        or raw.get("eventType", "default") != "default"
    )


def matches(record: Document, reply: ProviderReply) -> bool:
    """An ID collision, disappearance, or a matching title alone is never success."""
    raw = reply.body
    if reply.status != 200 or raw is None or not supported(raw):
        return False
    change = CHANGE.validate_python(record["normalized_request"])
    if isinstance(change, DeleteEvent):
        return False  # Google tombstones do not guarantee preservation of app metadata.
    if (
        raw.get("id") != event_id(record)
        or raw.get("extendedProperties", {}).get("private", {}).get("brainoidOperation")
        != marker(record)
        or not isinstance(raw.get("etag"), str)
        or raw.get("etag") == record["normalized_request"].get("provider_etag")
    ):
        return False
    try:
        actual = EventContent(
            calendar_id=change.calendar_id,
            summary=raw["summary"],
            description=raw.get("description") or None,
            start=endpoint(raw["start"], "UTC"),
            end=endpoint(raw["end"], "UTC"),
        )
        intended = EventContent.model_validate(
            {
                key: value
                for key, value in change.model_dump(mode="json").items()
                if key in EventContent.model_fields
            }
        )
        return actual == intended
    except (ValueError, KeyError, TypeError):
        return False
