"""Explicit local queue adapter with name retention and deterministic crash hooks."""

from collections.abc import Callable
from threading import Lock

from brainoid_jobs import Delivery

from brainoid_worker.entry import CallerDenied, Worker

LOCAL_AUTHORIZATION = "Bearer local-synthetic-worker"


class LocalCaller:
    """Only inject in a local composition root; never selected from request data."""

    def verify(self, authorization: str) -> None:
        if authorization != LOCAL_AUTHORIZATION:
            raise CallerDenied()


class LocalQueue:
    def __init__(self) -> None:
        self.tasks: dict[str, Delivery] = {}
        self.retained_names: set[str] = set()
        self.lock = Lock()
        self.before: Callable[[Delivery], None] = lambda delivery: None
        self.after: Callable[[Delivery], None] = lambda delivery: None

    def enqueue(self, delivery: Delivery) -> None:
        self.before(delivery)
        with self.lock:
            if delivery.task_id not in self.retained_names:
                self.tasks[delivery.task_id] = delivery
                self.retained_names.add(delivery.task_id)
        self.after(delivery)

    def drain(self, worker: Worker) -> int:
        """ACK only after worker returns; failed deliveries remain available for retry."""
        with self.lock:
            deliveries = list(self.tasks.items())
        for name, delivery in deliveries:
            worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
            with self.lock:
                self.tasks.pop(name, None)
        return len(deliveries)
