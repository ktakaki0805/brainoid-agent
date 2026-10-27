"""Synthetic subprocess fixture: abrupt exits bypass all Python cleanup."""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

from brainoid_device.ledger import CommandDelivery, DeviceStorage
from brainoid_device.migrations import migrate as original_migrate
from brainoid_device.storage import CredentialStore

TENANT = "11111111-1111-4111-8111-111111111111"
DEVICE = "22222222-2222-4222-8222-222222222222"
COMMAND = "33333333-3333-4333-8333-333333333333"


def main() -> None:
    root = Path(sys.argv[1])
    phase = sys.argv[2]
    credentials = CredentialStore(root / "credentials")
    if phase == "migration":

        def migrate(connection: sqlite3.Connection) -> None:
            def interrupt_migration(statement: str) -> None:
                if statement.startswith("ALTER TABLE"):
                    os._exit(73)

            connection.set_trace_callback(interrupt_migration)
            original_migrate(connection)

        patch("brainoid_device.ledger.migrate", migrate).start()

    def cleanup() -> None:
        (root / "private-temp").unlink(missing_ok=True)
        if phase == "reset-cleanup":
            os._exit(73)

    storage = DeviceStorage(root / "state", credentials, cleanup_private=cleanup)
    try:
        if phase == "inspect":
            snapshot = storage.snapshot()
            registration = snapshot.registration
            saved = storage.command(registration.epoch, COMMAND) if registration else None
            print(
                json.dumps(
                    {
                        "status": snapshot.status,
                        "cursor": registration.delivery_cursor if registration else None,
                        "command": saved.status if saved else None,
                        "execution": saved.execution_id if saved else None,
                    }
                )
            )
            return
        if phase == "read-credential":
            try:
                credentials.read("cloud_refresh")
            except RuntimeError:
                print("fenced")
            else:
                print("readable")
            return
        if phase.startswith("reset-"):
            if phase == "reset-intent":
                original_begin = credentials.begin_reset

                def begin() -> None:
                    original_begin()
                    os._exit(73)

                credentials.begin_reset = begin  # type: ignore[method-assign]
            elif phase == "reset-unlink":
                original_unlink = os.unlink

                def unlink(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    *,
                    dir_fd: int | None = None,
                ) -> None:
                    original_unlink(path, dir_fd=dir_fd)
                    os._exit(73)

                os.unlink = unlink
            elif phase == "reset-sql":

                def finish() -> None:
                    os._exit(73)

                credentials.finish_reset = finish  # type: ignore[method-assign]
            elif phase == "reset-complete":
                original_finish = credentials.finish_reset

                def finish_complete() -> None:
                    original_finish()
                    os._exit(73)

                credentials.finish_reset = finish_complete  # type: ignore[method-assign]
            storage.local_reset()
            raise AssertionError("reset interruption did not run")
        binding = storage.register(TENANT, DEVICE)
        storage.set_generation(binding.epoch, 1)
        if phase == "transaction":
            assert storage._db is not None

            def interrupt(statement: str) -> None:
                if statement.startswith("UPDATE registration SET delivery_cursor"):
                    os._exit(73)

            storage._db.set_trace_callback(interrupt)
        saved = storage.receive_command(
            CommandDelivery(TENANT, DEVICE, binding.epoch, 1, COMMAND, 1, "a" * 64, 30000),
            expected_cursor=0,
            now_ms=0,
            clock_trusted=True,
        )
        if phase == "committed":
            os._exit(73)
        if phase == "started":
            storage.record_started(
                binding.epoch,
                COMMAND,
                saved.execution_id,
                1,
                grant_expires_at_ms=30000,
                now_ms=1,
                clock_trusted=True,
            )
            os._exit(73)
        raise AssertionError("unknown fixture phase")
    finally:
        storage.close()
        credentials.close()


if __name__ == "__main__":
    main()
