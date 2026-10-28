"""The signed device update must boot against the previous installed contracts layout."""

import shutil
import subprocess
import sys
from pathlib import Path

from brainoid_contracts import device_snapshot as api_models
from brainoid_device import snapshot_models as device_models
from test_device_updater import UpdaterFixture


class SnapshotUpgradeTests(UpdaterFixture):
    def test_signed_candidate_imports_without_new_shared_module_and_matches_wire_models(
        self,
    ) -> None:
        for name in (
            "DeviceSnapshot",
            "DeviceSnapshotState",
            "SnapshotSelection",
            "PrivateSession",
            "RunAssignment",
            "NotificationView",
        ):
            self.assertEqual(
                getattr(api_models, name).model_json_schema(),
                getattr(device_models, name).model_json_schema(),
            )
        updater = self.engine()
        release = updater.prepare(self.repository.add(1))
        candidate = self.root / "releases" / release.name / "tree" / "lib"
        previous = self.path / "previous-bootstrap"
        previous.mkdir(mode=0o700)
        source = Path(api_models.__file__).parent
        shutil.copytree(
            source,
            previous / "brainoid_contracts",
            ignore=shutil.ignore_patterns("device_snapshot.py", "__pycache__"),
        )
        probe = self.path / "probe"
        probe.mkdir(mode=0o700)
        code = """
import asyncio, importlib.util, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import brainoid_contracts, brainoid_device.app
assert Path(brainoid_contracts.__file__).parent.parent == Path(sys.argv[1])
assert importlib.util.find_spec('brainoid_contracts.device_snapshot') is None
assert Path(brainoid_device.app.__file__).parent.parent == Path(sys.argv[2])
from brainoid_device.app import DeviceApplication
from brainoid_device.config import Settings, CloudSettings
root = Path(sys.argv[3])
for name in ('assets', 'state', 'credentials'):
    (root / name).mkdir(mode=0o700)
(root / 'assets' / 'index.html').write_text('<!doctype html><title>Synthetic upgrade</title>')
app = DeviceApplication(Settings('127.0.0.1', 18888, root / 'assets', root / 'credentials', root / 'state', CloudSettings('http://127.0.0.1:18999', local_test=True)))
assert app.snapshot_sync is not None
asyncio.run(app.cloud.close())
app.close()
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(previous), str(candidate), str(probe)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(updater.activate(release))
        self.assert_preserved()
