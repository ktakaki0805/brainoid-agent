"""BA-DEV-08/T-025: real TUF plus confined activation, restart and rollback."""

import http.client
import io
import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from brainoid_device.ledger import CommandDelivery, DeviceStorage
from brainoid_device.storage import CredentialStore
from brainoid_device.update_files import MAX_FILE, Release, UpdateError, archive_files
from brainoid_device.update_runtime import OwnedDaemon, check_storage
from brainoid_device.updater import ApplicationUpdater, Stopped, main
from device_update_fixture import FakeDaemon, Repository
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from tuf.api.exceptions import RepositoryError


class Crash(BaseException):
    pass


class UpdaterFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="brainoid-update-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.root = self.path / "updates"
        self.state = self.path / "state"
        self.private = self.path / "private"
        for path in (self.root, self.state, self.private):
            path.mkdir(mode=0o700)
        self.repository = Repository(self.path / "repository")
        self.daemon = FakeDaemon()
        self.credentials = CredentialStore(self.private)
        self.addCleanup(self.credentials.close)
        self.credentials.write("cloud_refresh", b"synthetic-secret")
        storage = DeviceStorage(self.state, self.credentials, cleanup_private=lambda: None)
        registration = storage.register(
            "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        )
        self.epoch = registration.epoch
        storage.set_generation(self.epoch, 1)
        self.ids = []
        for sequence in range(1, 4):
            command_id = f"33333333-3333-4333-8333-{sequence:012d}"
            delivery = CommandDelivery(
                registration.tenant_id,
                registration.device_id,
                self.epoch,
                1,
                command_id,
                sequence,
                "a" * 64,
                30000,
            )
            saved = storage.receive_command(
                delivery, expected_cursor=sequence - 1, now_ms=0, clock_trusted=True
            )
            self.ids.append((command_id, saved.execution_id))
            if sequence > 1:
                storage.record_started(
                    self.epoch,
                    command_id,
                    saved.execution_id,
                    1,
                    grant_expires_at_ms=30000,
                    now_ms=1,
                    clock_trusted=True,
                )
            if sequence == 3:
                storage.record_result(
                    self.epoch, command_id, saved.execution_id, status="outcome_unknown", now_ms=2
                )
        storage.close()

    def engine(self) -> ApplicationUpdater:
        updater = ApplicationUpdater(
            self.root,
            self.repository.trust,
            "https://fixture.invalid/",
            self.daemon,
            lambda: check_storage(self.state, self.private),
            fetcher=self.repository,
        )
        self.addCleanup(updater.close)
        return updater

    def install(self, updater: ApplicationUpdater, version: int) -> None:
        self.assertTrue(updater.activate(updater.prepare(self.repository.add(version))))

    def assert_preserved(self) -> None:
        storage = DeviceStorage(self.state, self.credentials, cleanup_private=lambda: None)
        try:
            for (command_id, execution_id), status in zip(
                self.ids, ("accepted", "started", "outcome_unknown"), strict=True
            ):
                saved = storage.command(self.epoch, command_id)
                assert saved is not None
                self.assertEqual((saved.execution_id, saved.status), (execution_id, status))
            self.assertEqual(storage.snapshot().registration.delivery_cursor, 3)  # type: ignore[union-attr]
        finally:
            storage.close()
        self.assertEqual(self.credentials.read("cloud_refresh"), b"synthetic-secret")


class VerificationTests(UpdaterFixture):
    def test_manifest_schema_and_expansion_limits(self) -> None:
        updater = self.engine()
        target = self.repository.add(1)
        archive = self.repository.path / "targets" / target
        manifest = json.loads(archive_files(archive)["release.json"])
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1] / "packaging/device/update-manifest.schema.json"
            ).read_bytes()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(manifest)
        for constant, limit in (("MAX_ENTRIES", 1), ("MAX_EXPANDED", 64)):
            with (
                patch("brainoid_device.update_files." + constant, limit),
                self.assertRaises(UpdateError),
            ):
                updater.prepare(target)
        self.assertNotIn(b"synthetic-secret", archive.read_bytes())
        # Extension headers and incomplete padding are rejected before extraction.
        content = bytearray(archive.read_bytes())
        content[156] = ord("x")
        malformed = self.path / "extension.tar"
        malformed.write_bytes(content)
        with self.assertRaises(UpdateError):
            archive_files(malformed)
        self.assertEqual(self.daemon.started, [])

    def test_missing_journal_cannot_rebootstrap_existing_release(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        updater.close()
        self.daemon.stop()
        (self.root / "state.json").unlink()
        with self.assertRaises(UpdateError):
            self.engine()

    def test_signed_update_rotation_restart_and_identity_preservation(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        self.repository.rotate()
        self.install(updater, 2)
        self.assertEqual(self.daemon.started, [1, 2])
        self.assertEqual(
            self.daemon.events,
            ["private_audio_stopped", "start:1", "private_audio_stopped", "start:2"],
        )
        updater.close()
        self.daemon.stop()
        # Known-good boot is offline and independent of fresh metadata expiry.
        self.repository.publish(expires=datetime.now(UTC) - timedelta(days=1))
        reopened = self.engine()
        reopened.resume()
        self.assertEqual(self.daemon.active, 2)
        self.assert_preserved()
        self.assertTrue((self.root / "metadata/root_history/2.root.json").is_file())

    def test_bad_hash_signature_expired_and_metadata_rollback_never_launch(self) -> None:
        updater = self.engine()
        target = self.repository.add(1)
        artifact = self.repository.path / "targets" / target
        original = artifact.read_bytes()
        artifact.write_bytes(original[:-1] + b"x")
        with self.assertRaises(RepositoryError):
            updater.prepare(target)
        artifact.write_bytes(original)
        timestamp = self.repository.path / "metadata/timestamp.json"
        original_timestamp = timestamp.read_bytes()
        value = json.loads(original_timestamp)
        value["signed"]["version"] += 1
        timestamp.write_text(json.dumps(value))
        with self.assertRaises(RepositoryError):
            updater.prepare(target)
        self.repository.publish(expires=datetime.now(UTC) - timedelta(days=1))
        with self.assertRaises(RepositoryError):
            updater.prepare(target)
        self.repository.publish()
        updater.prepare(target)
        timestamp.write_bytes(original_timestamp)
        with self.assertRaises(RepositoryError):
            updater.prepare(target)
        self.assertEqual(self.daemon.started, [])
        self.assertFalse((self.root / "current").exists())

    def test_signed_incompatible_os_arch_protocol_schema_and_abi_rejected(self) -> None:
        updater = self.engine()
        for index, manifest in enumerate(
            (
                {"os": "other"},
                {"architecture": "other"},
                {"protocol": 2},
                {"sqlite_schema": 1},
                {"sqlite_schema": 3},
                {"runtime_abi": "other"},
                {"version": True},
                {"format": 2},
            ),
            start=1,
        ):
            with self.subTest(manifest=manifest), self.assertRaises(UpdateError):
                updater.prepare(self.repository.add(index, manifest=cast(dict[str, Any], manifest)))
        self.assertEqual(self.daemon.started, [])

    def test_known_good_rollback_is_only_downgrade_exception(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        self.daemon.fail.add(2)
        self.assertFalse(updater.activate(updater.prepare(self.repository.add(2))))
        self.assertEqual(self.daemon.started, [1, 2, 1])
        self.assertEqual(updater.state["floor"], 2)
        self.assertEqual(updater.state["good"]["version"], 1)
        for version in (1, 2):
            with self.assertRaises(UpdateError):
                updater.prepare(f"device-{version}.tar")
        self.assert_preserved()
        self.assertEqual(
            os.readlink(self.root / "current"),
            f"releases/{updater.state['good']['version']}-{updater.state['good']['sha256']}/tree",
        )

    def test_archive_cannot_carry_credentials_state_links_duplicates_or_bombs(self) -> None:
        updater = self.engine()
        for index, name in enumerate(
            (
                "../private/cloud_refresh",
                "/tmp/escape",
                "lib/../../escape.py",
                "private/cloud_refresh",
                "state/agent.sqlite3",
                "lib/bad\\name.py",
                "ui/.hidden.html",
                "lib/brainoid_device/unknown.schema.json",
                "lib/other/device_protocol.schema.json",
            ),
            start=1,
        ):
            with self.subTest(name=name), self.assertRaises(UpdateError):
                updater.prepare(self.repository.add(index, extra={name: b"untrusted"}))
        for kind in (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.CHRTYPE,
            tarfile.FIFOTYPE,
            tarfile.DIRTYPE,
        ):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                item = tarfile.TarInfo("ui/link.html")
                item.type = kind
                item.linkname = "../../private/cloud_refresh"
                archive.addfile(item)
            path = self.path / "unsafe.tar"
            path.write_bytes(stream.getvalue())
            with self.subTest(kind=kind), self.assertRaises(UpdateError):
                archive_files(path)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for _ in range(2):
                archive.addfile(tarfile.TarInfo("ui/index.html"), io.BytesIO())
        path.write_bytes(stream.getvalue())
        with self.assertRaises(UpdateError):
            archive_files(path)
        with self.assertRaises(UpdateError):
            updater.prepare(self.repository.add(20, extra={"ui/huge.html": b"x" * (MAX_FILE + 1)}))
        self.assert_preserved()

    def test_lock_unsafe_existing_paths_and_release_modification_fail_closed(self) -> None:
        updater = self.engine()
        with self.assertRaises(BlockingIOError):
            self.engine()
        self.install(updater, 1)
        updater.close()
        self.daemon.stop()
        good = self.root / os.readlink(self.root / "current")
        (good / "ui/index.html").write_text("modified")
        reopened = self.engine()
        with self.assertRaises(UpdateError):
            reopened.resume()
        self.assertEqual(reopened.state["phase"], "blocked")
        self.assertFalse((self.root / "current").exists())
        reopened.close()
        (self.root / "metadata/evil").symlink_to(self.private)
        with self.assertRaises(UpdateError):
            self.engine()

    def test_reset_future_schema_and_existing_storage_owner_prevent_switch(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        release = updater.prepare(self.repository.add(2))
        (self.private / ".reset-pending").write_bytes(b"")
        (self.private / ".reset-pending").chmod(0o600)
        with self.assertRaises(UpdateError):
            updater.activate(release)
        self.assertEqual(updater.state["phase"], "blocked")
        self.assertEqual((self.private / "cloud_refresh").read_bytes(), b"synthetic-secret")
        (self.private / ".reset-pending").unlink()
        storage = DeviceStorage(self.state, self.credentials, cleanup_private=lambda: None)
        try:
            with self.assertRaises(UpdateError):
                check_storage(self.state, self.private)
        finally:
            storage.close()
        with sqlite3.connect(self.state / "agent.sqlite3") as db:
            db.execute("PRAGMA user_version=3")
        before = (self.state / "agent.sqlite3").read_bytes()
        with self.assertRaises(UpdateError):
            check_storage(self.state, self.private)
        self.assertEqual((self.state / "agent.sqlite3").read_bytes(), before)


class FailureTests(UpdaterFixture):
    def test_cli_shutdown_failure_cannot_return_success(self) -> None:
        trust = self.path / "trust.json"
        trust.write_bytes(self.repository.trust)
        argv = [
            "brainoid-update",
            "--root",
            str(self.root),
            "--trust-root",
            str(trust),
            "--repository",
            "http://127.0.0.1:8766/",
            "--state",
            str(self.state),
            "--credentials",
            str(self.private),
            "--test-repository",
        ]
        daemon = MagicMock()
        daemon.exited.side_effect = Stopped()
        daemon.stop.side_effect = UpdateError()
        previous = logging.root.manager.disable
        self.addCleanup(logging.disable, previous)
        with (
            patch("sys.argv", argv),
            patch("brainoid_device.updater.OwnedDaemon", return_value=daemon),
            patch("brainoid_device.updater.ApplicationUpdater"),
            patch("sys.stdout"),
            patch("sys.stderr") as diagnostics,
        ):
            self.assertEqual(main(), 1)
        self.assertTrue(
            any(
                "device_update_shutdown_failed" in str(call)
                for call in diagnostics.write.call_args_list
            )
        )

    def test_extract_and_rename_failure_retain_old_reference(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        old = os.readlink(self.root / "current")
        target = self.repository.add(2)
        for function in (
            "brainoid_device.update_files.write_new",
            "brainoid_device.updater.os.rename",
        ):
            with (
                patch(function, side_effect=OSError("synthetic disk fault")),
                self.assertRaises(OSError),
            ):
                updater.prepare(target)
            self.assertEqual(os.readlink(self.root / "current"), old)
            self.assertEqual(self.daemon.active, 1)
            self.assertFalse(list(self.root.glob("stage-*")))

    def test_restart_at_each_persisted_boundary(self) -> None:
        for phase in (
            "download",
            "extract",
            "release",
            "pending",
            "quiesced",
            "pointer_staged",
            "pointer",
            "launch",
            "health",
            "committed",
        ):
            with self.subTest(phase=phase):
                # Each scenario gets its own independent durable state/cache.
                self.root = self.path / f"update-{phase}"
                self.root.mkdir(mode=0o700)
                self.daemon = FakeDaemon()
                updater = self.engine()
                self.install(updater, 1)
                target = self.repository.add(2)

                def crash(point: str, selected: str = phase) -> None:
                    if point == selected:
                        raise Crash()

                updater.checkpoint = crash
                with self.assertRaises(Crash):
                    updater.activate(updater.prepare(target))
                updater.close()
                self.daemon.stop()
                recovered = self.engine()
                recovered.resume()
                expected = 2 if phase == "committed" else 1
                self.assertEqual(self.daemon.active, expected)
                self.assertEqual(recovered.state["phase"], "stable")
                self.assertEqual(recovered.state["good"]["version"], expected)
                self.assertEqual(
                    recovered.state["floor"],
                    1 if phase in {"download", "extract", "release"} else 2,
                )
                self.assert_preserved()
                recovered.close()

    def test_final_commit_replace_and_fsync_failure_restore_prior_good(self) -> None:
        replace_file, sync = os.replace, os.fsync
        for failure in ("replace", "fsync"):
            with self.subTest(failure=failure):
                self.root = self.path / failure
                self.root.mkdir(mode=0o700)
                self.daemon = FakeDaemon()
                updater = self.engine()
                self.install(updater, 1)
                release = updater.prepare(self.repository.add(2))
                failed, fail_sync = False, False

                def replace_fault(
                    source: Path,
                    destination: Path,
                    candidate: Release = release,
                    mode: str = failure,
                ) -> None:
                    nonlocal failed, fail_sync
                    if destination.name == "state.json" and not failed:
                        value = json.loads(source.read_bytes())
                        if value.get("good") == candidate.json():
                            failed = True
                            if mode == "replace":
                                raise OSError("synthetic journal replace failure")
                            fail_sync = True
                    replace_file(source, destination)

                def sync_fault(fd: int) -> None:
                    nonlocal fail_sync
                    if fail_sync:
                        fail_sync = False
                        raise OSError("synthetic journal directory fsync failure")
                    sync(fd)

                with (
                    patch("os.replace", side_effect=replace_fault),
                    patch("os.fsync", side_effect=sync_fault),
                ):
                    self.assertFalse(updater.activate(release))
                self.assertTrue(failed)
                self.assertEqual(self.daemon.started, [1, 2, 1])
                self.assertEqual(
                    json.loads((self.root / "state.json").read_bytes())["good"]["version"], 1
                )
                self.assertEqual(updater.state["floor"], 2)
                updater.close()
                self.daemon.stop()
                self.engine().resume()
                self.assertEqual(self.daemon.active, 1)
                self.assert_preserved()

    def test_rollback_failure_stops_further_updates_and_preserves_evidence(self) -> None:
        updater = self.engine()
        self.install(updater, 1)
        self.daemon.fail = {1, 2}
        with self.assertRaises(UpdateError):
            updater.activate(updater.prepare(self.repository.add(2)))
        self.assertEqual(updater.state["phase"], "blocked")
        self.assertEqual(updater.state["good"]["version"], 1)
        self.assertEqual(updater.state["pending"]["version"], 2)
        self.assertIsNone(self.daemon.active)
        updater.close()
        recovered = self.engine()
        with self.assertRaises(UpdateError):
            recovered.resume()
        self.assert_preserved()


@unittest.skipUnless(sys.platform == "linux", "requires Linux process and directory ownership")
class ActualCompositionTests(UpdaterFixture):
    def real_daemon(self) -> OwnedDaemon:
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        daemon = OwnedDaemon(self.state, self.private, "127.0.0.1", port)
        self.addCleanup(daemon.stop)
        return daemon

    def test_real_daemon_ui_session_cleanup_health_failure_and_ledger_survive(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        self.install(updater, 1)
        connection = http.client.HTTPConnection("127.0.0.1", daemon.port, timeout=2)
        self.addCleanup(connection.close)
        origin = f"http://127.0.0.1:{daemon.port}"
        connection.request(
            "POST", "/local/session", headers={"Origin": origin, "X-Brainoid-Local": "1"}
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]  # type: ignore[union-attr]
        response.read()
        old_child = daemon.process
        self.install(updater, 2)
        assert old_child is not None
        self.assertEqual(old_child.poll(), 0)
        connection.close()
        connection.request("GET", "/local/state", headers={"Cookie": cookie})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        self.assertFalse(updater.activate(updater.prepare(self.repository.add(3, broken=True))))
        self.assertEqual(updater.state["good"]["version"], 2)
        self.assertTrue(daemon.healthy())
        daemon.stop()
        self.assert_preserved()

    def test_unrelated_listener_cannot_confirm_health_or_be_stopped(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        release = updater.prepare(self.repository.add(1))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", daemon.port))
            listener.listen()
            updater.daemon = daemon
            with self.assertRaises(UpdateError):
                updater.activate(release)
            self.assertIsNone(daemon.process)
            self.assertGreater(listener.fileno(), 0)
            self.assertEqual(listener.getsockname()[1], daemon.port)

    def test_actual_audio_task_and_child_are_quiesced_before_switch(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        marker = self.path / "audio-closed"
        source = f"""
import asyncio, sys
from pathlib import Path
from brainoid_device.app import DeviceApplication
from brainoid_device.config import load_settings
from brainoid_device.server import serve
async def startup(runtime):
    async def audio():
        try:
            await asyncio.Event().wait()
        finally:
            Path({str(marker)!r}).write_text('closed')
    runtime.start_task(audio())
    await runtime.start_child([sys.executable, '-c', 'import time; time.sleep(60)'])
def main():
    settings = load_settings()
    app = DeviceApplication(settings, startup=startup)
    try:
        return 0 if serve(app, settings) else 1
    finally:
        app.close()
"""
        target = self.repository.add(1, extra={"lib/brainoid_device/__main__.py": source.encode()})
        self.assertTrue(updater.activate(updater.prepare(target)))
        release = updater.prepare(self.repository.add(2))

        def quiesced(phase: str) -> None:
            if phase == "pointer_staged":
                self.assertEqual(marker.read_text(), "closed")

        updater.checkpoint = quiesced
        self.assertTrue(updater.activate(release))
        daemon.stop()
        self.assert_preserved()

    def regrouped_release(self, version: int, marker: Path, *, fail: bool) -> str:
        child_source = (
            "import json,os,signal,time; from pathlib import Path; os.setpgrp(); "
            + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if fail else "")
            + "fields=Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split(); "
            + f"Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(),birth=fields[19],"
            + "session=os.getsid(0),group=os.getpgrp()))); time.sleep(60)"
        )
        if fail:
            source = f"""
import subprocess, sys, time
from pathlib import Path
def main():
    subprocess.Popen([sys.executable, '-c', {child_source!r}])
    while not Path({str(marker)!r}).exists():
        time.sleep(0.01)
    return 1
"""
        else:
            # The command leader owns a dedicated session. Put the regrouping
            # process beneath it so setpgrp exercises a distinct descendant group.
            command_source = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child_source!r}]).wait()"
            source = f"""
import asyncio, sys
from pathlib import Path
from brainoid_device.app import DeviceApplication
from brainoid_device.config import load_settings
from brainoid_device.server import serve
async def startup(runtime):
    await runtime.start_child([sys.executable, '-c', {command_source!r}])
    while not Path({str(marker)!r}).exists():
        await asyncio.sleep(0.01)
def main():
    settings = load_settings()
    app = DeviceApplication(settings, startup=startup)
    try:
        return 0 if serve(app, settings) else 1
    finally:
        app.close()
"""
        return self.repository.add(
            version, extra={"lib/brainoid_device/__main__.py": source.encode()}
        )

    def assert_owned_child_gone(self, marker: Path) -> None:
        owned = json.loads(marker.read_bytes())
        self.assertNotEqual(owned["group"], owned["session"])
        self.assertIsNone(OwnedDaemon.identity(owned["pid"]))

    def test_owned_orphan_descendant_reaped_before_rollback(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        self.install(updater, 1)
        marker = self.path / "orphan.json"
        target = self.regrouped_release(2, marker, fail=True)

        def checkpoint(phase: str) -> None:
            if phase == "pointer_staged" and marker.exists():
                self.assert_owned_child_gone(marker)

        updater.checkpoint = checkpoint
        self.assertFalse(updater.activate(updater.prepare(target)))
        self.assert_owned_child_gone(marker)
        self.assertEqual(updater.state["good"]["version"], 1)
        daemon.stop()
        self.assert_preserved()

    def test_other_process_group_closes_before_graceful_switch(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        marker = self.path / "audio-group.json"
        self.assertTrue(
            updater.activate(updater.prepare(self.regrouped_release(1, marker, fail=False)))
        )
        old = daemon.process
        assert old is not None
        owned = json.loads(marker.read_bytes())
        # Runtime commands have a dedicated session, while their trusted guardian
        # remains in the updater-owned daemon session and must drain before switch.
        self.assertNotEqual(owned["session"], old.pid)
        self.assertTrue(OwnedDaemon.session_members(old.pid))

        def checkpoint(phase: str) -> None:
            if phase == "pointer_staged":
                self.assert_owned_child_gone(marker)
                self.assertEqual(old.returncode, 0)

        updater.checkpoint = checkpoint
        self.install(updater, 2)
        daemon.stop()
        self.assert_preserved()

    def test_unreaped_session_member_blocks_rollback_and_retains_ownership(self) -> None:
        daemon = self.real_daemon()
        daemon.stop_seconds = 0.3
        daemon.kill_seconds = 0.15
        updater = self.engine()
        updater.daemon = daemon
        self.install(updater, 1)
        marker = self.path / "stubborn.json"
        target = self.regrouped_release(2, marker, fail=True)
        original = OwnedDaemon.signal_member

        def signal_fault(pid: int, birth: str, session: int, sig: signal.Signals) -> None:
            if marker.exists() and pid == json.loads(marker.read_bytes())["pid"]:
                return  # Inject kernel inability to deliver to one exact owned child.
            original(pid, birth, session, sig)

        with patch.object(OwnedDaemon, "signal_member", side_effect=signal_fault):
            with self.assertRaises(UpdateError):
                updater.activate(updater.prepare(target))
            self.assertEqual(updater.state["phase"], "blocked")
            self.assertFalse(daemon.stopped())
            with self.assertRaises(UpdateError):
                daemon.start(self.root / "unused")
        # Remove injection and drain only our retained child/session.
        with self.assertRaises(UpdateError):
            daemon.stop()
        self.assertTrue(daemon.stopped())
        self.assert_owned_child_gone(marker)
        self.assert_preserved()

    def test_pidfd_identity_check_and_unrelated_process_are_preserved(self) -> None:
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )

        def close_unrelated() -> None:
            unrelated.terminate()
            unrelated.wait(timeout=5)

        self.addCleanup(close_unrelated)
        identity = OwnedDaemon.identity(unrelated.pid)
        assert identity is not None
        # Same numeric PID but a stale birth or another session is never signaled.
        with patch("signal.pidfd_send_signal", wraps=signal.pidfd_send_signal) as sent:
            OwnedDaemon.signal_member(
                unrelated.pid, identity[1] + "stale", identity[0], signal.SIGKILL
            )
            OwnedDaemon.signal_member(unrelated.pid, identity[1], identity[0] + 1, signal.SIGKILL)
            sent.assert_not_called()
        with (
            patch.object(
                OwnedDaemon, "session_members", return_value={unrelated.pid: identity[1] + "stale"}
            ),
            patch("os.waitid", wraps=os.waitid) as waited,
        ):
            OwnedDaemon.reap_members(identity[0])
            waited.assert_not_called()
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        self.install(updater, 1)
        daemon.stop()
        self.assertIsNone(unrelated.poll())
        reaped = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        self.assertEqual(reaped.wait(timeout=5), 0)
        daemon.process = reaped
        with patch("signal.pidfd_send_signal") as sent:
            daemon.stop()
            sent.assert_not_called()
        self.assertIsNone(unrelated.poll())

    def test_slow_headers_health_and_ui_bodies_share_one_wall_deadline(self) -> None:
        daemon = self.real_daemon()
        updater = self.engine()
        updater.daemon = daemon
        self.install(updater, 1)
        daemon.health_seconds = 0.5

        def sockets() -> set[str]:
            values = set()
            for fd in Path("/proc/self/fd").iterdir():
                try:
                    value = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if value.startswith("socket:["):
                    values.add(value)
            return values

        for version, phase in enumerate(("headers", "health_body", "ui_body"), start=2):
            with self.subTest(phase=phase):
                marker = self.path / f"slow-{phase}.json"
                source = rf"""
import http.server, json, os, time
from pathlib import Path
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        health = self.path == '/healthz'
        body = b'{{"status":"ok"}}' if health else Path(os.environ['DEVICE_STATIC_DIR'], 'index.html').read_bytes()
        headers = ('HTTP/1.0 200 OK\r\nContent-Length: ' + str(len(body)) + '\r\nX-Brainoid-Activation: ' + os.environ['DEVICE_ACTIVATION_NONCE'] + '\r\nConnection: close\r\n\r\n').encode()
        slow_headers = {phase!r} == 'headers' and health
        slow_body = ({phase!r} == 'health_body' and health) or ({phase!r} == 'ui_body' and not health)
        if slow_headers or slow_body:
            fields = Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()
            Path({str(marker)!r}).write_text(json.dumps(dict(pid=os.getpid(),birth=fields[19])))
        for data, slow in ((headers, slow_headers), (body, slow_body)):
            if slow:
                for byte in data:
                    self.connection.sendall(bytes([byte]))
                    time.sleep(0.08)
            else:
                self.connection.sendall(data)
def main():
    with http.server.HTTPServer(('127.0.0.1', int(os.environ['DEVICE_PORT'])), Handler) as server:
        server.serve_forever()
"""
                compile(source, "synthetic-release", "exec")
                release = updater.prepare(
                    self.repository.add(
                        version, extra={"lib/brainoid_device/__main__.py": source.encode()}
                    )
                )
                observed: list[tuple[bool, float]] = []
                healthy = daemon.healthy

                def timed_health(
                    probe: Callable[[], bool] = healthy,
                    samples: list[tuple[bool, float]] = observed,
                ) -> bool:
                    started = time.monotonic()
                    result = probe()
                    samples.append((result, time.monotonic() - started))
                    return result

                before_sockets = sockets()
                previous_alarm = signal.getsignal(signal.SIGALRM)
                with patch.object(daemon, "healthy", side_effect=timed_health):
                    started = time.monotonic()
                    self.assertFalse(updater.activate(release))
                    elapsed = time.monotonic() - started
                self.assertTrue(marker.exists(), "candidate reached the slow stream")
                owned = json.loads(marker.read_bytes())
                self.assertIsNone(OwnedDaemon.identity(owned["pid"]))
                self.assertEqual(observed[0][0], False)
                self.assertLess(observed[0][1], daemon.health_seconds + 0.25)
                self.assertTrue(observed[-1][0], "last good passed its separate health check")
                self.assertLess(elapsed, 3)
                self.assertEqual(updater.state["good"]["version"], 1)
                self.assertEqual(sockets(), before_sockets, "no timed-out reader socket survived")
                self.assertEqual(signal.getsignal(signal.SIGALRM), previous_alarm)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                print(
                    json.dumps(
                        {
                            "health_deadline_case": phase,
                            "deadline_seconds": daemon.health_seconds,
                            "health_results_seconds": observed,
                            "activation_seconds": elapsed,
                            "candidate_pid": owned["pid"],
                            "candidate_birth": owned["birth"],
                            "candidate_reaped": True,
                            "parent_reader_sockets_closed": True,
                        }
                    )
                )
        daemon.stop()
        with socket.socket() as closed:
            self.assertNotEqual(closed.connect_ex(("127.0.0.1", daemon.port)), 0)
        self.assert_preserved()

    def test_real_process_crashes_recover_without_running_pending_release(self) -> None:
        # Real _exit skips Python finally: staging leftovers and orphan ownership
        # are exercised independently of the in-process exception tests.
        for phase in ("download", "pointer_staged", "launch", "committed"):
            with self.subTest(phase=phase):
                scenario = self.path / phase
                scenario.mkdir(mode=0o700)
                for name in ("state", "private", "updates"):
                    (scenario / name).mkdir(mode=0o700)
                credentials = CredentialStore(scenario / "private")
                storage = DeviceStorage(
                    scenario / "state", credentials, cleanup_private=lambda: None
                )
                storage.close()
                credentials.close()
                repository = Repository(scenario / "repository")
                (scenario / "trust.json").write_bytes(repository.trust)
                with socket.socket() as available:
                    available.bind(("127.0.0.1", 0))
                    port = available.getsockname()[1]
                daemon = OwnedDaemon(scenario / "state", scenario / "private", "127.0.0.1", port)
                updater = ApplicationUpdater(
                    scenario / "updates",
                    repository.trust,
                    "https://fixture.invalid/",
                    daemon,
                    partial(check_storage, scenario / "state", scenario / "private"),
                    fetcher=repository,
                )
                try:
                    self.assertTrue(updater.activate(updater.prepare(repository.add(1))))
                finally:
                    daemon.stop()
                    updater.close()
                repository.add(2)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).with_name("device_update_process.py")),
                        str(scenario),
                        phase,
                        str(port),
                    ],
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 77, result.stderr)
                pid = json.loads((scenario / "owned.json").read_bytes())["pid"]
                if pid is not None:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            waited, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            waited = pid if not Path(f"/proc/{pid}").exists() else 0
                        if waited:
                            break
                        time.sleep(0.05)
                    else:
                        self.fail("parent-death cleanup failed")
                recovered = ApplicationUpdater(
                    scenario / "updates",
                    repository.trust,
                    "https://fixture.invalid/",
                    daemon,
                    partial(check_storage, scenario / "state", scenario / "private"),
                    fetcher=repository,
                )
                try:
                    recovered.resume()
                    self.assertTrue(daemon.healthy())
                    self.assertEqual(
                        recovered.state["good"]["version"], 2 if phase == "committed" else 1
                    )
                    self.assertFalse(list((scenario / "updates").glob("stage-*")))
                finally:
                    daemon.stop()
                    recovered.close()

    def test_cli_requires_explicit_test_trust_and_closes_on_signal(self) -> None:
        # Serve only this synthetic repository, in an owned child process.
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            repository_port = available.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "http.server",
                str(repository_port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(self.repository.path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop_server() -> None:
            server.terminate()
            server.wait(timeout=5)

        self.addCleanup(stop_server)
        target = self.repository.add(1)
        trust = self.path / "test-root.json"
        trust.write_bytes(self.repository.trust)
        daemon = self.real_daemon()
        argv = [
            sys.executable,
            "-B",
            "-m",
            "brainoid_device.updater",
            "--root",
            str(self.root),
            "--trust-root",
            str(trust),
            "--repository",
            f"http://127.0.0.1:{repository_port}/",
            "--state",
            str(self.state),
            "--credentials",
            str(self.private),
            "--port",
            str(daemon.port),
            "--target",
            target,
        ]
        denied = subprocess.run(argv, capture_output=True, timeout=10)
        self.assertEqual(denied.returncode, 1)
        self.assertNotIn(b"synthetic-secret", denied.stderr)
        process = subprocess.Popen(
            [*argv, "--test-repository"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        def stop_process() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()

        self.addCleanup(stop_process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if (self.root / "state.json").exists():
                state = json.loads((self.root / "state.json").read_bytes())
                if state["good"] is not None:
                    break
            if process.poll() is not None:
                self.fail(f"supervisor exited {process.returncode}")
            time.sleep(0.05)
        else:
            self.fail("supervisor did not activate")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=18)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, b"")
        self.assertIn(b"device_update_serving", stdout)
        with socket.socket() as client:
            self.assertNotEqual(client.connect_ex(("127.0.0.1", daemon.port)), 0)
        self.assert_preserved()
