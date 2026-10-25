"""Official SDK adapter. Importing it never constructs a client or discovers credentials."""

from collections.abc import Callable
from typing import cast

from google.api_core.exceptions import Aborted, GoogleAPICallError, RetryError
from google.cloud.firestore_v1 import Client, FieldFilter
from google.cloud.firestore_v1.transaction import Transaction as SDKTransaction
from google.cloud.firestore_v1.transaction import transactional

from brainoid_api.common.transactions import Document, Transaction
from brainoid_api.errors import APIError, ErrorCode


class _Transaction:
    def __init__(self, client: Client, transaction: SDKTransaction) -> None:
        self._client = client
        self._transaction = transaction

    def get(self, path: str) -> Document | None:
        snapshot = self._client.document(path).get(transaction=self._transaction)
        return snapshot.to_dict()

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        query = self._client.collection(collection).where(filter=FieldFilter(field, "==", value))
        return [
            snapshot.to_dict() or {}
            for snapshot in query.limit(2).stream(transaction=self._transaction)
        ]

    def create(self, path: str, document: Document) -> None:
        self._transaction.create(self._client.document(path), document)

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        query = self._client.collection_group("memberships").where(
            filter=FieldFilter("user_id", "==", user_id)
        )
        return [
            (snapshot.reference.path, snapshot.to_dict() or {})
            for snapshot in query.stream(transaction=self._transaction)
        ]

    def set(self, path: str, document: Document) -> None:
        self._transaction.set(self._client.document(path), document)

    def delete(self, path: str) -> None:
        self._transaction.delete(self._client.document(path))


class FirestoreTransactions:
    """Inject a configured server client; run synchronous calls off the ASGI event loop."""

    def __init__(self, client: Client) -> None:
        self._client = client

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        def invoke(transaction: SDKTransaction) -> T:
            return work(_Transaction(self._client, transaction))

        try:
            return cast(T, transactional(invoke)(self._client.transaction(max_attempts=5)))
        except (GoogleAPICallError, RetryError):
            # A failed commit response may conceal a successful commit. The caller must
            # retry the same key; the durable record, not this exception, proves success.
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        except ValueError as error:
            # The pinned SDK wraps exhausted transaction contention in ValueError.
            if isinstance(error.__cause__, Aborted):
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
            raise
