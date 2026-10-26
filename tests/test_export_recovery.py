"""MEM09-R1-01: transient source authority is retryable, unknown puts are not."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable
from brainoid_jobs.models import job_id
from brainoid_memory.export_manifest import KIND
from brainoid_memory.export_projection import project
from brainoid_memory.exports import registry_path
from brainoid_memory.forget import operation_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.scopes import PolicyUnavailable
from brainoid_worker import export_data
from fakes.memory_repository import TENANT
from test_exports import ExportFixture


class ExportRecoveryTests(ExportFixture):
    def job(self, operation: ResourceId) -> Document:
        return self.store.data[f"tenants/{TENANT}/jobs/{job_id(TENANT, KIND, operation)}"]

    def retryable(self, operation: ResourceId, *, staged: bool = False) -> None:
        job = self.job(operation)
        self.assertEqual(
            (job["status"], job["reason"], job["attempts"]),
            ("pending", "authorization_unavailable", 0),
        )
        self.assertEqual(
            self.store.data[registry_path(operation)]["state"], "staged" if staged else "pending"
        )
        self.assertNotIn("erased", self.store.data[registry_path(operation)])
        self.assertFalse((self.storage.directory / (operation + ".revoked")).exists())
        self.assertNotIn(
            "error_code", self.store.data[operation_path(TENANT, operation)]["operation"]
        )

    def recover(self, operation: ResourceId) -> None:
        self.now += timedelta(seconds=6)
        self.generate(operation)
        self.assertEqual(self.requests.get(self.identity(), TENANT, operation).status, "succeeded")
        self.assertEqual(self.download(operation).status_code, 200)

    def test_real_source_ledger_outage_and_stage_outage_refund_before_effect(self) -> None:
        original = project
        target = f"tenants/{TENANT}/entities/{self.subject}"
        for occurrence in (1, 2):
            with self.subTest(occurrence=occurrence):
                operation = self.create_export(key=f"source-stage-outage-{occurrence}")
                calls = 0

                def unavailable(
                    tx: Transaction,
                    access: Any,
                    path: str,
                    *,
                    scope: str,
                    occurrence: int = occurrence,
                ) -> Any:
                    nonlocal calls
                    if path == target:
                        calls += 1
                        if calls == occurrence:
                            with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                                return original(tx, access, path, scope=scope)
                    return original(tx, access, path, scope=scope)

                with (
                    patch.object(export_data, "project", side_effect=unavailable),
                    patch.object(self.storage, "put", wraps=self.storage.put) as put,
                ):
                    with self.assertRaises(AuthorizationUnavailable):
                        self.generate(operation)
                    put.assert_not_called()
                self.assertEqual(calls, occurrence)
                self.retryable(operation)
                self.assertEqual(self.job(operation)["checkpoints"], [])
                self.recover(operation)

    def test_persistent_ledger_outage_in_finally_refunds_more_than_max_attempts(self) -> None:
        operation = self.create_export()
        original = project
        for _ in range(6):
            failure = patch.object(self.ledger, "latest", side_effect=LedgerError())
            started = False

            def unavailable(*args: Any, failure: Any = failure, **kwargs: Any) -> Any:
                nonlocal started
                if not started:
                    started = True
                    failure.start()
                return original(*args, **kwargs)

            try:
                with (
                    patch.object(export_data, "project", side_effect=unavailable),
                    self.assertRaises(AuthorizationUnavailable),
                ):
                    self.generate(operation)
                self.assertTrue(started)
                self.retryable(operation)
                self.assertEqual(self.job(operation)["checkpoints"], [])
            finally:
                failure.stop()
            self.now += timedelta(seconds=6)
        self.recover(operation)
        self.assertEqual(self.job(operation)["attempts"], 1)

    def test_commit_ledger_outage_replays_completed_receipt_without_second_put(self) -> None:
        operation = self.create_export()
        original = self.exports.current
        failed = False

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            nonlocal failed
            checkpoints = self.job(operation)["checkpoints"]
            if not failed and checkpoints and checkpoints[0]["state"] == "completed":
                failed = True
                with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)

        with patch.object(self.storage, "put", wraps=self.storage.put) as put:
            with (
                patch.object(self.exports, "current", side_effect=unavailable),
                self.assertRaises(AuthorizationUnavailable),
            ):
                self.generate(operation)
            self.assertTrue(failed)
            self.retryable(operation, staged=True)
            self.assertEqual(self.job(operation)["checkpoints"][0]["state"], "completed")
            self.assertEqual(put.call_count, 1)
            self.recover(operation)
            self.assertEqual(put.call_count, 1)

    def test_reconciliation_outage_after_commit_preserves_successful_progress(self) -> None:
        operation = self.create_export()
        original = self.exports.cleanup_one

        def unavailable(identity: ResourceId) -> bool:
            with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                return original(identity)

        with (
            patch.object(self.exports, "cleanup_one", side_effect=unavailable),
            self.assertRaises(AuthorizationUnavailable),
        ):
            self.generate(operation)
        self.assertEqual(self.job(operation)["status"], "succeeded")
        self.assertEqual(self.requests.get(self.identity(), TENANT, operation).status, "succeeded")
        self.assertEqual(self.store.data[registry_path(operation)]["state"], "ready")
        self.assertEqual(self.download(operation).status_code, 200)

    def test_finally_does_not_replace_original_persistence_signal(self) -> None:
        operation = self.create_export()
        original = PersistenceUnavailable()
        with (
            patch.object(export_data, "project", side_effect=original),
            patch.object(self.exports, "cleanup_one", side_effect=PolicyUnavailable()),
        ):
            with self.assertRaises(PersistenceUnavailable) as caught:
                self.generate(operation)
        self.assertIs(caught.exception, original)
        self.assertEqual(self.job(operation)["reason"], "persistence_unavailable")
        self.assertEqual(self.job(operation)["attempts"], 0)
        self.assertFalse((self.storage.directory / (operation + ".revoked")).exists())
        self.recover(operation)

    def test_failed_or_lost_deferral_bookkeeping_retains_recoverable_durable_state(self) -> None:
        for committed in (False, True):
            with self.subTest(committed=committed):
                operation = self.create_export(key=f"export-deferral-bookkeeping-{committed}")
                defer = self.exports.jobs.defer_unavailable

                def unavailable(
                    *args: Any, committed: bool = committed, defer: Any = defer, **kwargs: Any
                ) -> None:
                    if committed:
                        defer(*args, **kwargs)
                    raise PersistenceUnavailable()

                with (
                    patch.object(export_data, "project", side_effect=PolicyUnavailable()),
                    patch.object(self.exports.jobs, "defer_unavailable", side_effect=unavailable),
                ):
                    with self.assertRaises(PersistenceUnavailable):
                        self.generate(operation)
                self.assertEqual(
                    self.job(operation)["status"], "pending" if committed else "running"
                )
                self.assertEqual(self.job(operation)["attempts"], 0 if committed else 1)
                self.assertEqual(self.job(operation)["checkpoints"], [])
                self.assertEqual(self.store.data[registry_path(operation)]["state"], "pending")
                self.assertFalse((self.storage.directory / (operation + ".revoked")).exists())
                self.now += timedelta(minutes=2)
                self.recover(operation)

    def test_outage_after_durable_start_never_reexecutes_unknown_effect(self) -> None:
        for after_put in (False, True):
            with self.subTest(after_put=after_put):
                operation = self.create_export(key=f"export-started-outage-{after_put}")
                original = self.exports.current
                failed = False
                with patch.object(self.storage, "put", wraps=self.storage.put) as put:

                    def unavailable(
                        *args: Any,
                        operation: ResourceId = operation,
                        after_put: bool = after_put,
                        original: Any = original,
                        **kwargs: Any,
                    ) -> Any:
                        nonlocal failed
                        checkpoints = self.job(operation)["checkpoints"]
                        if (
                            not failed
                            and checkpoints
                            and checkpoints[0]["state"] == "started"
                            and bool(put.call_count) == after_put
                        ):
                            failed = True
                            with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                                return original(*args, **kwargs)
                        return original(*args, **kwargs)

                    with (
                        patch.object(self.exports, "current", side_effect=unavailable),
                        self.assertRaises(AuthorizationUnavailable),
                    ):
                        self.generate(operation)
                    self.assertTrue(failed)
                    self.assertEqual(self.job(operation)["status"], "failed")
                    self.assertEqual(self.job(operation)["reason"], "outcome_unknown")
                    self.assertEqual(put.call_count, int(after_put))
                    self.reaper.step()
                    self.erased(operation)
                    self.now += timedelta(minutes=2)
                    self.assertIsNone(
                        self.exports.jobs.prepare_dispatch(TENANT, job_id(TENANT, KIND, operation))
                    )
                    self.assertEqual(put.call_count, int(after_put))

    def test_post_put_authorization_signal_survives_erasure_outage(self) -> None:
        operation = self.create_export()
        original = self.exports.current
        failed = False
        with patch.object(self.storage, "put", wraps=self.storage.put) as put:

            def unavailable(*args: Any, **kwargs: Any) -> Any:
                nonlocal failed
                if not failed and put.call_count:
                    failed = True
                    with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                        return original(*args, **kwargs)
                return original(*args, **kwargs)

            with (
                patch.object(self.exports, "current", side_effect=unavailable),
                patch.object(
                    self.exports, "revoke", side_effect=RuntimeError("synthetic cleanup failure")
                ),
            ):
                with self.assertRaises(AuthorizationUnavailable):
                    self.generate(operation)
            self.assertEqual(self.job(operation)["reason"], "outcome_unknown")
            self.assertEqual(put.call_count, 1)
            self.reaper.step()
            self.erased(operation)
            self.assertIsNone(
                self.exports.jobs.prepare_dispatch(TENANT, job_id(TENANT, KIND, operation))
            )
            self.assertEqual(put.call_count, 1)
