"""BA-DEV-02 / T-017: durable receipt, failure quarantine and cross-store reset."""

import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_device.app import DeviceApplication
from brainoid_device.config import Settings
from brainoid_device.ledger import (
    DB_NAME,
    INITIALIZED_MARKER,
    RETENTION_SECONDS,
    CommandDelivery,
    DeviceStorage,
    LedgerConflict,
    Preferences,
    StorageUnavailable,
    TerminalStatus,
)
from brainoid_device.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION
from brainoid_device.storage import RESET_MARKER, CredentialStore, CredentialsUnavailable
from starlette.testclient import TestClient

TENANT = "11111111-1111-4111-8111-111111111111"
DEVICE = "22222222-2222-4222-8222-222222222222"
COMMAND = "33333333-3333-4333-8333-333333333333"
ROOT = Path(__file__).resolve().parents[1]
PROCESS = ROOT / "tests" / "device_storage_process.py"


@unittest.skipUnless(sys.platform == "linux", "device storage requires Linux directory FD/procfs")
class StorageFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="brainoid-storage-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.private = self.root / "credentials"
        self.state.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        self.temp = self.root / "private-temp"
        self.credentials = CredentialStore(self.private)
        self.addCleanup(self.credentials.close)

    def storage(self) -> DeviceStorage:
        result = DeviceStorage(
            self.state, self.credentials, cleanup_private=lambda: self.temp.unlink(missing_ok=True)
        )
        self.addCleanup(result.close)
        return result

    def seed(self) -> tuple[DeviceStorage, CommandDelivery]:
        storage = self.storage()
        binding = storage.register(TENANT, DEVICE)
        storage.set_generation(binding.epoch, 1)
        delivery = CommandDelivery(TENANT, DEVICE, binding.epoch, 1, COMMAND, 1, "a" * 64, 30000)
        return storage, delivery

    def run_process(self, phase: str, expected_exit: int = 0) -> dict[str, Any]:
        env = os.environ.copy()
        env.pop("FIRESTORE_EMULATOR_HOST", None)
        env.pop("FIREBASE_AUTH_EMULATOR_HOST", None)
        result = subprocess.run(
            [sys.executable, "-B", str(PROCESS), str(self.root), phase],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, expected_exit, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout) if result.stdout else {}


class LedgerTests(StorageFixture):
    def test_preferences_registration_and_secrets_are_references_only(self) -> None:
        storage, delivery = self.seed()
        preferences = Preferences(volume_pct=25, microphone_muted=True)
        storage.save_preferences(preferences)
        self.credentials.write("cloud_refresh", b"synthetic-private-secret")
        storage.save_credential_reference(delivery.epoch, "cloud_refresh", "cloud_refresh")
        self.assertEqual(
            storage.credential_reference(delivery.epoch, "cloud_refresh"), "cloud_refresh"
        )
        storage.close()
        reopened = self.storage()
        self.assertEqual(reopened.snapshot().preferences, preferences)
        self.assertTrue(reopened.snapshot().permits_operations)
        self.assertEqual(reopened.snapshot().registration.epoch, delivery.epoch)  # type: ignore[union-attr]
        with self.assertRaises(LedgerConflict):
            reopened.register(str(uuid4()), DEVICE)
        with self.assertRaises(ValueError):
            reopened.save_credential_reference(delivery.epoch, "home_assistant", "secret/value")
        self.assertNotIn(b"synthetic-private-secret", (self.state / DB_NAME).read_bytes())
        self.assertEqual(
            set(path.name for path in self.state.iterdir()), {DB_NAME, INITIALIZED_MARKER}
        )
        self.assertEqual((self.state / DB_NAME).stat().st_mode & 0o777, 0o600)
        for invalid in ({"protocol_version": 2}, {"retention_seconds": 1}, {"volume_pct": 101}):
            with self.assertRaises(ValueError):
                replace(Preferences(), **cast(dict[str, Any], invalid))

    def test_duplicate_identity_generation_cursor_and_grant_consumption(self) -> None:
        storage, delivery = self.seed()
        saved = storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        self.assertEqual(saved.status, "accepted")
        self.assertEqual(
            storage.receive_command(delivery, expected_cursor=0, now_ms=1, clock_trusted=True),
            saved,
        )
        for changed in (
            replace(delivery, payload_digest="b" * 64),
            replace(delivery, expires_at_ms=40000),
            replace(delivery, tenant_id=str(uuid4())),
            replace(delivery, delivery_seq=2),
        ):
            with self.assertRaises(LedgerConflict):
                storage.receive_command(changed, expected_cursor=1, now_ms=1, clock_trusted=True)
        storage.set_generation(delivery.epoch, 2)
        with self.assertRaises(LedgerConflict):
            storage.receive_command(delivery, expected_cursor=1, now_ms=1, clock_trusted=True)
        current = replace(delivery, connection_generation=2)
        self.assertEqual(
            storage.receive_command(current, expected_cursor=1, now_ms=1, clock_trusted=True), saved
        )
        with self.assertRaises(LedgerConflict):
            storage.record_started(
                delivery.epoch,
                COMMAND,
                str(uuid4()),
                2,
                grant_expires_at_ms=30000,
                now_ms=1,
                clock_trusted=True,
            )
        self.assertTrue(
            storage.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                2,
                grant_expires_at_ms=30000,
                now_ms=1,
                clock_trusted=True,
            )
        )
        self.assertFalse(
            storage.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                2,
                grant_expires_at_ms=30000,
                now_ms=2,
                clock_trusted=True,
            )
        )
        storage.close()
        storage = self.storage()
        restored = storage.command(delivery.epoch, COMMAND)
        assert restored is not None
        self.assertEqual(
            (restored.status, restored.wire_status, restored.start_generation),
            ("started", "outcome_unknown", 2),
        )
        self.assertFalse(
            storage.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                2,
                grant_expires_at_ms=30000,
                now_ms=3,
                clock_trusted=True,
            )
        )
        storage.record_result(
            delivery.epoch, COMMAND, saved.execution_id, status="outcome_unknown", now_ms=4
        )
        storage.record_result(
            delivery.epoch, COMMAND, saved.execution_id, status="succeeded", now_ms=5
        )
        self.assertEqual(storage.command(delivery.epoch, COMMAND).status, "succeeded")  # type: ignore[union-attr]
        with self.assertRaises(LedgerConflict):
            storage.record_result(
                delivery.epoch, COMMAND, saved.execution_id, status="failed", now_ms=6
            )
        with self.assertRaises(LedgerConflict):
            storage.advance_cursor(delivery.epoch, 2, expected=0, cursor=9)
        storage.advance_cursor(delivery.epoch, 2, expected=1, cursor=9)
        self.assertEqual(storage.snapshot().registration.delivery_cursor, 9)  # type: ignore[union-attr]

    def test_expiry_clock_and_transaction_rejection_leave_cursor_unchanged(self) -> None:
        storage, delivery = self.seed()
        with self.assertRaises(LedgerConflict):
            storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=False)
        with self.assertRaises(LedgerConflict):
            storage.receive_command(delivery, expected_cursor=1, now_ms=0, clock_trusted=True)
        self.assertEqual(storage.snapshot().registration.delivery_cursor, 0)  # type: ignore[union-attr]
        saved = storage.receive_command(
            delivery, expected_cursor=0, now_ms=30000, clock_trusted=True
        )
        self.assertEqual(saved.status, "expired")
        self.assertFalse(
            storage.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                1,
                grant_expires_at_ms=40000,
                now_ms=30000,
                clock_trusted=True,
            )
        )
        next_delivery = replace(
            delivery, command_id=str(uuid4()), delivery_seq=2, expires_at_ms=50000
        )
        pending = storage.receive_command(
            next_delivery, expected_cursor=1, now_ms=30000, clock_trusted=True
        )
        for expiry, now, trusted in (
            (40000, 40000, True),
            (60000, 50000, True),
            (50000, 30000, False),
        ):
            with self.assertRaises(LedgerConflict):
                storage.record_started(
                    delivery.epoch,
                    pending.command_id,
                    pending.execution_id,
                    1,
                    grant_expires_at_ms=expiry,
                    now_ms=now,
                    clock_trusted=trusted,
                )

    def test_retention_exact_boundary_never_discards_unresolved_or_unacknowledged(self) -> None:
        storage, delivery = self.seed()
        saved_commands = []
        for index, state in enumerate(
            (
                "succeeded",
                "failed",
                "expired",
                "accepted",
                "started",
                "outcome_unknown",
                "succeeded",
            ),
            1,
        ):
            item = replace(delivery, command_id=str(uuid4()), delivery_seq=index)
            saved = storage.receive_command(
                item, expected_cursor=index - 1, now_ms=0, clock_trusted=True
            )
            if state in {"succeeded", "started", "outcome_unknown"}:
                storage.record_started(
                    item.epoch,
                    item.command_id,
                    saved.execution_id,
                    1,
                    grant_expires_at_ms=30000,
                    now_ms=1,
                    clock_trusted=True,
                )
            if state not in {"accepted", "started"}:
                storage.record_result(
                    item.epoch,
                    item.command_id,
                    saved.execution_id,
                    status=cast(TerminalStatus, state),
                    now_ms=2,
                )
                if index != 7:
                    storage.acknowledge_result(
                        item.epoch, item.command_id, saved.execution_id, cast(TerminalStatus, state)
                    )
            saved_commands.append(item)
        cutoff = RETENTION_SECONDS * 1000 + 2
        self.assertEqual(storage.prune_commands(now_ms=cutoff - 1, clock_trusted=True), 0)
        self.assertEqual(storage.prune_commands(now_ms=cutoff, clock_trusted=True), 3)
        self.assertEqual(storage.prune_commands(now_ms=cutoff * 100, clock_trusted=True), 0)
        for item in saved_commands[3:]:
            self.assertIsNotNone(storage.command(item.epoch, item.command_id))
        with self.assertRaises(LedgerConflict):
            storage.receive_command(
                saved_commands[0], expected_cursor=7, now_ms=cutoff, clock_trusted=True
            )
        with self.assertRaises(LedgerConflict):
            storage.prune_commands(now_ms=cutoff, clock_trusted=False)

    def test_real_process_mid_transaction_rollback_then_commit_and_started_restart(self) -> None:
        self.run_process("transaction", 73)
        self.assertEqual(self.run_process("inspect")["cursor"], 0)
        self.assertIsNone(self.run_process("inspect")["command"])
        self.run_process("committed", 73)
        committed = self.run_process("inspect")
        self.assertEqual((committed["cursor"], committed["command"]), (1, "accepted"))
        self.run_process("started", 73)
        restarted = self.run_process("inspect")
        self.assertEqual(restarted["command"], "started")
        self.assertEqual(restarted["execution"], committed["execution"])

    def test_real_process_interrupted_first_migration_rolls_back_and_resumes(self) -> None:
        self.run_process("migration", 73)
        self.assertFalse((self.state / INITIALIZED_MARKER).exists())
        self.assertEqual(self.run_process("inspect")["status"], "unregistered")
        self.assertTrue((self.state / INITIALIZED_MARKER).exists())

    def test_v1_forward_migration_preserves_identity_and_adds_safe_ack_default(self) -> None:
        path = self.state / DB_NAME
        db = sqlite3.connect(path)
        path.chmod(0o600)
        db.execute("BEGIN")
        for statement in MIGRATIONS[0]:
            db.execute(statement)
        epoch = str(uuid4())
        execution = str(uuid4())
        db.execute("INSERT INTO registration VALUES (1,?,?,?,?,?)", (TENANT, DEVICE, epoch, 4, 9))
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (COMMAND, execution, epoch, "a" * 64, 9, 4, 4, "started", 0, 30000, 1),
        )
        db.execute("PRAGMA user_version=1")
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.commit()
        db.close()
        storage = self.storage()
        restored = storage.command(epoch, COMMAND)
        assert restored is not None
        self.assertEqual(
            (restored.execution_id, restored.status, restored.result_acked),
            (execution, "started", False),
        )
        self.assertEqual(storage.snapshot().registration.delivery_cursor, 9)  # type: ignore[union-attr]
        assert storage._db is not None
        self.assertEqual(storage._db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_future_version_is_untouched_even_by_reset(self) -> None:
        path = self.state / DB_NAME
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE future_private (value TEXT)")
            db.execute("INSERT INTO future_private VALUES ('synthetic')")
            db.execute("PRAGMA user_version=999")
        db.close()
        path.chmod(0o600)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        storage = self.storage()
        self.assertEqual(storage.snapshot().status, "recovery_required")
        self.assertFalse(storage.local_reset())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertTrue(self.credentials.reset_pending)


class StorageFailureTests(StorageFixture):
    def test_empty_first_creation_is_resumable_but_used_database_loss_is_not(self) -> None:
        path = self.state / DB_NAME
        path.touch(mode=0o600)
        storage, delivery = self.seed()
        storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        storage.close()
        path.write_bytes(b"")
        failed = self.storage()
        self.assertEqual(failed.snapshot().status, "recovery_required")
        self.assertEqual(path.stat().st_size, 0)
        failed.close()
        path.unlink()
        missing = self.storage()
        self.assertEqual(missing.snapshot().status, "recovery_required")
        self.assertFalse(path.exists())

    def test_runtime_database_replacement_cannot_return_stale_success(self) -> None:
        storage, _ = self.seed()
        path = self.state / DB_NAME
        replacement = self.root / "replacement"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        self.assertEqual(storage.snapshot().status, "recovery_required")
        with self.assertRaises(StorageUnavailable):
            storage.save_preferences(Preferences())

    def test_credential_directory_lock_competition_is_finite_and_never_bypasses_fence(self) -> None:
        storage, _ = self.seed()
        self.credentials.write("cloud_refresh", b"synthetic token")
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
                "fcntl.flock(fd,fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read(1); os.close(fd)",
                str(self.private),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            self.assertTrue(select.select([process.stdout], [], [], 3)[0])
            self.assertEqual(process.stdout.readline(), "locked\n")
            with self.assertRaises(BlockingIOError):
                self.credentials.read("cloud_refresh")
            with self.assertRaises(BlockingIOError):
                self.credentials.write("cloud_refresh", b"replacement")
            self.assertEqual(storage.snapshot().status, "recovery_required")
            self.assertFalse(storage.local_reset())
            storage.close()
        finally:
            try:
                output, error = process.communicate("x", timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
                raise
        self.assertEqual((process.returncode, output, error), (0, "", ""))
        # No destructive reset step ran before acquiring the intent lock.
        self.assertEqual(self.credentials.read("cloud_refresh"), b"synthetic token")

    def test_sqlite_busy_commit_and_disk_full_error_roll_back_without_receipt(self) -> None:
        storage, delivery = self.seed()
        reader = sqlite3.connect(self.state / DB_NAME, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM settings").fetchall()
        with self.assertRaises(StorageUnavailable):
            storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        self.assertFalse(storage.snapshot().permits_operations)
        assert storage._db is not None
        self.assertFalse(storage._db.in_transaction)
        reader.execute("ROLLBACK")
        storage.close()
        recovered = self.storage()
        self.assertIsNone(recovered.command(delivery.epoch, COMMAND))
        assert recovered._db is not None
        # Deny all page growth then fill existing pages with valid command metadata.
        pages = recovered._db.execute("PRAGMA page_count").fetchone()[0]
        recovered._db.execute(f"PRAGMA max_page_count={pages}")
        accepted = 0
        for sequence in range(1, 1000):
            try:
                recovered.receive_command(
                    replace(delivery, command_id=str(uuid4()), delivery_seq=sequence),
                    expected_cursor=sequence - 1,
                    now_ms=0,
                    clock_trusted=True,
                )
                accepted = sequence
            except StorageUnavailable:
                break
        else:
            self.fail("fixture failed to exhaust fixed SQLite pages")
        self.assertFalse(recovered.snapshot().permits_operations)
        recovered.close()
        final = self.storage()
        self.assertEqual(final.snapshot().registration.delivery_cursor, accepted)  # type: ignore[union-attr]

    def test_corruption_fails_closed_and_reset_cannot_claim_success(self) -> None:
        path = self.state / DB_NAME
        path.write_bytes(b"synthetic invalid database with secret-looking-content")
        path.chmod(0o600)
        self.credentials.write("cloud_refresh", b"synthetic-token")
        self.temp.write_text("synthetic private text")
        storage = self.storage()
        self.assertFalse(storage.snapshot().permits_operations)
        with self.assertRaisesRegex(StorageUnavailable, "^device_storage_recovery_required$"):
            storage.register(TENANT, DEVICE)
        self.assertFalse(storage.local_reset())
        self.assertFalse((self.private / "cloud_refresh").exists())
        self.assertFalse(self.temp.exists())
        self.assertTrue((self.private / RESET_MARKER).exists())
        storage.close()
        self.assertEqual(self.storage().snapshot().status, "reset_pending")

    def test_readonly_directory_file_and_runtime_write_failure_quarantine(self) -> None:
        storage, delivery = self.seed()
        storage.close()
        for path, mode in ((self.state, 0o500), (self.state / DB_NAME, 0o400)):
            original = path.stat().st_mode & 0o777
            path.chmod(mode)
            failed = self.storage()
            self.assertEqual(failed.snapshot().status, "recovery_required")
            self.assertFalse(failed.snapshot().permits_operations)
            failed.close()
            path.chmod(original)
        storage = self.storage()
        assert storage._db is not None
        storage._db.execute("PRAGMA query_only=ON")
        with self.assertRaises(StorageUnavailable):
            storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        storage._db.execute("PRAGMA query_only=OFF")
        self.assertEqual(storage.snapshot().status, "recovery_required")
        storage.close()
        recovered = self.storage()
        self.assertIsNone(recovered.command(delivery.epoch, COMMAND))
        self.assertEqual(recovered.snapshot().registration.delivery_cursor, 0)  # type: ignore[union-attr]

    def test_single_owner_across_process_and_thread_handles(self) -> None:
        storage, _ = self.seed()
        self.assertEqual(self.storage().snapshot().status, "recovery_required")
        self.assertEqual(self.run_process("inspect")["status"], "recovery_required")
        self.assertEqual(storage.snapshot().status, "ready")

    def test_ambiguous_commit_after_started_is_reconciled_without_reexecution(self) -> None:
        storage, delivery = self.seed()
        saved = storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        storage.close()
        original_connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            fail_after_commit = False

            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                result = super().execute(sql, parameters)
                if sql == "COMMIT" and self.fail_after_commit:
                    raise sqlite3.OperationalError("synthetic commit response failure")
                return result

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=FailingConnection)

        with patch("brainoid_device.ledger.sqlite3.connect", side_effect=connect):
            storage = self.storage()
        assert isinstance(storage._db, FailingConnection)
        storage._db.fail_after_commit = True
        with self.assertRaises(StorageUnavailable):
            storage.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                1,
                grant_expires_at_ms=30000,
                now_ms=1,
                clock_trusted=True,
            )
        self.assertFalse(storage.snapshot().permits_operations)
        storage.close()
        recovered = self.storage()
        self.assertFalse(
            recovered.record_started(
                delivery.epoch,
                COMMAND,
                saved.execution_id,
                1,
                grant_expires_at_ms=30000,
                now_ms=2,
                clock_trusted=True,
            )
        )

    def test_daemon_health_state_and_private_cleanup_reset_integration(self) -> None:
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text("<p>synthetic</p>")
        application = DeviceApplication(
            Settings("127.0.0.1", 8765, static, self.private, self.state)
        )
        self.addCleanup(application.close)
        with TestClient(
            application.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 1)
        ) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            client.post(
                "/local/session",
                headers={"Origin": "http://127.0.0.1:8765", "X-Brainoid-Local": "1"},
            )
            self.assertEqual(client.get("/local/state").json()["storage_status"], "unregistered")
            self.assertTrue(application.storage.local_reset())
            self.assertEqual(client.get("/local/state").status_code, 401)
            client.post(
                "/local/session",
                headers={"Origin": "http://127.0.0.1:8765", "X-Brainoid-Local": "1"},
            )
            assert application.storage._db is not None
            application.storage._db.execute("PRAGMA query_only=ON")
            self.assertEqual(client.get("/healthz").status_code, 503)
            response = client.get("/local/state")
            self.assertEqual(response.json()["storage_status"], "recovery_required")
            self.assertNotIn(str(self.root), response.text)


class ResetTests(StorageFixture):
    def prepare_reset(self) -> tuple[DeviceStorage, CommandDelivery]:
        storage, delivery = self.seed()
        storage.receive_command(delivery, expected_cursor=0, now_ms=0, clock_trusted=True)
        for name in ("cloud_refresh", "home_assistant"):
            self.credentials.write(name, b"synthetic-token-" + name.encode())
            storage.save_credential_reference(delivery.epoch, name, name)
        self.temp.write_text("synthetic private text")
        return storage, delivery

    def test_complete_reset_is_idempotent_and_rebinding_gets_new_epoch(self) -> None:
        storage, delivery = self.prepare_reset()
        old_handle = CredentialStore(self.private)
        self.addCleanup(old_handle.close)
        self.assertTrue(storage.local_reset())
        self.assertEqual(storage.snapshot().status, "unregistered")
        self.assertEqual(list(self.private.iterdir()), [])
        self.assertFalse(self.temp.exists())
        for store in (self.credentials, old_handle):
            with self.assertRaises(FileNotFoundError):
                store.read("cloud_refresh")
        self.assertTrue(storage.local_reset())
        binding = storage.register(TENANT, DEVICE)
        self.assertNotEqual(binding.epoch, delivery.epoch)
        self.assertEqual(binding.delivery_cursor, 0)
        self.assertIsNone(storage.command(binding.epoch, COMMAND))
        self.assertIsNone(storage.credential_reference(binding.epoch, "cloud_refresh"))
        with self.assertRaises(LedgerConflict):
            storage.receive_command(delivery, expected_cursor=0, now_ms=1, clock_trusted=True)

    def test_unlink_failure_retains_fence_for_existing_fresh_and_separate_process_stores(
        self,
    ) -> None:
        storage, delivery = self.prepare_reset()
        with patch(
            "brainoid_device.storage.os.unlink", side_effect=PermissionError("synthetic-token")
        ):
            self.assertFalse(storage.local_reset())
        self.assertEqual(storage.snapshot().status, "reset_pending")
        for credential_store in (self.credentials, CredentialStore(self.private)):
            self.addCleanup(credential_store.close)
            with self.assertRaises(CredentialsUnavailable):
                credential_store.read("cloud_refresh")
            with self.assertRaises(CredentialsUnavailable):
                credential_store.write("home_assistant", b"synthetic-new")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path; from brainoid_device.storage import CredentialStore, CredentialsUnavailable; "
                "import sys; s=CredentialStore(Path(sys.argv[1]));\n"
                "try: s.read('cloud_refresh')\nexcept CredentialsUnavailable: print('fenced')\nfinally: s.close()",
                str(self.private),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fenced\n", ""))
        with self.assertRaises(StorageUnavailable):
            storage.command(delivery.epoch, COMMAND)
        self.assertTrue(storage.local_reset())
        self.assertEqual(storage.snapshot().status, "unregistered")

    def test_private_cleanup_failure_and_malformed_leftover_resume_on_restart(self) -> None:
        storage, _ = self.prepare_reset()
        with patch.object(storage, "_cleanup_private", side_effect=OSError("private body")):
            self.assertFalse(storage.local_reset())
        self.assertTrue(self.temp.exists())
        self.assertEqual(set(p.name for p in self.private.iterdir()), {RESET_MARKER})
        storage.close()
        self.credentials.close()
        (self.private / "leftover").symlink_to(self.temp)
        self.credentials = CredentialStore(self.private)
        self.addCleanup(self.credentials.close)
        resumed = self.storage()
        self.assertEqual(resumed.snapshot().status, "unregistered")
        self.assertFalse(self.temp.exists())
        self.assertEqual(list(self.private.iterdir()), [])

    def test_reset_sql_failure_keeps_fence_and_retry_requires_reopen(self) -> None:
        storage, _ = self.prepare_reset()
        assert storage._db is not None
        storage._db.execute("PRAGMA query_only=ON")
        self.assertFalse(storage.local_reset())
        self.assertTrue(self.credentials.reset_pending)
        self.assertFalse(storage.local_reset())
        storage.close()
        self.assertEqual(self.storage().snapshot().status, "unregistered")

    def test_reset_fsync_failure_does_not_delete_anything_before_durable_intent(self) -> None:
        storage, _ = self.prepare_reset()
        with patch(
            "brainoid_device.storage.os.fsync", side_effect=OSError("synthetic fsync failure")
        ):
            self.assertFalse(storage.local_reset())
        self.assertTrue(self.temp.exists())
        self.assertTrue((self.private / "cloud_refresh").exists())
        self.assertTrue((self.private / RESET_MARKER).exists())
        self.assertEqual(storage.snapshot().status, "reset_pending")
        storage.close()
        self.assertEqual(self.storage().snapshot().status, "unregistered")

    def test_final_marker_removal_failure_keeps_even_empty_reset_fenced(self) -> None:
        storage, _ = self.prepare_reset()
        with patch.object(
            self.credentials, "finish_reset", side_effect=PermissionError("synthetic")
        ):
            self.assertFalse(storage.local_reset())
        self.assertEqual(storage.snapshot().status, "reset_pending")
        self.assertEqual(set(path.name for path in self.private.iterdir()), {RESET_MARKER})
        self.assertFalse(self.temp.exists())
        assert storage._db is not None
        self.assertEqual(storage._db.execute("SELECT count(*) FROM registration").fetchone()[0], 0)
        self.assertTrue(storage.local_reset())

    def test_real_process_reset_interruptions_resume_before_any_operation(self) -> None:
        for phase in (
            "reset-intent",
            "reset-unlink",
            "reset-cleanup",
            "reset-sql",
            "reset-complete",
        ):
            with self.subTest(phase=phase):
                storage, _ = self.prepare_reset()
                storage.close()
                self.run_process(phase, 73)
                if phase != "reset-complete":
                    with self.assertRaises(CredentialsUnavailable):
                        self.credentials.read("cloud_refresh")
                result = self.run_process("inspect")
                self.assertEqual(
                    result,
                    {"status": "unregistered", "cursor": None, "command": None, "execution": None},
                )
                self.assertEqual(list(self.private.iterdir()), [])
                self.assertFalse(self.temp.exists())


if __name__ == "__main__":
    unittest.main()
