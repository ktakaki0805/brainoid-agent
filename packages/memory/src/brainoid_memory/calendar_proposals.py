"""Immutable Calendar intents. Provider reads never occur inside a DB transaction."""

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Literal, Protocol, Self
from uuid import uuid4

from brainoid_contracts import ProviderId, ResourceId, UtcTimestamp, normalize_utc
from brainoid_contracts.access import AuthorizedScope, fingerprint
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import ReleaseGuard, release_guard
from brainoid_contracts.idempotency import Idempotency, MutationResult
from brainoid_contracts.transactions import Document, Transaction
from pydantic import Field, TypeAdapter, model_validator

from brainoid_memory.calendar_oauth import WRITE
from brainoid_memory.calendar_reader import (
    CalendarDate,
    CalendarDateTime,
    CalendarQuery,
    CalendarReader,
    endpoint,
    integration_stamp,
    normalize,
)
from brainoid_memory.models import MemoryValue


class EventContent(MemoryValue):
    calendar_id: ProviderId
    summary: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    start: CalendarDate | CalendarDateTime
    end: CalendarDate | CalendarDateTime
    description: Annotated[str, Field(strict=True, max_length=4000)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if isinstance(self.start, CalendarDate) and isinstance(self.end, CalendarDate):
            valid = self.start.date < self.end.date
        elif isinstance(self.start, CalendarDateTime) and isinstance(self.end, CalendarDateTime):
            valid = self.start.date_time < self.end.date_time
        else:
            valid = False
        if not valid:
            raise ValueError("Calendar endpoints must agree and increase")
        return self


class CreateEvent(EventContent):
    action: Literal["create"]


class EventTarget(MemoryValue):
    calendar_id: ProviderId
    event_id: ProviderId
    provider_etag: Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class UpdateEvent(EventContent, EventTarget):
    action: Literal["update"]


class DeleteEvent(EventTarget):
    action: Literal["delete"]


type Change = Annotated[CreateEvent | UpdateEvent | DeleteEvent, Field(discriminator="action")]
CHANGE = TypeAdapter(Change)


class ProposalView(MemoryValue):
    proposal_id: ResourceId
    integration_id: ResourceId
    action: Literal["create", "update", "delete"]
    normalized_request: Change
    args_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    expires_at: UtcTimestamp
    status: Literal[
        "pending",
        "confirmed",
        "executing",
        "succeeded",
        "failed",
        "outcome_unknown",
        "expired",
        "declined",
        "cancelled",
    ]
    run_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    tool_call_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    checkpoint_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)


def proposal_path(tenant: ResourceId, proposal: ResourceId) -> str:
    return f"tenants/{tenant}/calendar_proposals/{proposal}"


def immutable(record: Document) -> Document:
    return {
        key: record.get(key)
        for key in (
            "id",
            "tenant_id",
            "owner_user_id",
            "integration_id",
            "normalized_request",
            "binding",
            "granted_scopes",
            "calendar_source",
            "created_at",
            "expires_at",
            "origin",
            "run_id",
            "tool_call_id",
            "checkpoint_id",
            "checkpoint_version",
        )
    }


def validate_proposal(record: Document) -> None:
    try:
        CHANGE.validate_python(record["normalized_request"])
        if fingerprint(immutable(record)) != record.get("args_hash") or normalize_utc(
            record["expires_at"]
        ) - normalize_utc(record["created_at"]) != timedelta(minutes=5):
            raise ValueError("Invalid immutable intent")
    except (ValueError, KeyError, TypeError):
        raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None


def view(record: Document) -> ProposalView:
    validate_proposal(record)
    return ProposalView.model_validate(
        {
            "proposal_id": record["id"],
            "integration_id": record["integration_id"],
            "action": record["normalized_request"]["action"],
            **{
                key: record[key]
                for key in (
                    "normalized_request",
                    "args_hash",
                    "expires_at",
                    "status",
                    "run_id",
                    "tool_call_id",
                    "checkpoint_id",
                )
                if record.get(key) is not None
            },
        }
    )


type Authorize = Callable[[Transaction], tuple[Document, AuthorizedScope]]


class ProposalPages(Protocol):
    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]: ...


class CalendarProposals:
    def __init__(self, reader: CalendarReader) -> None:
        self.reader, self.store = reader, reader.store

    def read(self, tx: Transaction, tenant: ResourceId, identity: ResourceId) -> Document:
        record = tx.get(proposal_path(tenant, identity))
        if record is None:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        record = fenced_document(tx, tenant, "calendar_proposals", identity, record)
        validate_proposal(record)
        if record.get("deleted") is not False or record.get("suppress_reuse"):
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return record

    @staticmethod
    def authorized(record: Document, integration: Document, scope: AuthorizedScope) -> None:
        if (
            record["tenant_id"] != scope.context.tenant_id
            or record["owner_user_id"] != scope.owner_user_id
            or record["integration_id"] != integration["id"]
            or record["calendar_source"]
            != {
                "integration_id": integration["id"],
                "calendar_id": record["normalized_request"]["calendar_id"],
                "stamp": integration_stamp(integration),
                "operation": "calendar.propose",
                "proposal_id": record["id"],
            }
            or WRITE not in integration["granted_scopes"]
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)

    def create(
        self,
        authorize: Authorize,
        change: Change,
        key: str | None,
        *,
        verify_origin: Callable[[Transaction, Document], None],
        identity: ResourceId | None = None,
        provenance: Callable[[Transaction], Document] = lambda tx: {},
        retain: Callable[[Transaction, Document], None] = lambda tx, doc: None,
    ) -> Document:
        record, guard = self.create_for_release(
            authorize,
            change,
            key,
            verify_origin=verify_origin,
            identity=identity,
            provenance=provenance,
            retain=retain,
        )
        guard.finish()
        return record

    def create_for_release(
        self,
        authorize: Authorize,
        change: Change,
        key: str | None,
        *,
        verify_origin: Callable[[Transaction, Document], None],
        identity: ResourceId | None = None,
        provenance: Callable[[Transaction], Document] = lambda tx: {},
        retain: Callable[[Transaction, Document], None] = lambda tx, doc: None,
    ) -> tuple[Document, ReleaseGuard]:
        change = CHANGE.validate_python(change.model_dump(mode="json"))
        initial, _ = self.store.run(authorize)
        integration_id = initial["id"]
        proposal = identity or ResourceId(str(uuid4()))
        normalized = change.model_dump(mode="json")
        captured: tuple[Document, AuthorizedScope] | None = None
        captured_origin: Document | None = None
        verified = False

        class Probe(Exception):
            pass

        def access(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            integration, scope = authorize(tx)
            if WRITE not in integration["granted_scopes"]:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if replay is not None:
                assert replay.resource_id is not None
                record = self.read(tx, scope.context.tenant_id, replay.resource_id)
                self.authorized(record, integration, scope)
                verify_origin(tx, record)
            return scope

        def mutate(tx: Transaction, scope: AuthorizedScope) -> MutationResult:
            nonlocal captured, captured_origin
            integration, current = authorize(tx)
            if not verified:
                captured = integration, current
                captured_origin = provenance(tx)
                raise Probe()
            assert captured is not None
            if (
                integration_stamp(integration) != integration_stamp(captured[0])
                or current.binding() != captured[1].binding()
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            origin = provenance(tx)
            if origin != captured_origin:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            now = self.reader.now()
            record = {
                "id": proposal,
                "tenant_id": scope.context.tenant_id,
                "owner_user_id": scope.owner_user_id,
                "integration_id": integration["id"],
                "normalized_request": normalized,
                "binding": scope.binding(),
                "granted_scopes": integration["granted_scopes"],
                "calendar_source": {
                    "integration_id": integration["id"],
                    "calendar_id": change.calendar_id,
                    "stamp": integration_stamp(integration),
                    "operation": "calendar.propose",
                    "proposal_id": proposal,
                },
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "status": "pending",
                "version": 1,
                "deletion_epoch": 0,
                "deleted": False,
                "visibility": "private",
                "display_policy": "authenticated_session",
                **origin,
            }
            record["args_hash"] = fingerprint(immutable(record))
            verify_origin(tx, record)
            retain(tx, record)
            tx.create(proposal_path(scope.context.tenant_id, proposal), record)
            return MutationResult(status_code=201, resource_id=proposal, version=1)

        def execute() -> MutationResult:
            return Idempotency(self.store, now=self.reader.now).execute(
                operation="calendar.propose:" + integration_id,
                key=key,
                body={"integration_id": integration_id, "change": normalized, "identity": identity},
                authorize=access,
                mutate=mutate,
            )

        try:
            result = execute()
        except Probe:
            assert captured is not None
            integration, scope = captured

            def before_send() -> None:
                def probe(tx: Transaction) -> tuple[Document, AuthorizedScope]:
                    current = authorize(tx)
                    if provenance(tx) != captured_origin:
                        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                    return current

                latest, current = self.store.run(probe)
                if (
                    integration_stamp(latest) != integration_stamp(integration)
                    or current.binding() != scope.binding()
                ):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)

            try:
                now = self.reader.now()
                query = CalendarQuery(
                    calendar_id=change.calendar_id,
                    time_min=now,
                    time_max=now + timedelta(seconds=1),
                    limit=1,
                )
                acl = self.reader.fetch(integration, query, None, before_send)
                try:
                    normalize(acl, query, self.reader.now())
                except (ValueError, KeyError, TypeError):
                    raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
                before_send()
                if acl.get("accessRole") not in {"writer", "owner", "writerWithoutPrivateAccess"}:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                if isinstance(change, EventTarget):
                    if acl.get("accessRole") == "writerWithoutPrivateAccess":
                        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                    raw = self.reader.target(
                        integration, change.calendar_id, change.event_id, before_send
                    )
                    if raw.get("id") != change.event_id or raw.get("etag") != change.provider_etag:
                        raise APIError(ErrorCode.VERSION_CONFLICT)
                    if (
                        raw.get("status") == "cancelled"
                        or raw.get("attendees")
                        or raw.get("recurrence")
                        or raw.get("recurringEventId")
                        or raw.get("attendeesOmitted")
                        or raw.get("locked")
                        or raw.get("eventType", "default") != "default"
                    ):
                        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                    try:
                        EventContent(
                            calendar_id=change.calendar_id,
                            summary=raw.get("summary") or "予定",
                            start=endpoint(raw["start"], "UTC"),
                            end=endpoint(raw["end"], "UTC"),
                        )
                    except (ValueError, KeyError, TypeError):
                        raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
                before_send()
            except APIError as failure:
                try:
                    result = execute()
                except Probe:
                    raise failure from None
            else:
                verified = True
                result = execute()
        assert result.resource_id is not None

        def get(tx: Transaction) -> tuple[Document, ReleaseGuard]:
            integration, scope = authorize(tx)
            record = self.read(tx, scope.context.tenant_id, result.resource_id)  # type: ignore[arg-type]
            self.authorized(record, integration, scope)
            verify_origin(tx, record)
            return record, release_guard(tx)

        return self.store.run(get)

    def expire_unsuspended(
        self,
        tenant: ResourceId,
        pages: ProposalPages,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> str | None:
        """Bounded cleanup for standalone intents and the pre-suspension crash window."""
        if not 1 <= limit <= 100:
            raise ValueError("Invalid proposal expiry limit")
        records = pages.page(f"tenants/{tenant}/calendar_proposals", after=after, limit=limit)
        for path, _ in records:

            def expire(tx: Transaction, path: str = path) -> None:
                record = tx.get(path)
                if (
                    record is not None
                    and (
                        record.get("run_id") is None
                        or tx.get(f"tenants/{tenant}/run_proposals/{record['id']}") is None
                    )
                    and record.get("status") == "pending"
                    and normalize_utc(record["expires_at"]) <= self.reader.now()
                ):
                    validate_proposal(record)
                    tx.set(path, {**record, "status": "expired", "version": record["version"] + 1})

            self.store.run(expire)
        return records[-1][0].rsplit("/", 1)[-1] if len(records) == limit else None
