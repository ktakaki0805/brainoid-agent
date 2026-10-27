"""Disposable system roots: real sysusers/enable, preservation and bounded failure."""

import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "device_provision", ROOT / "packaging/device/provision.py"
)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ba-dev-09-provision-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_real_image_accounts_and_boot_enable_twice_preserve_private_data(self) -> None:
        if not shutil.which("systemd-sysusers") or not shutil.which("systemctl"):
            self.skipTest("Linux systemd image tools required")
        provision.provision(self.root, image=True)
        passwd = (self.root / "etc/passwd").read_bytes()
        for path in ("var/lib/brainoid-agent/cloud", "var/lib/brainoid-agent-state/device.sqlite3"):
            target = self.root / path
            target.parent.mkdir(parents=True, mode=0o700)
            target.write_bytes(b"synthetic-private-preserve")
            target.chmod(0o600)
        config = self.root / provision.CONFIG
        config.write_bytes(config.read_bytes() + b"# administrator note retained\n")
        baseline = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [config, *(self.root / "var/lib").glob("*/*")]
        }
        provision.provision(self.root, image=True)
        self.assertEqual((self.root / "etc/passwd").read_bytes(), passwd)
        entries = [line.split(":") for line in passwd.decode().splitlines()]
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0][2], entries[1][2])
        self.assertTrue(
            all(int(entry[2]) > 0 and entry[6] == "/usr/sbin/nologin" for entry in entries)
        )
        for unit in provision.UNITS:
            link = self.root / "etc/systemd/system/multi-user.target.wants" / unit
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), "/etc/systemd/system/" + unit)
        kiosk = (self.root / "etc/systemd/system/brainoid-kiosk.service").read_text()
        self.assertIn("User=brainoid-kiosk", kiosk)
        self.assertIn("After=brainoid-agent.service", kiosk)
        self.assertIn("RuntimeDirectoryMode=0700", kiosk)
        self.assertIn("PAMName=brainoid-kiosk", kiosk)
        daemon = (self.root / "etc/systemd/system/brainoid-agent.service").read_text()
        self.assertIn("User=brainoid-agent", daemon)
        self.assertIn("StateDirectoryMode=0700", daemon)
        browser = (self.root / "usr/local/libexec/brainoid-kiosk-browser").read_text()
        self.assertIn("http://127.0.0.1:8765/", browser)
        self.assertNotIn("--no-sandbox", browser)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        provision.provision(self.root, image=True, uninstall=True)
        for unit in provision.UNITS:
            self.assertFalse((self.root / "etc/systemd/system" / unit).exists())
            self.assertFalse(
                (self.root / "etc/systemd/system/multi-user.target.wants" / unit).is_symlink()
            )
        self.assertEqual((self.root / "etc/passwd").read_bytes(), passwd)
        for name, digest in baseline.items():
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(), digest)
        provision.provision(self.root, image=True)
        self.assertEqual((self.root / "etc/passwd").read_bytes(), passwd)

    def test_malformed_root_symlink_and_collisions_fail_before_any_write(self) -> None:
        for root in (Path("relative"), self.root / "missing", self.root / ".."):
            with self.assertRaises((provision.ProvisionError, OSError)):
                provision.provision(root)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "etc").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(provision.ProvisionError):
            provision.provision(self.root)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "etc").unlink()
        target = self.root / "etc/systemd/system/brainoid-kiosk.service"
        target.parent.mkdir(parents=True)
        target.write_text("unrelated")
        with self.assertRaises(provision.ProvisionError):
            provision.provision(self.root)
        self.assertEqual(target.read_text(), "unrelated")
        self.assertFalse((self.root / provision.MANIFEST).exists())

    def test_partial_install_retries_and_partial_file_never_overwritten(self) -> None:
        original = provision.write_new
        count = 0

        def fail_before_file(path: Path, content: bytes, mode: int) -> None:
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("synthetic disk full")
            original(path, content, mode)

        with patch.object(provision, "write_new", fail_before_file), self.assertRaises(OSError):
            provision.provision(self.root)
        provision.provision(self.root)
        target = self.root / "etc/systemd/system/brainoid-kiosk.service"
        target.write_bytes(b"interrupted-or-edited")
        for uninstall in (False, True):
            with self.assertRaises(provision.ProvisionError):
                provision.provision(self.root, uninstall=uninstall)
            self.assertEqual(target.read_bytes(), b"interrupted-or-edited")

    def test_explicit_permissions_ignore_caller_umask_without_chmod_existing_parents(self) -> None:
        for mask in (0o022, 0o077, 0o002, 0o000):
            with self.subTest(mask=oct(mask)):
                root = self.root / str(mask)
                root.mkdir(mode=0o700)
                original = os.umask(mask)
                try:
                    provision.provision(root)
                    provision.provision(root)
                    for name, (_, mode) in provision.files().items():
                        path = root / name
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                        # Every new ancestor below the deliberately private test
                        # image root permits traversal by the separate kiosk UID.
                        for parent in path.parents:
                            if parent == root:
                                break
                            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
                    self.assertEqual(stat.S_IMODE((root / provision.CONFIG).stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                    provision.provision(root, uninstall=True)
                finally:
                    os.umask(original)

    def test_writable_selected_root_is_rejected_before_creating_output(self) -> None:
        for mode in (0o777, 0o1777, 0o775):
            self.root.chmod(mode)
            try:
                with self.assertRaises(provision.ProvisionError):
                    provision.provision(self.root)
                self.assertEqual(list(self.root.iterdir()), [])
            finally:
                self.root.chmod(0o700)
        # /tmp is a real root-owned sticky directory. Only call the read-only
        # preflight; never attempt provisioning or create outputs in shared /tmp.
        with self.assertRaises(provision.ProvisionError):
            provision.provision(Path("/tmp"))
        provision.provision(self.root)  # private child of that ancestor is valid

    def test_account_collision_and_failure_never_start_host_service(self) -> None:
        (self.root / "etc").mkdir()
        (self.root / "etc/passwd").write_text("brainoid-agent:x:0:0:unrelated:/root:/bin/bash\n")
        with self.assertRaises(provision.ProvisionError):
            provision.provision(self.root)
        self.assertFalse((self.root / provision.MANIFEST).exists())
        (self.root / "etc/passwd").unlink()
        calls: list[list[str]] = []

        def failed(args: list[str]) -> None:
            calls.append(args)
            raise subprocess.CalledProcessError(1, args)

        with self.assertRaises(subprocess.CalledProcessError):
            provision.provision(self.root, image=True, runner=failed)
        self.assertEqual(len(calls), 1)
        self.assertIn("--root=" + str(self.root), calls[0])
        self.assertFalse(any("start" in call for call in calls))
        with self.assertRaises(provision.ProvisionError):
            provision.provision(self.root, activate=True, runner=failed)
