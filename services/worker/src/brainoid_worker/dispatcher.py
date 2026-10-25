"""Bounded outbox recovery and durable task names; enqueue always runs outside DB tx."""

from dataclasses import dataclass
from typing import Protocol

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document
from brainoid_jobs import Delivery, Jobs
from brainoid_jobs.models import TERMINAL
from pydantic import TypeAdapter, ValidationError

ID = TypeAdapter(ResourceId)


class Queue(Protocol):
    def enqueue(self, delivery: Delivery) -> None:
        """Return on created OR identical existing task name; failures may be ambiguous."""
        ...


class JobPages(Protocol):
    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]: ...


@dataclass(frozen=True)
class RecoveryPage:
    inspected: int
    dispatched: int
    enqueue_errors: int
    next_cursor: str | None


class Dispatcher:
    def __init__(self, jobs: Jobs, pages: JobPages, queue: Queue) -> None:
        self.jobs = jobs
        self.pages = pages
        self.queue = queue

    def dispatch(self, tenant: ResourceId, identity: ResourceId) -> bool:
        delivery = self.jobs.prepare_dispatch(tenant, identity)
        if delivery is None:
            return False
        self.queue.enqueue(delivery)
        self.jobs.dispatched(delivery)
        return True

    def recover(
        self, tenant: ResourceId, *, after: str | None = None, limit: int = 100
    ) -> RecoveryPage:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid recovery limit")
        if after is not None and (not after or "/" in after or len(after.encode()) > 1500):
            raise ValueError("Invalid recovery cursor")
        documents = self.pages.page(f"tenants/{tenant}/jobs", after=after, limit=limit)
        sent = errors = 0
        for document_path, document in documents:
            status = document.get("status")
            if isinstance(status, str) and status in TERMINAL:
                continue
            try:
                identity = ID.validate_python(document_path.rsplit("/", 1)[-1])
            except ValidationError:
                # IDs created through the API are UUIDs. Unknown storage keys are not tasks.
                continue
            try:
                sent += self.dispatch(tenant, identity)
            except QueueUnavailable:
                errors += 1
        cursor = documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None
        return RecoveryPage(len(documents), sent, errors, cursor)


class QueueUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("queue_unavailable")
