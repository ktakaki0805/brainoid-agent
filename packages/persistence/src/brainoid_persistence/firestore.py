"""Official SDK adapter. Importing it never constructs a client or discovers credentials."""

from collections.abc import Callable
from typing import cast

from brainoid_contracts.transactions import Document, Transaction
from google.api_core.exceptions import Aborted, GoogleAPICallError, RetryError
from google.cloud.firestore_v1 import Client, FieldFilter
from google.cloud.firestore_v1.transaction import Transaction as SDKTransaction
from google.cloud.firestore_v1.transaction import transactional


class PersistenceUnavailable(Exception):
    """Storage could not confirm an operation; a commit may already be durable.

    Callers must reconcile persisted state or retry with the same logical identity.
    This exception alone never proves that a business operation permanently failed.
    """

    def __init__(self) -> None:
        super().__init__("persistence_unavailable")


class _AbortedBeforeCommit(Exception):
    """A transactional SDK read aborted before any buffered writes were submitted."""


def _read[T](work: Callable[[], T]) -> T:
    try:
        return work()
    except Aborted:
        # The pinned SDK retries commit Aborted only. Distinguish a read abort
        # at the SDK boundary from arbitrary callback/business exceptions.
        raise _AbortedBeforeCommit() from None


class _Transaction:
    def __init__(self, client: Client, transaction: SDKTransaction) -> None:
        self._client = client
        self._transaction = transaction

    def get(self, path: str) -> Document | None:
        return _read(
            lambda: self._client.document(path).get(transaction=self._transaction).to_dict()
        )

    def collection(self, path: str) -> list[tuple[str, Document]]:
        return _read(
            lambda: [
                (snapshot.reference.path, snapshot.to_dict() or {})
                for snapshot in self._client.collection(path).stream(transaction=self._transaction)
            ]
        )

    def query(self, path: str, filters: dict[str, str]) -> list[tuple[str, Document]]:
        query = self._client.collection(path).where(
            filter=FieldFilter(next(iter(filters)), "==", next(iter(filters.values())))
        )
        for field, value in list(filters.items())[1:]:
            query = query.where(filter=FieldFilter(field, "==", value))
        return _read(
            lambda: [
                (item.reference.path, item.to_dict() or {})
                for item in query.stream(transaction=self._transaction)
            ]
        )

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        query = self._client.collection(collection).where(filter=FieldFilter(field, "==", value))
        return _read(
            lambda: [
                snapshot.to_dict() or {}
                for snapshot in query.limit(2).stream(transaction=self._transaction)
            ]
        )

    def query_page(
        self, path: str, filters: dict[str, str], *, after: str | None, limit: int
    ) -> list[tuple[str, Document]]:
        if not filters or not 1 <= limit <= 100 or (after is not None and "/" in after):
            raise ValueError("Invalid query page")
        collection = self._client.collection(path)
        query = collection.order_by("__name__").limit(limit)
        for field, value in filters.items():
            query = query.where(filter=FieldFilter(field, "==", value))
        if after is not None:
            query = query.start_after({"__name__": collection.document(after)})
        return _read(
            lambda: [
                (item.reference.path, item.to_dict() or {})
                for item in query.stream(transaction=self._transaction)
            ]
        )

    def find_array(self, collection: str, field: str, value: Document) -> list[Document]:
        query = (
            self._client.collection(collection)
            .where(filter=FieldFilter(field, "array_contains", value))
            .limit(2)
        )
        return _read(
            lambda: [
                snapshot.to_dict() or {} for snapshot in query.stream(transaction=self._transaction)
            ]
        )

    def create(self, path: str, document: Document) -> None:
        self._transaction.create(self._client.document(path), document)

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        query = self._client.collection_group("memberships").where(
            filter=FieldFilter("user_id", "==", user_id)
        )
        return _read(
            lambda: [
                (snapshot.reference.path, snapshot.to_dict() or {})
                for snapshot in query.stream(transaction=self._transaction)
            ]
        )

    def set(self, path: str, document: Document) -> None:
        self._transaction.set(self._client.document(path), document)

    def delete(self, path: str) -> None:
        self._transaction.delete(self._client.document(path))


class FirestoreTransactions:
    """Inject a configured server client; run synchronous calls off the ASGI event loop."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def page(
        self, collection: str, *, after: str | None = None, limit: int = 100
    ) -> list[tuple[str, Document]]:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid page limit")
        query = self._client.collection(collection).order_by("__name__").limit(limit)
        if after is not None:
            if "/" in after:
                raise ValueError("Invalid page cursor")
            query = query.start_after(
                {"__name__": self._client.collection(collection).document(after)}
            )
        try:
            return [(item.reference.path, item.to_dict() or {}) for item in query.stream()]
        except (GoogleAPICallError, RetryError):
            raise PersistenceUnavailable() from None

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        attempts = 0

        def invoke(transaction: SDKTransaction) -> T:
            nonlocal attempts
            attempts += 1
            return work(_Transaction(self._client, transaction))

        try:
            while attempts < 5:
                try:
                    # Count callbacks including the SDK's own commit retries: a
                    # read/commit conflict mixture never multiplies the budget.
                    return cast(
                        T,
                        transactional(invoke)(self._client.transaction(max_attempts=5 - attempts)),
                    )
                except _AbortedBeforeCommit:
                    # The SDK has rolled back this known non-commit. All policy
                    # and source reads run again inside a fresh transaction.
                    continue
            raise PersistenceUnavailable()
        except (GoogleAPICallError, RetryError):
            # A failed commit response may conceal a successful commit. The caller must
            # retry the same key; the durable record, not this exception, proves success.
            raise PersistenceUnavailable() from None
        except ValueError as error:
            # The pinned SDK wraps exhausted transaction contention in ValueError.
            if isinstance(error.__cause__, Aborted):
                raise PersistenceUnavailable() from None
            raise
