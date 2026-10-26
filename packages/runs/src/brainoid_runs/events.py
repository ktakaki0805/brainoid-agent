"""Durable public text events; no provider continuation or tool candidates."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from brainoid_contracts import ResourceId, UtcTimestamp
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.models import Record
from pydantic import Field, TypeAdapter

HISTORY_LIFETIME = timedelta(hours=24)
_timestamp = TypeAdapter(UtcTimestamp)


class StartedPayload(Record):
    pass


class TextPayload(Record):
    text: Annotated[str, Field(min_length=1, max_length=2000, repr=False)]


class CompletedPayload(Record):
    message_id: ResourceId


class FailedPayload(Record):
    error_code: Annotated[str, Field(min_length=1, max_length=80)]
    retryable: bool = False


class Event(Record):
    run_id: ResourceId
    seq: Annotated[int, Field(strict=True, ge=1)]
    occurred_at: UtcTimestamp


class Started(Event):
    type: Literal["run.started"] = "run.started"
    payload: StartedPayload = Field(default_factory=StartedPayload)


class Text(Event):
    type: Literal["text.delta"] = "text.delta"
    payload: TextPayload


class Completed(Event):
    type: Literal["run.completed"] = "run.completed"
    payload: CompletedPayload


class Failed(Event):
    type: Literal["run.failed"] = "run.failed"
    payload: FailedPayload


type RunEvent = Annotated[Started | Text | Completed | Failed, Field(discriminator="type")]
EVENT = TypeAdapter(RunEvent)


def expires_at(run: Document) -> datetime:
    # Fixed at admission, including runs with zero events. Polls never extend it.
    return _timestamp.validate_python(run["created_at"]) + HISTORY_LIFETIME


def event_path(run: Document, seq: int) -> str:
    return f"tenants/{run['tenant_id']}/runs/{run['run_id']}/events/{seq}"


def append(tx: Transaction, run: Document, event: Event) -> None:
    """Write-only phase: caller read/authorized the run in this transaction first."""
    if event.run_id != run["run_id"] or event.seq != run.get("last_event_seq", 0) + 1:
        raise ValueError("Invalid event sequence")
    tx.create(
        event_path(run, event.seq), {**event.model_dump(mode="json"), "expires_at": expires_at(run)}
    )
    run["last_event_seq"] = event.seq
    run["events_version"] = 1


def chunks(text: str) -> list[str]:
    """At most 1 KiB UTF-8 per event, preserving Unicode code points."""
    result: list[str] = []
    current = ""
    size = 0
    for character in text:
        width = len(character.encode("utf-8"))
        if size + width > 1024:
            result.append(current)
            current, size = "", 0
        current += character
        size += width
    if current:
        result.append(current)
    return result


def append_text(tx: Transaction, run: Document, text: str, now: datetime) -> None:
    for part in chunks(text):
        append(
            tx,
            run,
            Text(
                run_id=run["run_id"],
                seq=run.get("last_event_seq", 0) + 1,
                occurred_at=now,
                payload=TextPayload(text=part),
            ),
        )
    run["streamed_text"] = run.get("streamed_text", "") + text


def save_projection(tx: Transaction, original: Document, updated: Document, now: datetime) -> None:
    if updated == original:
        return
    append(
        tx,
        updated,
        Failed(
            run_id=updated["run_id"],
            seq=updated.get("last_event_seq", 0) + 1,
            occurred_at=now,
            payload=FailedPayload(error_code=updated["error_code"]),
        ),
    )
    tx.set(f"tenants/{updated['tenant_id']}/runs/{updated['run_id']}", updated)
