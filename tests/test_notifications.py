"""APP-04 actual scanner/dispatcher/worker, strict reads-before-writes fake."""

import copy
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs.models import job_id
from brainoid_memory.notifications import KIND, notification_path
from brainoid_persistence.firestore import PersistenceUnavailable
from fakes import notification_scenarios
from fakes.memory_repository import TENANT, GraphStore


class NotificationStore(GraphStore):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.RLock()

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        with self.lock:
            return super().run(work)

    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]:
        with self.lock:
            return [
                (p, copy.deepcopy(d))
                for p, d in sorted(self.data.items())
                if p.rsplit("/", 1)[0] == collection
                and (after is None or p.rsplit("/", 1)[-1] > after)
            ][:limit]


class NotificationTests(notification_scenarios.NotificationScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_notifications()

    def test_lost_atomic_scan_commit_and_reclaimed_lease_do_not_duplicate(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        lost: list[bool] = []

        def after() -> None:
            if not lost and notification_path(TENANT, self.identity(record)) in self.store.data:
                lost.append(True)
                raise PersistenceUnavailable()

        self.store.after_commit = after
        with self.assertRaises(PersistenceUnavailable):
            self.scanner.scan(TENANT)
        self.store.after_commit = lambda: None
        self.assertTrue(self.scanner.scan(TENANT).busy)
        self.clock.advance(60)
        self.assertEqual(self.scanner.scan(TENANT).created, 0)
        self.deliver()
        self.assertEqual(self.job(record).status, "succeeded")
        self.assertEqual(len(self.provider.calls), 1)

    def test_uncommitted_scan_creation_rolls_back_notification_and_job(self) -> None:
        record = self.reminder(due_at=self.clock.now())
        claim = self.scanner._claim

        def fail(*args: Any, **kwargs: Any) -> int:
            claim(*args, **kwargs)
            raise PersistenceUnavailable()

        with patch.object(self.scanner, "_claim", side_effect=fail):
            with self.assertRaises(PersistenceUnavailable):
                self.scanner.scan(TENANT)
        self.assertNotIn(notification_path(TENANT, self.identity(record)), self.store.data)
        self.assertIsNone(self.jobs.get(TENANT, job_id(TENANT, KIND, self.identity(record))))
        self.clock.advance(60)
        self.assertEqual(self.scanner.scan(TENANT).created, 1)
