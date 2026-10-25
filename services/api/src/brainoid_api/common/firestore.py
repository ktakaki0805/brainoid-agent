"""HTTP error compatibility for the shared Firestore transaction adapter."""

from collections.abc import Callable

from brainoid_contracts.transactions import Transaction
from brainoid_persistence.firestore import FirestoreTransactions as SharedFirestoreTransactions
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_persistence.firestore import _Transaction as _Transaction

from brainoid_api.errors import APIError, ErrorCode


class FirestoreTransactions(SharedFirestoreTransactions):
    def run[T](self, work: Callable[[Transaction], T]) -> T:
        try:
            return super().run(work)
        except PersistenceUnavailable:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
