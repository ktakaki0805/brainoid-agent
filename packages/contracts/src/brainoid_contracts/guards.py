"""Transaction-local no-I/O guards retained through writes and delayed responses.

Checks only use already verified evidence and an injected clock. Database reads,
provider calls and new grants do not belong in a guard. A retry gets a fresh guard
set; a failed post-commit guard never claims that durable writes were rolled back.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from brainoid_contracts.transactions import Document, Transaction, TransactionStore


@dataclass(frozen=True)
class ReleaseGuard:
    """Only already-verified no-I/O evidence; safe to retain across async handoffs."""

    checks: tuple[Callable[[], None], ...] = field(repr=False)

    def finish(self) -> None:
        for check in self.checks:
            check()


def release_guard(tx: Transaction) -> ReleaseGuard:
    """Capture this successful attempt after its last evidence registration."""
    if not isinstance(tx, GuardedTransaction):
        raise TypeError("A guarded transaction is required")
    return ReleaseGuard(tuple(tx.checks))


class GuardedTransaction:
    def __init__(self, transaction: Transaction) -> None:
        self.transaction = transaction
        self.checks: list[Callable[[], None]] = []

    def finish(self) -> None:
        for check in self.checks:
            check()

    def get(self, path: str) -> Document | None:
        return self.transaction.get(path)

    def collection(self, path: str) -> list[tuple[str, Document]]:
        return self.transaction.collection(path)

    def query(self, path: str, filters: dict[str, str]) -> list[tuple[str, Document]]:
        return self.transaction.query(path, filters)

    def query_page(
        self, path: str, filters: dict[str, str], *, after: str | None, limit: int
    ) -> list[tuple[str, Document]]:
        return self.transaction.query_page(path, filters, after=after, limit=limit)

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        return self.transaction.find(collection, field, value)

    def find_array(self, collection: str, field: str, value: Document) -> list[Document]:
        return self.transaction.find_array(collection, field, value)

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        return self.transaction.memberships(user_id)

    def create(self, path: str, document: Document) -> None:
        self.finish()
        self.transaction.create(path, document)

    def set(self, path: str, document: Document) -> None:
        self.finish()
        self.transaction.set(path, document)

    def delete(self, path: str) -> None:
        self.finish()
        self.transaction.delete(path)


def protect(tx: Transaction, check: Callable[[], None]) -> None:
    """Check immediately; guarded stores additionally retain it until response release.

    Unguarded composition still requires its explicit finish-after-read/return calls.
    Worker run execution installs GuardedTransactions on its shared Jobs store.
    """
    check()
    if isinstance(tx, GuardedTransaction):
        tx.checks.append(check)


class GuardedTransactions:
    def __init__(self, store: TransactionStore) -> None:
        self.store = store

    def __getattr__(self, name: str) -> Any:
        # Preserve adapter extensions such as bounded dispatcher paging. Only run
        # is decorated; callers must not lose capabilities of an injected store.
        return getattr(self.store, name)

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        @wraps(work)
        def checked(tx: Transaction) -> tuple[T, GuardedTransaction]:
            guarded = GuardedTransaction(tx)
            result = work(guarded)
            guarded.finish()
            return result, guarded

        result, guard = self.store.run(checked)
        guard.finish()
        return result
