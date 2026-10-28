"""Pinned SDK retry boundary: known pre-commit aborts share the five-attempt budget."""

import unittest
from typing import Any
from unittest.mock import Mock

from brainoid_contracts.transactions import Transaction as TransactionPort
from brainoid_persistence.firestore import FirestoreTransactions, PersistenceUnavailable
from google.api_core.exceptions import Aborted, ServiceUnavailable
from google.cloud.firestore_v1 import Client
from google.cloud.firestore_v1.transaction import Transaction


def aborted(message: str) -> Aborted:
    return Aborted(message)  # type: ignore[no-untyped-call]


def unavailable(message: str) -> ServiceUnavailable:
    return ServiceUnavailable(message)  # type: ignore[no-untyped-call]


class FirestoreTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock(spec=Client)
        self.transactions: list[Any] = []
        self.commit = Mock()
        self.snapshot = Mock()
        self.snapshot.to_dict.return_value = {"version": 1}
        self.read = self.client.document.return_value.get
        self.read.return_value = self.snapshot

        def transaction(*, max_attempts: int) -> Any:
            tx = Mock(spec=Transaction)
            tx._max_attempts, tx._read_only, tx._id = max_attempts, False, b"synthetic"
            tx._commit.side_effect = self.commit
            self.transactions.append(tx)
            return tx

        self.client.transaction.side_effect = transaction
        self.store = FirestoreTransactions(self.client)

    def read_write(self, tx: Any) -> Any:
        value = tx.get("tenants/test/resources/item")
        tx.set("tenants/test/resources/item", value)
        return value

    def test_read_abort_rolls_back_and_rereads_before_one_commit(self) -> None:
        self.read.side_effect = [aborted("read conflict"), self.snapshot]
        self.assertEqual(self.store.run(self.read_write), {"version": 1})
        self.assertEqual(self.read.call_count, 2)
        self.assertEqual(self.commit.call_count, 1)
        self.assertEqual([t._max_attempts for t in self.transactions], [5, 4])
        self.transactions[0]._rollback.assert_called_once()
        self.transactions[0]._commit.assert_not_called()
        self.transactions[1].set.assert_called_once()

    def test_repeated_read_aborts_stop_after_five_callbacks(self) -> None:
        self.read.side_effect = aborted("read conflict")
        with self.assertRaises(PersistenceUnavailable):
            self.store.run(self.read_write)
        self.assertEqual(self.read.call_count, 5)
        self.assertEqual([t._max_attempts for t in self.transactions], [5, 4, 3, 2, 1])
        self.commit.assert_not_called()
        for tx in self.transactions:
            tx._rollback.assert_called_once()

    def test_mixed_commit_and_read_aborts_share_total_budget(self) -> None:
        self.read.side_effect = [
            self.snapshot,
            aborted("read conflict"),
            self.snapshot,
            self.snapshot,
            self.snapshot,
        ]
        self.commit.side_effect = aborted("commit conflict")
        with self.assertRaises(PersistenceUnavailable):
            self.store.run(self.read_write)
        self.assertEqual(self.read.call_count, 5)
        self.assertEqual(self.commit.call_count, 4)
        self.assertEqual([t._max_attempts for t in self.transactions], [5, 3])

    def test_unknown_commit_response_is_not_retried(self) -> None:
        self.commit.side_effect = unavailable("lost response")
        with self.assertRaises(PersistenceUnavailable):
            self.store.run(self.read_write)
        self.assertEqual(self.read.call_count, 1)
        self.commit.assert_called_once()

    def test_every_transactional_query_restarts_on_read_abort(self) -> None:
        query = Mock()
        query.where.return_value = query
        query.limit.return_value = query
        self.client.collection.return_value = query
        self.client.collection_group.return_value = query
        self.snapshot.reference.path = "tenants/test/resources/item"
        for method, args in (
            ("collection", ("resources",)),
            ("query", ("resources", {"owner": "test"})),
            ("find", ("resources", "owner", "test")),
            ("memberships", ("test",)),
        ):
            with self.subTest(method=method):
                query.stream.reset_mock(side_effect=True)
                query.stream.side_effect = [aborted("query conflict"), iter((self.snapshot,))]

                def read(tx: Any, method: str = method, args: tuple[Any, ...] = args) -> Any:
                    return getattr(tx, method)(*args)

                result = self.store.run(read)
                self.assertTrue(result)
                self.assertEqual(query.stream.call_count, 2)
                self.assertEqual([t._max_attempts for t in self.transactions[-2:]], [5, 4])

    def test_new_attempt_reauthorizes_and_business_abort_is_not_retried(self) -> None:
        self.read.side_effect = aborted("read conflict")
        calls: list[bool] = []

        def authorized(tx: Any) -> Any:
            calls.append(True)
            if len(calls) > 1:
                raise ValueError("revoked")
            return self.read_write(tx)

        with self.assertRaisesRegex(ValueError, "revoked"):
            self.store.run(authorized)
        self.assertEqual(len(calls), 2)
        self.commit.assert_not_called()
        work = Mock(side_effect=aborted("callback business error"))
        with self.assertRaises(PersistenceUnavailable):
            self.store.run(work)
        work.assert_called_once()

    def test_unavailable_read_and_policy_failure_are_not_retried(self) -> None:
        self.read.side_effect = unavailable("unavailable read")
        with self.assertRaises(PersistenceUnavailable):
            self.store.run(self.read_write)
        self.assertEqual(self.read.call_count, 1)
        self.commit.assert_not_called()
        self.read.reset_mock(side_effect=True)
        self.read.side_effect = ValueError("business denial")
        with self.assertRaisesRegex(ValueError, "business denial"):
            self.store.run(self.read_write)
        self.assertEqual(self.read.call_count, 1)
        self.commit.assert_not_called()

    def test_bounded_query_applies_keyset_and_limit_before_transactional_stream(self) -> None:
        query = Mock()
        query.order_by.return_value = query
        query.where.return_value = query
        query.limit.return_value = query
        query.start_after.return_value = query
        self.client.collection.return_value = query
        self.snapshot.reference.path = "resources/b"
        query.stream.side_effect = [aborted("bounded query conflict"), iter((self.snapshot,))]
        result = self.store.run(
            lambda tx: tx.query_page("resources", {"target.id": "device"}, after="a", limit=21)
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(query.stream.call_count, 2)
        query.order_by.assert_called_with("__name__")
        query.limit.assert_called_with(21)
        query.start_after.assert_called_with({"__name__": query.document.return_value})
        query.document.assert_called_with("a")
        self.assertEqual([t._max_attempts for t in self.transactions], [5, 4])
        for limit, after in ((101, None), (0, None), (20, "../other")):

            def invalid(tx: TransactionPort, limit: int = limit, after: str | None = after) -> Any:
                return tx.query_page("resources", {"target.id": "device"}, after=after, limit=limit)

            with self.assertRaises(ValueError):
                self.store.run(invalid)
