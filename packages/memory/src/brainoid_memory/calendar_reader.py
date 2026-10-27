"""Bounded Calendar reads; identity and current authority come from the caller."""

import json
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Annotated, Protocol, Self
from zoneinfo import ZoneInfo

from brainoid_contracts import IanaTimezone, ProviderId, ResourceId, UtcTimestamp, normalize_utc
from brainoid_contracts.access import AuthorizedScope, BoundScope, fingerprint
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import GuardedTransactions
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from pydantic import Field, JsonValue, SecretStr, TypeAdapter, model_validator

from brainoid_memory.calendar_oauth import READ, WRITE, SecretStore
from brainoid_memory.models import MemoryValue


class CalendarQuery(MemoryValue):
    calendar_id: ProviderId
    time_min: UtcTimestamp
    time_max: UtcTimestamp
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 20
    cursor: Annotated[str, Field(max_length=2048)] | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not timedelta(0) < self.time_max - self.time_min <= timedelta(days=31):
            raise ValueError("Calendar interval must be positive and at most 31 days")
        return self


class CalendarSelection(MemoryValue):
    allowed_calendar_ids: Annotated[list[ProviderId], Field(max_length=10)]

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.allowed_calendar_ids)) != len(self.allowed_calendar_ids):
            raise ValueError("Duplicate calendar")
        return self


class CalendarDate(MemoryValue):
    date: date


class CalendarDateTime(MemoryValue):
    date_time: UtcTimestamp
    timezone: IanaTimezone


class CalendarEvent(MemoryValue):
    id: ProviderId
    calendar_id: ProviderId
    summary: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    start: CalendarDate | CalendarDateTime
    end: CalendarDate | CalendarDateTime
    etag: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    description: Annotated[str, Field(strict=True, max_length=4000)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    fetched_at: UtcTimestamp

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if isinstance(self.start, CalendarDate) and isinstance(self.end, CalendarDate):
            valid = self.start.date < self.end.date
        elif isinstance(self.start, CalendarDateTime) and isinstance(self.end, CalendarDateTime):
            valid = self.start.date_time < self.end.date_time
        else:
            valid = False
        if not valid:
            raise ValueError("Calendar event endpoints must agree and increase")
        return self


class CalendarPage(MemoryValue):
    items: Annotated[list[CalendarEvent], Field(max_length=100)]
    next_cursor: str | None


class CalendarProvider(Protocol):
    def events(
        self, token: SecretStr, query: CalendarQuery, page_token: str | None
    ) -> Document: ...


class CalendarCursors(Protocol):
    def issue(
        self,
        *,
        position: str,
        query: JsonValue,
        authorize: Callable[[], BoundScope],
        lifetime: timedelta = timedelta(hours=1),
    ) -> str: ...
    def read(self, token: str, *, query: JsonValue, authorize: Callable[[], BoundScope]) -> str: ...


def integration_path(tenant: ResourceId, integration: ResourceId) -> str:
    return f"tenants/{tenant}/integrations/{integration}"


def integration_stamp(record: Document) -> str:
    return fingerprint(
        {
            key: record.get(key)
            for key in (
                "id",
                "tenant_id",
                "owner_user_id",
                "version",
                "binding",
                "authorization_id",
                "granted_scopes",
                "secret_ref",
                "status",
                "deleted",
                "deletion_epoch",
                "allowed_calendar_ids",
                "visibility",
                "display_policy",
            )
        }
    )


def endpoint(raw: object, fallback: object) -> CalendarDate | CalendarDateTime:
    if not isinstance(raw, dict):
        raise ValueError("Invalid event endpoint")
    if "date" in raw:
        if set(raw) != {"date"} or not isinstance(raw["date"], str):
            raise ValueError("Invalid all-day time")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw["date"]) is None:
            raise ValueError("Invalid calendar date")
        return CalendarDate(date=date.fromisoformat(raw["date"]))
    zone = TypeAdapter(IanaTimezone).validate_python(raw.get("timeZone", fallback))
    value = raw.get("dateTime")
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Invalid event time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    timezone = ZoneInfo(zone)
    if parsed.tzinfo is None:
        # Google permits local dateTime only with explicit timeZone. DST gaps and
        # ambiguous folds cannot identify one instant; require an offset there.
        if (
            "timeZone" not in raw
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", value) is None
        ):
            raise ValueError("Missing explicit offset")
        first, second = (
            parsed.replace(tzinfo=timezone, fold=0),
            parsed.replace(tzinfo=timezone, fold=1),
        )
        if first.utcoffset() != second.utcoffset():
            raise ValueError("Ambiguous local time")
        parsed = first
    else:
        normalize_utc(value)
    if parsed.utcoffset() != parsed.astimezone(timezone).utcoffset():
        raise ValueError("Offset and timezone disagree")
    return CalendarDateTime(date_time=parsed, timezone=zone)


def normalize(
    raw: Document, query: CalendarQuery, now: datetime
) -> tuple[list[CalendarEvent], str | None]:
    if (
        not isinstance(raw, dict)
        or "error" in raw
        or not isinstance(raw.get("items"), list)
        or len(raw["items"]) > query.limit
    ):
        raise ValueError("Invalid Calendar page")
    zone = TypeAdapter(IanaTimezone).validate_python(raw.get("timeZone"))
    token = raw.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 384):
        raise ValueError("Invalid Calendar continuation")
    events = []
    for item in raw["items"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid event")
        if item.get("status") == "cancelled":
            continue
        events.append(
            CalendarEvent(
                id=item["id"],
                calendar_id=query.calendar_id,
                summary=item.get("summary") or "予定",
                start=endpoint(item["start"], zone),
                end=endpoint(item["end"], zone),
                etag=item["etag"],
                description=item.get("description"),
                fetched_at=now,
            )
        )
    return events, token


class CalendarReader:
    def __init__(
        self,
        store: TransactionStore,
        provider: CalendarProvider,
        secrets: SecretStore,
        cursors: CalendarCursors,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self.store, self.provider, self.secrets, self.cursors, self.now = (
            GuardedTransactions(store),
            provider,
            secrets,
            cursors,
            now,
        )

    def fetch(
        self,
        record: Document,
        query: CalendarQuery,
        page_token: str | None,
        before_send: Callable[[], None],
    ) -> Document:
        try:
            secret = json.loads(self.secrets.get(record["secret_ref"]))
            if (
                secret["integration_id"] != record["id"]
                or secret["binding"] != record["binding"]
                or secret["granted_scopes"] != record["granted_scopes"]
                or not set(secret["granted_scopes"]).intersection((READ, WRITE))
            ):
                raise ValueError("Invalid Calendar grant")
            token = SecretStr(secret["access_token"])
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        before_send()
        try:
            return self.provider.events(token, query, page_token)
        except APIError:
            raise
        except Exception:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def list(
        self,
        authorize: Callable[[Transaction], tuple[Document, AuthorizedScope]],
        query: CalendarQuery,
    ) -> CalendarPage:
        record, scope = self.store.run(authorize)
        params = {
            **query.model_dump(mode="json", exclude={"cursor"}),
            "integration": record["id"],
            "generation": integration_stamp(record),
            "operation": "calendar.events",
        }
        number, token = 1, None
        if query.cursor is not None:
            try:
                position = json.loads(
                    self.cursors.read(query.cursor, query=params, authorize=lambda: scope)
                )
                number, token = position["page"], position["token"]
                if type(number) is not int or not 2 <= number <= 10 or not isinstance(token, str):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise APIError(ErrorCode.INVALID_REQUEST) from None

        def before_send() -> None:
            latest, latest_scope = self.store.run(authorize)
            if (
                integration_stamp(latest) != integration_stamp(record)
                or latest_scope.binding() != scope.binding()
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)

        raw = self.fetch(record, query, token, before_send)
        try:
            events, next_token = normalize(raw, query, self.now())
            if next_token == token and next_token is not None:
                raise ValueError("Repeating provider cursor")
            if number == 10 and next_token is not None:
                raise ValueError("Calendar paging budget exceeded")
        except (ValueError, KeyError, TypeError):
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        current, current_scope = self.store.run(authorize)
        if (
            integration_stamp(current) != integration_stamp(record)
            or current_scope.binding() != scope.binding()
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        current_scope.ensure_live(self.now())
        try:
            cursor = (
                None
                if next_token is None
                else self.cursors.issue(
                    position=json.dumps(
                        {"page": number + 1, "token": next_token}, separators=(",", ":")
                    ),
                    query=params,
                    authorize=lambda: current_scope,
                    lifetime=timedelta(minutes=5),
                )
            )
        except ValueError:
            # Opaque provider tokens can expand when encoded into the shared
            # position/wire budgets. Fail the whole page before exposing events.
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        return CalendarPage(items=events, next_cursor=cursor)
