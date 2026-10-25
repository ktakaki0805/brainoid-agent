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
    def collection(self, path: str) -> list[tuple[str, Document]]:
        """Read a canonical tenant collection in this transaction (including phantoms)."""
        ...

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        """Equality read, capped at two matches, in this same transaction.

        Used to resolve opaque user IDs safely and to reject ambiguous authority.
        """
        ...

    def create(self, path: str, document: Document) -> None: ...

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        """Current UID-filtered collection-group query, including canonical paths."""
        ...

    def set(self, path: str, document: Document) -> None: ...
    def delete(self, path: str) -> None: ...


class TransactionStore(Protocol):
    def run[T](self, work: Callable[[Transaction], T]) -> T:
        """Return only after all writes commit atomically; retry the whole callback."""
        ...
