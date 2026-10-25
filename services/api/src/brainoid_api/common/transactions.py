"""Small transaction boundary. All callback effects must be writes through this port."""

from collections.abc import Callable
from typing import Any, Protocol

type Document = dict[str, Any]


class Transaction(Protocol):
    """Read before writing. No provider calls, messages, or other external effects.

    A transaction callback may be invoked repeatedly after contention. Authorization
    reads must use this same transaction to conflict with concurrent revocation.
    """

    def get(self, path: str) -> Document | None: ...
    def create(self, path: str, document: Document) -> None: ...
    def set(self, path: str, document: Document) -> None: ...
    def delete(self, path: str) -> None: ...


class TransactionStore(Protocol):
    def run[T](self, work: Callable[[Transaction], T]) -> T:
        """Return only after all writes commit atomically; retry the whole callback."""
        ...
